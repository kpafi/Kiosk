from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_state (
    id            TEXT PRIMARY KEY,
    enabled       INTEGER NOT NULL DEFAULT 1,
    etag          TEXT,
    last_modified TEXT,
    last_fetch_at TEXT,
    last_ok_at    TEXT,
    last_error    TEXT,
    last_new      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       TEXT NOT NULL,
    guid            TEXT NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,                  -- reiner Text
    summary         TEXT NOT NULL DEFAULT '',       -- reiner Text
    author          TEXT,                           -- reiner Text
    image_url       TEXT,                           -- nur http(s)
    published_at    TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    body_html       TEXT,                           -- bereinigtes HTML
    body_text       TEXT,                           -- reiner Text für die Suche
    body_len        INTEGER NOT NULL DEFAULT 0,     -- Länge des reinen Texts
    text_status     TEXT NOT NULL DEFAULT 'pending',   -- pending | full | partial | empty | failed
    text_error      TEXT,
    extracted_at    TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,     -- vorübergehende Fehlschläge in Folge
    next_attempt_at TEXT,                           -- frühester nächster Versuch (NULL = sofort)
    paywalled       INTEGER NOT NULL DEFAULT 0,
    read            INTEGER NOT NULL DEFAULT 0,
    starred         INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source_id, guid)
);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles (published_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_articles_source ON articles (source_id, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles (text_status);

-- Bereits gesehene Feed-Einträge. Bleiben nach dem Löschen der Artikel (purge) länger erhalten,
-- damit Einträge ohne Datum (published_at = Zeitpunkt des ersten Abrufs) nicht nach Ablauf der
-- Aufbewahrungsfrist als "neu" wieder auftauchen.
CREATE TABLE IF NOT EXISTS seen_guids (
    source_id  TEXT NOT NULL,
    guid       TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (source_id, guid)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_seen_guids_first ON seen_guids (first_seen);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Spalten, die ältere Datenbanken noch nicht haben (Name, Definition für ALTER TABLE).
ADDED_COLUMNS = (
    ("body_text", "TEXT"),
    ("attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("next_attempt_at", "TEXT"),
)

# Volltextindex über den reinen Text. Die Trigger müssen exakt die indizierten Werte
# wieder löschen, sonst wird der External-Content-Index inkonsistent.
FTS_DDL = (
    "DROP TRIGGER IF EXISTS articles_ai",
    "DROP TRIGGER IF EXISTS articles_ad",
    "DROP TRIGGER IF EXISTS articles_au",
    "DROP TABLE IF EXISTS articles_fts",
    """CREATE VIRTUAL TABLE articles_fts USING fts5(
        title, summary, body_text, content='articles', content_rowid='id',
        tokenize='unicode61 remove_diacritics 2'
    )""",
    """CREATE TRIGGER articles_ai AFTER INSERT ON articles BEGIN
        INSERT INTO articles_fts(rowid, title, summary, body_text)
        VALUES (new.id, new.title, new.summary, new.body_text);
    END""",
    """CREATE TRIGGER articles_ad AFTER DELETE ON articles BEGIN
        INSERT INTO articles_fts(articles_fts, rowid, title, summary, body_text)
        VALUES ('delete', old.id, old.title, old.summary, old.body_text);
    END""",
    """CREATE TRIGGER articles_au AFTER UPDATE OF title, summary, body_text ON articles BEGIN
        INSERT INTO articles_fts(articles_fts, rowid, title, summary, body_text)
        VALUES ('delete', old.id, old.title, old.summary, old.body_text);
        INSERT INTO articles_fts(rowid, title, summary, body_text)
        VALUES (new.id, new.title, new.summary, new.body_text);
    END""",
)

# Wartezeiten nach vorübergehenden Fehlschlägen; danach gilt der Artikel als "failed".
RETRY_BACKOFF = (timedelta(hours=1), timedelta(hours=4), timedelta(hours=12))
MAX_RETRY_DELAY = timedelta(hours=12)

ARTICLE_LIST_COLS = ", ".join(
    f"a.{c}" for c in (
        "id", "source_id", "url", "title", "summary", "author", "image_url", "published_at",
        "fetched_at", "body_len", "text_status", "paywalled", "read", "starred",
    )
)


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    """Eine Verbindung, ein Lock. Für einen einzelnen Nutzer völlig ausreichend."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._conn.executescript(SCHEMA)  # executescript verwaltet die Transaktion selbst
            self._migrate()

    def _migrate(self) -> None:
        """Bringt eine bestehende Datenbank auf SCHEMA_VERSION. Mehrfach ausführbar."""
        row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        version = int(row["value"]) if row else 1
        if version >= SCHEMA_VERSION:
            return
        with self.tx() as c:
            if version < 2:
                self._migrate_v2(c)
            if version < 3:
                # seen_guids aus den vorhandenen Artikeln füllen (schnell, ein INSERT ... SELECT).
                c.execute("INSERT OR IGNORE INTO seen_guids (source_id, guid, first_seen) "
                          "SELECT source_id, guid, fetched_at FROM articles")
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                      (str(SCHEMA_VERSION),))

    @staticmethod
    def _migrate_v2(c: sqlite3.Connection) -> None:
        """v1 -> v2: neue Spalten, body_text aus body_html, FTS über reinen Text (ca. 2 ms pro Artikel)."""
        from .extract import html_to_text  # hier importiert, damit db.py ohne trafilatura-Import lädt

        existing = {r["name"] for r in c.execute("PRAGMA table_info(articles)")}
        for name, ddl in ADDED_COLUMNS:
            if name not in existing:
                c.execute(f"ALTER TABLE articles ADD COLUMN {name} {ddl}")
        # Alte Trigger zuerst weg, damit das Befüllen von body_text sie nicht auslöst.
        for stmt in FTS_DDL[:4]:
            c.execute(stmt)
        # In Blöcken zu 500 Zeilen (Keyset über die ID), damit nie alle HTML-Texte gleichzeitig im
        # Speicher liegen. Kein offener SELECT-Cursor während der UPDATEs auf derselben Tabelle
        # (dessen Verhalten wäre in SQLite nicht festgelegt); alles bleibt eine Transaktion.
        last_id = 0
        while True:
            rows = c.execute(
                "SELECT id, body_html FROM articles WHERE id > ? AND body_html IS NOT NULL "
                "AND body_text IS NULL ORDER BY id LIMIT 500", (last_id,)
            ).fetchall()
            if not rows:
                break
            for r in rows:
                c.execute("UPDATE articles SET body_text=? WHERE id=?", (html_to_text(r["body_html"]), r["id"]))
            last_id = rows[-1]["id"]
        for stmt in FTS_DDL[4:]:
            c.execute(stmt)
        c.execute("INSERT INTO articles_fts(articles_fts) VALUES('rebuild')")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def q(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- sources -------------------------------------------------------

    def ensure_sources(self, ids_with_default: list[tuple[str, bool]]) -> None:
        with self.tx() as c:
            for sid, enabled in ids_with_default:
                c.execute(
                    "INSERT OR IGNORE INTO source_state (id, enabled) VALUES (?, ?)",
                    (sid, 1 if enabled else 0),
                )

    def source_states(self) -> dict[str, sqlite3.Row]:
        return {r["id"]: r for r in self.q("SELECT * FROM source_state")}

    def set_source_enabled(self, sid: str, enabled: bool) -> None:
        with self.tx() as c:
            c.execute("UPDATE source_state SET enabled=? WHERE id=?", (1 if enabled else 0, sid))

    def enabled_source_ids(self) -> list[str]:
        return [r["id"] for r in self.q("SELECT id FROM source_state WHERE enabled=1")]

    def record_fetch(self, sid: str, *, ok: bool, error: str | None = None,
                     etag: str | None = None, last_modified: str | None = None,
                     new_count: int = 0) -> None:
        now = utcnow()
        with self.tx() as c:
            if ok:
                c.execute(
                    "UPDATE source_state SET last_fetch_at=?, last_ok_at=?, last_error=NULL, "
                    "etag=COALESCE(?, etag), last_modified=COALESCE(?, last_modified), last_new=? WHERE id=?",
                    (now, now, etag, last_modified, new_count, sid),
                )
            else:
                c.execute(
                    "UPDATE source_state SET last_fetch_at=?, last_error=? WHERE id=?",
                    (now, (error or "unbekannter Fehler")[:500], sid),
                )

    def source_counts(self) -> dict[str, dict[str, int]]:
        rows = self.q(
            "SELECT source_id, COUNT(*) AS total, SUM(read=0) AS unread FROM articles GROUP BY source_id"
        )
        return {r["source_id"]: {"total": r["total"], "unread": r["unread"]} for r in rows}

    # ---- articles ------------------------------------------------------

    def upsert_articles(self, rows: list[dict[str, Any]]) -> int:
        """Fügt neue Artikel ein, ignoriert bekannte. Gibt die Zahl der neuen zurück.

        Bekannt ist ein Eintrag, wenn (source_id, guid) in seen_guids steht, auch wenn der Artikel
        selbst schon gelöscht wurde.
        """
        new = 0
        with self.tx() as c:
            for r in rows:
                seen = c.execute(
                    "INSERT OR IGNORE INTO seen_guids (source_id, guid, first_seen) VALUES (?,?,?)",
                    (r["source_id"], r["guid"], r["fetched_at"]),
                )
                if seen.rowcount == 0:
                    continue
                cur = c.execute(
                    "INSERT OR IGNORE INTO articles (source_id, guid, url, title, summary, author, "
                    "image_url, published_at, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (r["source_id"], r["guid"], r["url"], r["title"], r["summary"], r.get("author"),
                     r.get("image_url"), r["published_at"], r["fetched_at"]),
                )
                new += cur.rowcount
        return new

    def pending_articles(self, limit: int, before: tuple[str, int] | None = None) -> list[sqlite3.Row]:
        """Fällige Artikel ohne Volltext, nur von aktuell eingeschalteten Quellen, neueste zuerst.

        `before` = (published_at, id) des letzten Artikels der vorigen Seite (Keyset-Blättern):
        Artikel, die nach einem Versuch noch 'pending' sind, kommen so nicht erneut.
        """
        where, params = "", [utcnow()]
        if before is not None:
            where = "AND (a.published_at, a.id) < (?, ?) "
            params += [before[0], before[1]]
        return self.q(
            "SELECT a.id, a.source_id, a.url, a.published_at FROM articles a "
            "JOIN source_state s ON s.id = a.source_id AND s.enabled = 1 "
            "WHERE a.text_status='pending' AND (a.next_attempt_at IS NULL OR a.next_attempt_at <= ?) "
            + where + "ORDER BY a.published_at DESC, a.id DESC LIMIT ?",
            (*params, limit),
        )

    def is_due_pending(self, article_id: int) -> bool:
        """True, wenn der Artikel noch 'pending' und fällig ist (next_attempt_at leer oder erreicht)."""
        return self.one(
            "SELECT 1 AS ok FROM articles WHERE id=? AND text_status='pending' "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?)",
            (article_id, utcnow()),
        ) is not None

    def store_extraction(self, article_id: int, *, status: str, body_html: str | None,
                         body_len: int, paywalled: bool, error: str | None = None,
                         image_url: str | None = None, author: str | None = None,
                         body_text: str | None = None) -> None:
        """Speichert ein endgültiges Ergebnis (auch endgültige Fehler) und setzt den Retry-Zähler zurück.

        Bringt das neue Ergebnis keinen Text (Fehler oder leere Extraktion), der Artikel hat aber
        schon welchen, bleiben Text, Länge, Paywall-Markierung und Status erhalten; nur
        text_error wird gesetzt. Ein 'pending' mit Text wird dabei zu 'partial', sonst würde er
        in jedem Lauf erneut geholt.
        """
        with self.tx() as c:
            if not body_text and not body_html and _has_text(c, article_id):
                c.execute(
                    "UPDATE articles SET text_error=?, attempts=0, next_attempt_at=NULL, "
                    "text_status=CASE WHEN text_status='pending' THEN 'partial' ELSE text_status END WHERE id=?",
                    ((error or f"Erneute Extraktion ohne Text ({status})")[:500], article_id),
                )
                return
            c.execute(
                "UPDATE articles SET text_status=?, body_html=?, body_text=?, body_len=?, paywalled=?, "
                "text_error=?, extracted_at=?, attempts=0, next_attempt_at=NULL, "
                "image_url=COALESCE(image_url, ?), author=COALESCE(author, ?) WHERE id=?",
                (status, body_html, body_text, body_len, 1 if paywalled else 0,
                 (error or None) and error[:500], utcnow(), image_url, author, article_id),
            )

    def schedule_retry(self, article_id: int, error: str, retry_after: float | None = None) -> str:
        """Vorübergehender Fehler: später erneut versuchen, nach len(RETRY_BACKOFF) Wiederholungen aufgeben.

        Gibt den neuen text_status zurück ('pending' oder 'failed'; bei vorhandenem Text den
        bisherigen, wobei ein 'pending' mit Text zu 'partial' wird).
        """
        with self.tx() as c:
            row = c.execute("SELECT attempts, text_status FROM articles WHERE id=?", (article_id,)).fetchone()
            if row is None:
                return "failed"
            attempts = row["attempts"] + 1
            now = datetime.now(timezone.utc)
            if _has_text(c, article_id):
                # Erneuter Versuch bei vorhandenem Text (z. B. ?retry=true bei "partial"): Text und
                # Status bleiben, nur Fehler und Zähler werden fortgeschrieben. Ausnahme: 'pending' mit
                # Text wird 'partial', sonst würde der Artikel ohne Ende erneut versucht.
                status = "partial" if row["text_status"] == "pending" else row["text_status"]
                due = None
                if attempts <= len(RETRY_BACKOFF):
                    delay = RETRY_BACKOFF[attempts - 1]
                    if retry_after is not None:
                        delay = min(max(timedelta(seconds=retry_after), timedelta(minutes=1)), MAX_RETRY_DELAY)
                    due = _iso(now + delay)
                c.execute("UPDATE articles SET text_status=?, text_error=?, attempts=?, next_attempt_at=? "
                          "WHERE id=?", (status, error[:500], attempts, due, article_id))
                return status
            if attempts > len(RETRY_BACKOFF):
                c.execute(
                    "UPDATE articles SET text_status='failed', text_error=?, extracted_at=?, attempts=?, "
                    "next_attempt_at=NULL WHERE id=?",
                    (error[:500], _iso(now), attempts, article_id),
                )
                return "failed"
            delay = RETRY_BACKOFF[attempts - 1]
            if retry_after is not None:
                delay = min(max(timedelta(seconds=retry_after), timedelta(minutes=1)), MAX_RETRY_DELAY)
            c.execute(
                "UPDATE articles SET text_status='pending', text_error=?, attempts=?, next_attempt_at=? WHERE id=?",
                (error[:500], attempts, _iso(now + delay), article_id),
            )
            return "pending"

    def requeue(self, article_id: int, error: str) -> None:
        """Versuch ohne eigenes Verschulden abgebrochen (z. B. Pool wegen eines hängenden Nachbarn
        verworfen): bleibt 'pending' und sofort fällig, der Versuch zählt nicht."""
        with self.tx() as c:
            c.execute("UPDATE articles SET next_attempt_at=NULL, text_error=? "
                      "WHERE id=? AND text_status='pending'", (error[:500], article_id))

    def published_at_of(self, article_id: int) -> str | None:
        r = self.one("SELECT published_at FROM articles WHERE id=?", (article_id,))
        return r["published_at"] if r else None

    def list_articles(self, *, sources: list[str] | None,
                      query: str | None, unread: bool, starred: bool, free_only: bool,
                      before: tuple[str, int] | None, limit: int, offset: int = 0) -> list[sqlite3.Row]:
        """Artikelliste. Ohne Suche per Keyset-Cursor `before` = (published_at, id),
        mit Suche (bm25-Sortierung) per `offset`."""
        where: list[str] = []
        params: list[Any] = []
        if sources:
            where.append(f"a.source_id IN ({','.join('?' * len(sources))})")
            params += sources
        if unread:
            where.append("a.read=0")
        if starred:
            where.append("a.starred=1")
        if free_only:
            where.append("a.paywalled=0")
        join = ""
        order = "a.published_at DESC, a.id DESC"
        if query:
            join = "JOIN articles_fts f ON f.rowid = a.id"
            where.append("articles_fts MATCH ?")
            params.append(_fts_query(query))
            order = "bm25(articles_fts), a.published_at DESC, a.id DESC"
        elif before is not None:
            where.append("(a.published_at, a.id) < (?, ?)")
            params += [before[0], before[1]]
        sql = (
            f"SELECT {ARTICLE_LIST_COLS} FROM articles a {join} "
            + (("WHERE " + " AND ".join(where)) if where else "")
            + f" ORDER BY {order} LIMIT ?"
        )
        params.append(limit)
        if query and offset:
            sql += " OFFSET ?"
            params.append(offset)
        return self.q(sql, tuple(params))

    def get_article(self, article_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM articles WHERE id=?", (article_id,))

    def set_flag(self, article_id: int, flag: str, value: bool) -> bool:
        assert flag in ("read", "starred")
        with self.tx() as c:
            cur = c.execute(f"UPDATE articles SET {flag}=? WHERE id=?", (1 if value else 0, article_id))
            return cur.rowcount > 0

    def mark_all_read(self, sources: list[str] | None) -> int:
        with self.tx() as c:
            if sources:
                cur = c.execute(
                    f"UPDATE articles SET read=1 WHERE read=0 AND source_id IN ({','.join('?' * len(sources))})",
                    tuple(sources),
                )
            else:
                cur = c.execute("UPDATE articles SET read=1 WHERE read=0")
            return cur.rowcount

    def purge(self, retention_days: int) -> int:
        """Löscht alte Artikel (Favoriten bleiben). seen_guids bleiben doppelt so lange erhalten."""
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=retention_days)).isoformat()
        seen_cutoff = _iso(now - timedelta(days=2 * retention_days))
        with self.tx() as c:
            cur = c.execute("DELETE FROM articles WHERE starred=0 AND published_at < ?", (cutoff,))
            c.execute("DELETE FROM seen_guids WHERE first_seen < ?", (seen_cutoff,))
            return cur.rowcount

    def stats(self) -> dict[str, Any]:
        row = self.one(
            "SELECT COUNT(*) AS total, SUM(read=0) AS unread, SUM(starred=1) AS starred, "
            "SUM(text_status='pending') AS pending, SUM(text_status='full') AS full, "
            "SUM(text_status='partial') AS partial, SUM(text_status='failed') AS failed, "
            "SUM(paywalled=1) AS paywalled, COALESCE(SUM(body_len),0) AS body_bytes, "
            "MIN(published_at) AS oldest, MAX(published_at) AS newest FROM articles"
        )
        size = 0
        for suffix in ("", "-wal"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                size += p.stat().st_size
        return {**dict(row), "db_bytes": size}

    def get_meta(self, key: str) -> str | None:
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def session_epoch(self) -> int:
        """Zähler für Sessions; steckt im signierten Cookie. Standard 1."""
        raw = self.get_meta("session_epoch")
        return int(raw) if raw and raw.isascii() and raw.isdigit() else 1

    def bump_session_epoch(self) -> int:
        """Erhöht den Zähler und macht damit alle bisher ausgegebenen Sessions ungültig."""
        with self.tx() as c:
            row = c.execute("SELECT value FROM meta WHERE key='session_epoch'").fetchone()
            current = int(row["value"]) if row and row["value"].isascii() and row["value"].isdigit() else 1
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('session_epoch', ?)", (str(current + 1),))
            return current + 1

    def ping(self) -> bool:
        """True, wenn die Datenbank antwortet."""
        try:
            return self.one("SELECT 1 AS ok")["ok"] == 1
        except sqlite3.Error:
            return False


def _has_text(c: sqlite3.Connection, article_id: int) -> bool:
    row = c.execute(
        "SELECT body_text IS NOT NULL AND body_text != '' OR body_html IS NOT NULL AND body_html != '' AS t "
        "FROM articles WHERE id=?", (article_id,),
    ).fetchone()
    return bool(row and row["t"])


def _fts_query(raw: str) -> str:
    """Macht aus freier Eingabe eine sichere FTS5-Abfrage: jedes Wort als Präfix, UND-verknüpft.

    Jedes Wort wird als FTS5-String in doppelte Anführungszeichen gesetzt; eingebettete
    Anführungszeichen werden dabei verdoppelt.
    """
    words = [w.strip('"\'*()^:-') for w in raw.split()]
    words = [w for w in words if w]
    if not words:
        return '""'
    return " AND ".join('"' + w.replace('"', '""') + '"*' for w in words)
