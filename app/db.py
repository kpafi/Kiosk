from __future__ import annotations

import json
import re
import sqlite3
import threading
import time as _time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from .config import DEFAULT_TZ

SCHEMA_VERSION = 8

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
    -- Seit Schema 6 unbenutzt (Lesestand steht in user_articles). SQLite kann Spalten nicht billig
    -- entfernen; kein Code darf sie lesen oder schreiben (tests/test_users.py prüft das).
    read            INTEGER NOT NULL DEFAULT 0,
    starred         INTEGER NOT NULL DEFAULT 0,
    later           INTEGER NOT NULL DEFAULT 0,
    story_id        INTEGER,                        -- Geschichte (v8): kleinste Artikel-ID der Gruppe, NULL = keine
    UNIQUE (source_id, guid)
);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles (published_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles (text_status);
-- Je Quelle nach Zeit, mit paywalled: deckt die Zähler je Quelle (source_counts,
-- unread_free_by_source) ganz ab. Ohne paywalled im Index liest SQLite jede Artikelzeile samt
-- Volltext, um die Spalte zu erreichen. Ersetzt idx_articles_source (source_id, published_at DESC),
-- neben dem der Planer den neuen Index für source_counts nicht nahm. SCHEMA läuft bei jedem Start:
-- bestehende Datenbanken bekommen den Tausch so ohne neue Schema-Version.
DROP INDEX IF EXISTS idx_articles_source;
CREATE INDEX IF NOT EXISTS idx_articles_source_counts ON articles (source_id, published_at, paywalled);
-- Artikelliste über viele Quellen (ALLE, Profile): nach Zeit mit Quelle und paywalled im Index, damit
-- die Liste in Zeitreihenfolge läuft, ohne für jeden Eintrag die Artikelzeile samt Volltext zu lesen,
-- bis LIMIT erreicht ist (siehe LIST_INDEX). Wie oben ohne neue Schema-Version: SCHEMA läuft bei jedem
-- Start, ältere Versionen der App stören sich an dem zusätzlichen Index nicht.
CREATE INDEX IF NOT EXISTS idx_articles_list ON articles (published_at DESC, id DESC, source_id, paywalled);

-- Bereits gesehene Feed-Einträge. Bleiben nach dem Löschen der Artikel (purge) länger erhalten,
-- damit Einträge ohne Datum (published_at = Zeitpunkt des ersten Abrufs) nicht nach Ablauf der
-- Aufbewahrungsfrist als "neu" wieder auftauchen. first_seen heißt nur noch aus historischen
-- Gründen so: upsert_articles frischt es auf (höchstens einmal täglich), solange der Eintrag im
-- Feed steht; purge löscht also nur Einträge, die seit 2x Aufbewahrungsfrist nicht mehr kamen.
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

-- Konten (v6). Angelegt per Kommandozeile (python -m app.users), keine Registrierung.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,  -- 2–32 Zeichen, [a-z0-9._-], klein geschrieben
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,        -- "scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>"
    is_admin      INTEGER NOT NULL DEFAULT 0,
    disabled      INTEGER NOT NULL DEFAULT 0,
    session_epoch INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

-- Lesestand pro Benutzer (v6); eine Zeile entsteht beim ersten Setzen eines Flags.
CREATE TABLE IF NOT EXISTS user_articles (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    read       INTEGER NOT NULL DEFAULT 0,
    starred    INTEGER NOT NULL DEFAULT 0,
    later      INTEGER NOT NULL DEFAULT 0,     -- "Später lesen"
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, article_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_user_articles_flags ON user_articles (user_id, read, starred, later);
-- Für purge (gemerkt von irgendwem?) und das Löschen per Cascade beim Entfernen alter Artikel.
CREATE INDEX IF NOT EXISTS idx_user_articles_article ON user_articles (article_id);
"""

# Tabellen pro Benutzer (v6). Ältere Datenbanken haben sie ohne user_id; die Migration legt sie
# neu an und kopiert um (SQLite kann Primärschlüssel nicht ändern).
USER_TABLES = """
-- Profile: gespeicherte Filter aus Quellen und Suchbegriffen; position gilt pro Benutzer.
CREATE TABLE IF NOT EXISTS profiles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    builtin      TEXT,                          -- 'de' | 'world' | 'business' | 'military' | NULL (eigenes)
    sources_json TEXT,                          -- JSON-Liste von Quellen-IDs oder NULL = alle aktiven
    terms_json   TEXT NOT NULL DEFAULT '[]',    -- JSON-Liste von Suchbegriffen (ODER, Präfix)
    in_bar       INTEGER NOT NULL DEFAULT 0,    -- unten in der Leiste
    in_chips     INTEGER NOT NULL DEFAULT 1,    -- oben bei den Filtern der Liste (v7)
    position     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_profiles_user ON profiles (user_id, position);

-- Lieblingszeitungen.
CREATE TABLE IF NOT EXISTS favorite_sources (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_id  TEXT NOT NULL,
    in_bar     INTEGER NOT NULL DEFAULT 1,
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, source_id)
);

-- Einstellungen der Oberfläche, Werte als JSON.
CREATE TABLE IF NOT EXISTS settings (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
"""

# Spalten der Tabellen aus USER_TABLES ohne user_id (für das Umkopieren in der Migration).
LEGACY_USER_COLUMNS = {
    "profiles": "id, name, builtin, sources_json, terms_json, in_bar, position, created_at",
    "favorite_sources": "source_id, in_bar, position, created_at",
    "settings": "key, value",
}

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

# Höchstlaufzeit einer einzelnen Leseabfrage (q/one) in Sekunden, während einer API-Anfrage: Alle
# Abfragen laufen auf der einen Event-Loop, eine teure Suche oder ein Profil voller Begriffe würde
# sonst alle anderen Anfragen blockieren. Die Frist gilt nur, wo request_time_limit gesetzt ist
# (Middleware in main.py für /api/); Hintergrundarbeit (Sammler, Extraktion, purge, Migration) und
# schreibende Transaktionen (tx) haben keine.
READ_TIME_LIMIT = 2.0
request_time_limit: ContextVar[float | None] = ContextVar("kiosk_request_time_limit", default=None)
_FROM_CONTEXT: Any = object()   # time_limit nicht angegeben: request_time_limit gilt
PROGRESS_STEPS = 10_000     # SQLite-VM-Schritte zwischen zwei Blicken auf die Uhr
# Suche: höchstens so viele Wörter (der Rest zählt nicht); Präfix-Suche erst ab PREFIX_MIN_LEN
# Zeichen, ein Präfix wie "a*" müsste die Trefferlisten Tausender Begriffe zusammenführen.
SEARCH_MAX_WORDS = 8
PREFIX_MIN_LEN = 3
# Wortzeichen wie im FTS-Tokenizer unicode61 (Buchstaben und Ziffern, kein Unterstrich).
_TOKEN_RE = re.compile(r"[^\W_]+")
# seen_guids.first_seen wird bei erneutem Auftauchen höchstens so oft aufgefrischt.
SEEN_REFRESH = timedelta(days=1)
# Leerraum am Rand eines Anreißers, wie stories._fields ihn abschneidet (Länge „kurzer Anreißer“ gleich
# in SQL und Python): Leerzeichen, Tab, Zeilenumbrüche, geschütztes Leerzeichen.
SUMMARY_TRIM = " \t\n\r\xa0"
# Große Schreibvorgänge in Blöcken mit je eigener Transaktion, dazwischen kommt die Event-Loop dran:
# purge (Artikel je Block) und „alles gelesen“ (Artikel-IDs je Block).
PURGE_BATCH = 200          # ca. 50-70 ms je Block (FTS-Trigger, Cascade), 80 000 Artikel
MARK_READ_ID_STEP = 2000   # ca. 45 ms je Block für einen frischen Benutzer
SAME_URL_WINDOW = timedelta(hours=6)  # gleiche Adresse und gleicher Titel so nah beieinander: derselbe Artikel

# Lesestand des Benutzers aus user_articles (Alias ua, per LEFT JOIN; ohne Zeile: 0).
FLAG_COLS = ("COALESCE(ua.read, 0) AS read, COALESCE(ua.starred, 0) AS starred, "
             "COALESCE(ua.later, 0) AS later")
USER_JOIN = "LEFT JOIN user_articles ua ON ua.article_id = a.id AND ua.user_id = ?"
# Zähler je Quelle nur aus dem Index (siehe SCHEMA): Der Planer wählt ihn nicht immer von selbst
# (z. B. ohne Aufbewahrungsgrenze), und jede Artikelzeile samt Volltext zu lesen kostet viel.
COUNT_INDEX = "INDEXED BY idx_articles_source_counts"
# Artikelliste über mehrere Quellen: Mit "a.source_id IN (...)" nimmt der Planer sonst den Index über
# (source_id, guid) bzw. (source_id, published_at), liest ALLE Artikel dieser Quellen samt Spalten und
# sortiert sie (TEMP B-TREE; 80 000 Artikel: 110-130 ms je Seite, auch für Seite 2 und „Nächster“). In
# Zeitreihenfolge über idx_articles_list endet die Abfrage nach LIMIT passenden Zeilen (unter 1 ms);
# selbst wenn fast nichts passt (fast alles gelesen, leere Quellen), prüft sie nur Indexeinträge
# (ca. 45 ms statt 125 ms). Nicht für eine einzelne Quelle (ihr Index liefert schon die Reihenfolge),
# für Suche (bm25) und für Gemerkt/Später (dort sind die wenigen Zeilen aus user_articles der beste Start).
LIST_INDEX = "INDEXED BY idx_articles_list"

ARTICLE_LIST_COLS = ", ".join(
    f"a.{c}" for c in (
        "id", "source_id", "url", "title", "summary", "author", "image_url", "published_at",
        "fetched_at", "body_len", "text_status", "paywalled",
    )
) + ", " + FLAG_COLS

# Alle Spalten eines Artikels außer den alten Flags (die kommen aus user_articles).
ARTICLE_DETAIL_COLS = ", ".join(
    f"a.{c}" for c in (
        "id", "source_id", "guid", "url", "title", "summary", "author", "image_url", "published_at",
        "fetched_at", "body_html", "body_text", "body_len", "text_status", "text_error",
        "extracted_at", "attempts", "next_attempt_at", "paywalled",
    )
) + ", " + FLAG_COLS


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def day_start(tz: tzinfo, now: datetime | None = None) -> str:
    """Beginn des Kalendertags von `now` (Standard: jetzt): 00:00 Ortszeit in `tz`, als UTC-Zeitpunkt
    im Format von published_at (_iso), damit der Textvergleich a.published_at >= ? stimmt.

    Über die Zone gerechnet, nicht als „jetzt minus Stunden“: Tage mit Zeitumstellung haben 23
    oder 25 Stunden. `now` muss eine Zeitzone haben."""
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    return _iso(datetime.combine(local.date(), time(0), tzinfo=tz))


class MigrationError(RuntimeError):
    """Die Datenbank lässt sich ohne weitere Angaben nicht migrieren (z. B. fehlt der Admin)."""


class QueryTimeout(Exception):
    """Eine Leseabfrage lief länger als erlaubt (READ_TIME_LIMIT) und wurde abgebrochen."""


class Database:
    """Eine Verbindung, ein Lock. Für eine Handvoll Benutzer völlig ausreichend.

    `admin` = (Benutzername, Passwort) legt den Admin an, wenn es noch keine Benutzer gibt
    (frische Datenbank oder Umbau auf Schema 6); `admin_created` sagt, ob das passiert ist.
    """

    def __init__(self, path: Path, admin: tuple[str, str] | None = None):
        self.path = path
        self.admin_created = False
        # Aufbewahrung in Tagen (settings.retention_days, setzt die App): Ältere Artikel, die nur
        # noch wegen eines Favoriten/„Später lesen“ existieren, sieht nur, wer sie gemerkt hat.
        self.retention_days: int | None = None
        # Zeitzone für den Tagesbeginn der Ungelesen-Zähler (settings.tz, setzt die App).
        self.tz: tzinfo = ZoneInfo(DEFAULT_TZ)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        # Zeitlimit für Leseabfragen (siehe _time_limit): Der Handler bricht ab, sobald die Frist
        # der laufenden Abfrage verstrichen ist; ohne Frist (tx, Migration) nie.
        self._deadline: float | None = None
        self._timed_out = False
        self._conn.set_progress_handler(self._progress, PROGRESS_STEPS)
        try:
            with self._lock:
                self._conn.executescript(SCHEMA)  # executescript verwaltet die Transaktion selbst
                self._migrate(admin)
                if admin and not self.one("SELECT 1 FROM users LIMIT 1"):
                    with self.tx() as c:
                        self._create_admin(c, admin)
        except BaseException:
            self._conn.close()
            raise

    def _create_admin(self, c: sqlite3.Connection, admin: tuple[str, str]) -> int:
        from .auth import hash_password  # hier importiert: Hashen nur, wenn wirklich nötig

        username, password = admin
        # Anzeigename: Benutzername mit großem Anfangsbuchstaben ("admin" -> "Admin").
        cur = c.execute(
            "INSERT INTO users (username, display_name, password_hash, is_admin, created_at) "
            "VALUES (?, ?, ?, 1, ?)", (username, username.capitalize(), hash_password(password), utcnow()))
        self.admin_created = True
        return cur.lastrowid

    def _migrate(self, admin: tuple[str, str] | None) -> None:
        """Bringt eine bestehende Datenbank auf SCHEMA_VERSION, alles in einer Transaktion.
        Mehrfach ausführbar."""
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
            if version < 4:
                # Neue Tabellen legt SCHEMA an; hier nur die Spalte für "Später lesen".
                # Die vorgegebenen Profile sät seed_profiles() beim Start (braucht den Katalog).
                existing = {r["name"] for r in c.execute("PRAGMA table_info(articles)")}
                if "later" not in existing:
                    c.execute("ALTER TABLE articles ADD COLUMN later INTEGER NOT NULL DEFAULT 0")
            if version < 5:
                # Platzhalter aus Feeds ("None", "null" ...) als Teaser bzw. Autor entfernen.
                c.execute("UPDATE articles SET summary='' "
                          "WHERE lower(trim(summary)) IN ('none', 'null', 'undefined')")
                c.execute("UPDATE articles SET author=NULL "
                          "WHERE lower(trim(author)) IN ('none', 'null', 'undefined', '')")
            if version < 6:
                self._migrate_v6(c, admin)
            if version < 7:
                # Schalter "oben bei den Filtern" je Profil; bestehende Profile bleiben sichtbar
                # (DEFAULT 1). Nach _migrate_v6 hat profiles die Spalte schon (USER_TABLES).
                existing = {r["name"] for r in c.execute("PRAGMA table_info(profiles)")}
                if "in_chips" not in existing:
                    c.execute("ALTER TABLE profiles ADD COLUMN in_chips INTEGER NOT NULL DEFAULT 1")
            if version < 8:
                # Geschichten (app/stories.py): story_id füllt der erste Lauf nach dem Start. Der Index
                # steht nicht in SCHEMA, weil SCHEMA vor der Migration läuft, wenn die Spalte fehlt.
                existing = {r["name"] for r in c.execute("PRAGMA table_info(articles)")}
                if "story_id" not in existing:
                    c.execute("ALTER TABLE articles ADD COLUMN story_id INTEGER")
                c.execute("CREATE INDEX IF NOT EXISTS idx_articles_story ON articles (story_id) "
                          "WHERE story_id IS NOT NULL")
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

    def _migrate_v6(self, c: sqlite3.Connection, admin: tuple[str, str] | None) -> None:
        """v5 -> v6: Benutzer. Lesestand, Profile, Lieblingszeitungen und Einstellungen gehen auf
        den Admin über; die Tabellen pro Benutzer werden neu angelegt und umkopiert.

        Einzige Stelle, die die alten Spalten articles.read/starred/later noch liest."""
        tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # Alte Tabellen ohne user_id beiseitelegen (vor v4 gibt es sie noch gar nicht).
        legacy = []
        for table in LEGACY_USER_COLUMNS:
            if table in tables and "user_id" not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(f"ALTER TABLE {table} RENAME TO {table}_v5")
                legacy.append(table)
        for stmt in _statements(USER_TABLES):
            c.execute(stmt)
        # Index über die alte Spalte read; ein Ersatz über paywalled ist zu unselektiv (der Planer
        # sortierte dann, statt idx_articles_published zu nutzen).
        c.execute("DROP INDEX IF EXISTS idx_articles_read_paywalled_source")

        has_state = c.execute(
            "SELECT 1 FROM articles WHERE read=1 OR starred=1 OR later=1 LIMIT 1").fetchone() is not None
        has_state = has_state or any(
            c.execute(f"SELECT 1 FROM {t}_v5 LIMIT 1").fetchone() for t in legacy)
        admin_row = c.execute("SELECT id FROM users WHERE is_admin=1 ORDER BY id LIMIT 1").fetchone()
        if admin_row:
            admin_id = admin_row["id"]
        elif admin:
            admin_id = self._create_admin(c, admin)
        elif has_state:
            raise MigrationError("Umbau auf Schema 6: KIOSK_PASSWORD muss gesetzt sein, der bisherige "
                                 "Lesestand geht auf den Admin (KIOSK_ADMIN_USER, Standard admin) über")
        else:
            admin_id = None  # frische oder leere Datenbank: Der Admin kommt beim Start

        if admin_id is not None:
            c.execute(
                "INSERT OR IGNORE INTO user_articles (user_id, article_id, read, starred, later, updated_at) "
                "SELECT ?, id, read, starred, later, ? FROM articles WHERE read=1 OR starred=1 OR later=1",
                (admin_id, utcnow()))
            for table in legacy:
                cols = LEGACY_USER_COLUMNS[table]
                c.execute(f"INSERT INTO {table} (user_id, {cols}) SELECT ?, {cols} FROM {table}_v5",
                          (admin_id,))
        for table in legacy:
            c.execute(f"DROP TABLE {table}_v5")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Schreibende Transaktion. BEGIN IMMEDIATE nimmt die Schreibsperre gleich am Anfang (wartet
        dabei höchstens busy_timeout): Viele Methoden lesen erst und schreiben dann (set_flag,
        mark_all_read, schedule_retry, put_favorite, create_profile, reorder, seed_profiles). Mit
        einem einfachen BEGIN scheitert der Wechsel vom Lesen zum Schreiben sofort mit "database is
        locked", wenn ein anderer Prozess (python -m app.users, Backup) inzwischen geschrieben hat;
        busy_timeout hilft dann nicht. Zurückgerollt wird bei jedem Abbruch, auch bei
        CancelledError oder KeyboardInterrupt."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                # Manche Fehler (z. B. SQLITE_FULL) beenden die Transaktion schon selbst.
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def _progress(self) -> int:
        if self._deadline is not None and _time.monotonic() > self._deadline:
            self._timed_out = True
            return 1  # SQLite bricht die Abfrage mit "interrupted" ab
        return 0

    @contextmanager
    def _time_limit(self, seconds: float | None) -> Iterator[None]:
        """Frist für genau eine Abfrage; bei Überschreitung QueryTimeout. Innerhalb einer offenen
        Transaktion (tx) und mit seconds=None gilt keine Frist. Aufrufer hält self._lock."""
        if seconds is None or self._conn.in_transaction:
            yield
            return
        self._deadline, self._timed_out = _time.monotonic() + seconds, False
        try:
            yield
        except sqlite3.OperationalError as e:
            if self._timed_out:
                raise QueryTimeout(f"Abfrage nach {seconds:g} s abgebrochen") from e
            raise
        finally:
            self._deadline = None

    def q(self, sql: str, params: tuple | dict = (), *,
          time_limit: float | None = _FROM_CONTEXT) -> list[sqlite3.Row]:
        """Leseabfrage; Frist `time_limit` Sekunden (None = keine), ohne Angabe die der laufenden
        API-Anfrage (request_time_limit)."""
        limit = request_time_limit.get() if time_limit is _FROM_CONTEXT else time_limit
        with self._lock, self._time_limit(limit):
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = (), *,
            time_limit: float | None = _FROM_CONTEXT) -> sqlite3.Row | None:
        """Wie q, nur die erste Zeile."""
        limit = request_time_limit.get() if time_limit is _FROM_CONTEXT else time_limit
        with self._lock, self._time_limit(limit):
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
                     validators: tuple[str | None, str | None] | None = None,
                     new_count: int = 0) -> None:
        """Ergebnis eines Feed-Abrufs. `validators` = (ETag, Last-Modified) einer 200-Antwort
        ersetzt die gespeicherten (None löscht den Wert); ohne Angabe (304) bleiben sie. Ein
        Fehlschlag löscht beide: Ein unbrauchbarer Validator soll eine Quelle nicht dauerhaft
        blockieren, der nächste Abruf geht dann ohne Bedingung."""
        now = utcnow()
        with self.tx() as c:
            if ok and validators is not None:
                c.execute(
                    "UPDATE source_state SET last_fetch_at=?, last_ok_at=?, last_error=NULL, "
                    "etag=?, last_modified=?, last_new=? WHERE id=?",
                    (now, now, *validators, new_count, sid),
                )
            elif ok:
                c.execute(
                    "UPDATE source_state SET last_fetch_at=?, last_ok_at=?, last_error=NULL, last_new=? "
                    "WHERE id=?", (now, now, new_count, sid),
                )
            else:
                c.execute(
                    "UPDATE source_state SET last_fetch_at=?, last_error=?, etag=NULL, last_modified=NULL "
                    "WHERE id=?", (now, (error or "unbekannter Fehler")[:500], sid),
                )

    def source_counts(self, user_id: int) -> dict[str, dict[str, int]]:
        """Je Quelle ohne Paywall-Artikel: total, unread (nur heute, der Zähler) und unread_all
        (alle ungelesenen, wie die Ungelesen-Liste und „alles gelesen“)."""
        # Aufbewahrungsgrenze wie in der Liste: Alte Artikel zählen nur für den, der sie gemerkt hat.
        where, params = _article_filter(sources=None, unread=False, starred=False, free_only=False,
                                        cutoff=self.retention_cutoff())
        cond = f"WHERE {' AND '.join(where)} " if where else ""
        rows = self.q(
            "SELECT a.source_id, SUM(a.paywalled=0) AS total, "
            "SUM(COALESCE(ua.read, 0)=0 AND a.paywalled=0 AND a.published_at >= ?) AS unread, "
            "SUM(COALESCE(ua.read, 0)=0 AND a.paywalled=0) AS unread_all "
            f"FROM articles a {COUNT_INDEX} {USER_JOIN} {cond}GROUP BY a.source_id",
            (self.today_start(), user_id, *params)
        )
        return {r["source_id"]: {"total": r["total"], "unread": r["unread"], "unread_all": r["unread_all"]}
                for r in rows}

    # ---- articles ------------------------------------------------------

    def upsert_articles(self, rows: list[dict[str, Any]], max_new: int | None = None) -> int:
        """Fügt neue Artikel ein, ignoriert bekannte. Gibt die Zahl der neuen zurück.

        Bekannt ist ein Eintrag, wenn (source_id, guid) in seen_guids steht, auch wenn der Artikel
        selbst schon gelöscht wurde. Taucht ein bekannter Eintrag wieder auf, wird sein
        first_seen aufgefrischt (höchstens einmal pro SEEN_REFRESH), damit purge ihn nicht vergisst,
        solange er im Feed steht (sonst käme ein Eintrag ohne Datum nach 2x Aufbewahrungsfrist als
        „neu“ zurück). Mit `max_new` höchstens so viele neue Artikel; die übrigen Einträge bleiben
        ungesehen und kommen beim nächsten Abruf dran.
        """
        new = 0
        refresh_before = _iso(datetime.now(timezone.utc) - SEEN_REFRESH)
        with self.tx() as c:
            for r in rows:
                if max_new is not None and new >= max_new:
                    break
                seen = c.execute(
                    "INSERT OR IGNORE INTO seen_guids (source_id, guid, first_seen) VALUES (?,?,?)",
                    (r["source_id"], r["guid"], r["fetched_at"]),
                )
                if seen.rowcount == 0:
                    c.execute("UPDATE seen_guids SET first_seen=? WHERE source_id=? AND guid=? AND first_seen < ?",
                              (r["fetched_at"], r["source_id"], r["guid"], refresh_before))
                    continue
                # Derselbe Artikel unter neuer GUID (BBC hängt eine wechselnde Nummer an: „…#1“, „…#8“):
                # nur dieses Muster (die GUID ohne „#…“ ist die Adresse oder die GUID ohne „#…“ des schon
                # gespeicherten Artikels), dazu gleiche Quelle, gleiche Adresse, gleicher Titel, weniger als
                # SAME_URL_WINDOW auseinander. Ein täglicher Eintrag unter fester Adresse („Presseschau
                # 27.9.“ nach „26.9.“) oder eine stündliche Sendung mit fester Adresse und Titel ist neu.
                base, frag, _ = r["guid"].partition("#")
                if frag:
                    when = datetime.fromisoformat(r["published_at"])
                    title = _same_title(r["title"])
                    if any(_same_title(o["title"]) == title and (base == r["url"] or o["guid"].partition("#")[0] == base)
                           for o in c.execute(
                               "SELECT guid, title FROM articles INDEXED BY idx_articles_source_counts WHERE "
                               "source_id=? AND published_at > ? AND published_at < ? AND url=?",
                               (r["source_id"], _iso(when - SAME_URL_WINDOW), _iso(when + SAME_URL_WINDOW),
                                r["url"]))):
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
                         body_text: str | None = None, replace_image: bool = False) -> None:
        """Speichert ein endgültiges Ergebnis (auch endgültige Fehler) und setzt den Retry-Zähler zurück.

        image_url füllt nur ein fehlendes Bild; mit replace_image=True ersetzt es das vorhandene
        (Feed-Vorschaubild durch das große Bild aus den Seiten-Metadaten).

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
                "image_url=CASE WHEN ? THEN ? ELSE COALESCE(image_url, ?) END, "
                "author=COALESCE(author, ?) WHERE id=?",
                (status, body_html, body_text, body_len, 1 if paywalled else 0,
                 (error or None) and error[:500], utcnow(), 1 if replace_image and image_url else 0,
                 image_url, image_url, author, article_id),
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

    def published_at_of(self, article_id: int, user_id: int | None = None) -> str | None:
        """published_at des Artikels; für einen Benutzer nur, wenn er ihn sehen darf (wie
        get_article): Ein alter Artikel, den nur ein anderer gemerkt hat, gilt als nicht vorhanden."""
        where, params = ["a.id=?"], [article_id]
        if user_id is not None:
            where, params = _article_filter(sources=None, unread=False, starred=False, free_only=False,
                                            cutoff=self.retention_cutoff())
            where.append("a.id=?")
            params.append(article_id)
        r = self.one(f"SELECT a.published_at FROM articles a {USER_JOIN} WHERE {' AND '.join(where)}",
                     (user_id, *params))
        return r["published_at"] if r else None

    def retention_cutoff(self) -> str | None:
        """Zeitpunkt (wie published_at gespeichert), vor dem Artikel nur noch für Benutzer
        sichtbar sind, die sie gemerkt haben; None = keine Aufbewahrungsgrenze gesetzt."""
        if not self.retention_days:
            return None
        return _iso(datetime.now(timezone.utc) - timedelta(days=self.retention_days))

    def today_start(self, now: datetime | None = None) -> str:
        """Beginn des heutigen Tages (00:00 in self.tz) wie published_at gespeichert: Ab hier zählen
        die Ungelesen-Zähler (Leiste, Zeitungen, Profile, Menü). Liste, „Nächster ungelesener“ und
        „alles gelesen“ nehmen weiter alle ungelesenen Artikel. `now` nur für Tests."""
        return day_start(self.tz, now)

    def list_articles(self, user_id: int, *, sources: list[str] | None,
                      query: str | None, unread: bool, starred: bool, free_only: bool,
                      before: tuple[str, int] | None, limit: int, offset: int = 0,
                      later: bool = False, terms: list[str] | None = None,
                      since: str | None = None) -> list[sqlite3.Row]:
        """Artikelliste. Ohne Suche per Keyset-Cursor `before` = (published_at, id),
        mit Suche (bm25-Sortierung) per `offset`. `terms` (Profil-Begriffe) filtern nur,
        die Sortierung bleibt chronologisch. Flags und Filter gelten für `user_id`."""
        sql, params = self.list_articles_sql(
            user_id, sources=sources, query=query, unread=unread, starred=starred, free_only=free_only,
            before=before, limit=limit, offset=offset, later=later, terms=terms, since=since)
        return self.q(sql, params)

    def list_articles_sql(self, user_id: int, *, sources: list[str] | None,
                          query: str | None, unread: bool, starred: bool, free_only: bool,
                          before: tuple[str, int] | None, limit: int, offset: int = 0,
                          later: bool = False, terms: list[str] | None = None,
                          since: str | None = None) -> tuple[str, tuple]:
        """SQL und Parameter für list_articles (getrennt, damit Tests den Plan prüfen können)."""
        where, params = _article_filter(sources=sources, unread=unread, starred=starred,
                                        free_only=free_only, later=later, terms=terms, since=since,
                                        cutoff=self.retention_cutoff())
        join = ""
        order = "a.published_at DESC, a.id DESC"
        index = ""
        if not query and not starred and not later and (sources is None or len(sources) > 1):
            index = LIST_INDEX
        if query:
            join = "JOIN articles_fts f ON f.rowid = a.id"
            where.append("articles_fts MATCH ?")
            params.append(_fts_query(query))
            order = "bm25(articles_fts), a.published_at DESC, a.id DESC"
        elif before is not None:
            where.append("(a.published_at, a.id) < (?, ?)")
            params += [before[0], before[1]]
        sql = (
            f"SELECT {ARTICLE_LIST_COLS} FROM articles a {index} {USER_JOIN} {join} "
            + (("WHERE " + " AND ".join(where)) if where else "")
            + f" ORDER BY {order} LIMIT ?"
        )
        params = [user_id, *params, limit]
        if query and offset:
            sql += " OFFSET ?"
            params.append(offset)
        return sql, tuple(params)

    def count_articles(self, user_id: int, *, cap: int, sources: list[str] | None = None, query: str | None = None,
                       unread: bool = False, starred: bool = False, free_only: bool = False,
                       later: bool = False, terms: list[str] | None = None,
                       since: str | None = None, time_limit: float | None = _FROM_CONTEXT) -> int:
        """Anzahl passender Artikel, höchstens `cap` (zählt nie mehr Zeilen als nötig). `time_limit`
        wie bei q."""
        where, params = _article_filter(sources=sources, unread=unread, starred=starred,
                                        free_only=free_only, later=later, terms=terms, since=since,
                                        cutoff=self.retention_cutoff())
        if query:
            where.append("a.id IN (SELECT rowid FROM articles_fts WHERE articles_fts MATCH ?)")
            params.append(_fts_query(query))
        sql = (f"SELECT COUNT(*) AS n FROM (SELECT 1 FROM articles a {USER_JOIN} "
               + (("WHERE " + " AND ".join(where)) if where else "") + " LIMIT ?)")
        return self.one(sql, (user_id, *params, cap), time_limit=time_limit)["n"]

    def get_article(self, article_id: int, user_id: int | None = None) -> sqlite3.Row | None:
        """Artikel mit Volltext; read/starred/later von `user_id` (ohne Benutzer alle 0). Für einen
        Benutzer None, wenn der Artikel hinter der Aufbewahrungsgrenze liegt und er ihn nicht gemerkt hat."""
        where, params = ["a.id=?"], [article_id]
        if user_id is not None:
            where, params = _article_filter(sources=None, unread=False, starred=False, free_only=False,
                                            cutoff=self.retention_cutoff())
            where.append("a.id=?")
            params.append(article_id)
        return self.one(f"SELECT {ARTICLE_DETAIL_COLS} FROM articles a {USER_JOIN} WHERE {' AND '.join(where)}",
                        (user_id, *params))

    def set_flag(self, user_id: int, article_id: int, flag: str, value: bool) -> bool:
        """Setzt ein Flag des Benutzers; False, wenn es den Artikel nicht gibt."""
        assert flag in ("read", "starred", "later")
        where, params = _article_filter(sources=None, unread=False, starred=False, free_only=False,
                                        cutoff=self.retention_cutoff())
        where.append("a.id=?")
        with self.tx() as c:
            # Nur sichtbare Artikel (siehe get_article): Wer einen alten, von anderen gemerkten
            # Artikel per ID merkt, soll ihn dadurch nicht sehen können.
            if not c.execute(f"SELECT 1 FROM articles a {USER_JOIN} WHERE {' AND '.join(where)}",
                             (user_id, *params, article_id)).fetchone():
                return False
            c.execute(
                f"INSERT INTO user_articles (user_id, article_id, {flag}, updated_at) VALUES (?, ?, ?, ?) "
                f"ON CONFLICT (user_id, article_id) DO UPDATE SET {flag}=excluded.{flag}, "
                "updated_at=excluded.updated_at",
                (user_id, article_id, 1 if value else 0, utcnow()))
            # Zeilen ohne jedes Flag sind überflüssig
            c.execute("DELETE FROM user_articles WHERE user_id=? AND article_id=? "
                      "AND read=0 AND starred=0 AND later=0", (user_id, article_id))
            return True

    def mark_all_read(self, user_id: int, sources: list[str] | None, terms: list[str] | None = None,
                      max_id: int | None = None) -> int:
        """Markiert alle Artikel der Menge als gelesen (sources None = alle, [] = keine), mit
        `max_id` nur Artikel bis zu dieser ID. Gibt die Zahl der markierten Artikel ohne Paywall
        zurück (so viele, wie die Oberfläche als ungelesen gezählt hat). Am Stück; die API nimmt
        mark_all_read_steps und gibt der Event-Loop zwischen den Blöcken Luft."""
        return sum(self.mark_all_read_steps(user_id, sources, terms, max_id))

    def mark_all_read_steps(self, user_id: int, sources: list[str] | None, terms: list[str] | None = None,
                            max_id: int | None = None) -> Iterator[int]:
        """Wie mark_all_read, in Blöcken von MARK_READ_ID_STEP Artikel-IDs mit je einer eigenen
        Transaktion; liefert je Block die Zahl der markierten Artikel ohne Paywall. Ein frischer
        Benutzer mit allem ungelesen schreibt sonst Zehntausende Zeilen in einer Transaktion (ca. 0,8 s,
        in der die Event-Loop steht).

        Die Menge steht beim Aufruf fest: höchstens bis zur damals höchsten ID (bzw. `max_id`), mit
        der Aufbewahrungsgrenze von da; später geholte Artikel bleiben ungelesen, wie am Stück."""
        where, params = _article_filter(sources=sources, unread=True, starred=False,
                                        free_only=False, terms=terms, cutoff=self.retention_cutoff())
        bounds = self.one("SELECT MIN(id) AS lo, MAX(id) AS hi FROM articles", time_limit=None)
        if bounds["lo"] is None:
            return
        hi = bounds["hi"] if max_id is None else min(max_id, bounds["hi"])
        cond = " AND ".join([*where, "a.id > ?", "a.id <= ?"])
        now = utcnow()
        # Blöcke über die Artikel-ID (Primärschlüssel): NOT INDEXED hält den Planer beim Bereich der ID,
        # statt je Block alle Artikel der Quellen über deren Index zu lesen.
        for start in range(bounds["lo"] - 1, hi, MARK_READ_ID_STEP):
            window = (start, min(start + MARK_READ_ID_STEP, hi))
            with self.tx() as c:
                visible = c.execute(f"SELECT COUNT(*) AS n FROM articles a NOT INDEXED {USER_JOIN} WHERE {cond} "
                                    "AND a.paywalled=0", (user_id, *params, *window)).fetchone()["n"]
                # WHERE ist Pflicht vor ON CONFLICT (sonst mehrdeutig für den SQLite-Parser); cond ist nie leer.
                c.execute(
                    "INSERT INTO user_articles (user_id, article_id, read, updated_at) "
                    f"SELECT ?, a.id, 1, ? FROM articles a NOT INDEXED {USER_JOIN} WHERE {cond} "
                    "ON CONFLICT (user_id, article_id) DO UPDATE SET read=1, updated_at=excluded.updated_at",
                    (user_id, now, user_id, *params, *window))
            yield visible

    def article_counts(self, user_id: int, unread_sources: list[str] | None = None, *,
                       time_limit: float | None = _FROM_CONTEXT) -> dict[str, int]:
        """Zähler für Menü und Kopfzeilen. `unread` ohne Paywall-Artikel, nur von heute und nur aus
        `unread_sources` (None = alle Quellen); `later` und `starred` zählen alle gemerkten
        Artikel (Gemerkt zeigt auch Bezahlartikel und ausgeschaltete Quellen)."""
        where, params = _article_filter(sources=unread_sources, unread=True, starred=False,
                                        free_only=True, since=self.today_start(), cutoff=self.retention_cutoff())
        unread = self.one(f"SELECT COUNT(*) AS n FROM articles a {USER_JOIN} WHERE {' AND '.join(where)}",
                          (user_id, *params), time_limit=time_limit)["n"]
        row = self.one("SELECT COALESCE(SUM(ua.later=1), 0) AS later, COALESCE(SUM(ua.starred=1), 0) AS starred "
                       "FROM user_articles ua WHERE ua.user_id=?", (user_id,), time_limit=time_limit)
        return {"unread": unread, **dict(row)}

    def max_article_id(self) -> int:
        """Höchste vergebene Artikel-ID (Wasserstand für "alles gelesen"), 0 ohne Artikel."""
        return self.one("SELECT COALESCE(MAX(id), 0) AS n FROM articles")["n"]

    def unread_free_by_source(self, user_id: int) -> dict[str, int]:
        """Ungelesene Artikel ohne Paywall je Quelle, nur von heute (Zähler der Leiste)."""
        where, params = _article_filter(sources=None, unread=True, starred=False, free_only=True,
                                        since=self.today_start(), cutoff=self.retention_cutoff())
        rows = self.q(f"SELECT a.source_id, COUNT(*) AS n FROM articles a {COUNT_INDEX} {USER_JOIN} "
                      f"WHERE {' AND '.join(where)} GROUP BY a.source_id", (user_id, *params))
        return {r["source_id"]: r["n"] for r in rows}

    def purge(self, retention_days: int) -> int:
        """Löscht alte Artikel am Stück (siehe purge_steps); gibt die Zahl der gelöschten zurück."""
        return sum(self.purge_steps(retention_days))

    def purge_steps(self, retention_days: int) -> Iterator[int]:
        """Löscht alte Artikel. Was irgendein Benutzer gemerkt hat (Favorit oder „Später lesen“),
        bleibt; der Lesestand gelöschter Artikel verschwindet per Cascade.
        seen_guids bleiben doppelt so lange erhalten.

        In Blöcken zu PURGE_BATCH Artikeln, je Block eine Transaktion; liefert je Block die Zahl der
        gelöschten Artikel (für seen_guids 0). Jede gelöschte Zeile kostet den FTS-Trigger und die
        Cascade nach user_articles: 26 000 Artikel in einer Transaktion hielten die Event-Loop über
        7 s an. Der Sammler gibt zwischen den Blöcken ab (Fetcher._purge)."""
        now = datetime.now(timezone.utc)
        cutoff = _iso(now - timedelta(days=retention_days))
        seen_cutoff = _iso(now - timedelta(days=2 * retention_days))
        while True:
            with self.tx() as c:
                n = c.execute(
                    "DELETE FROM articles WHERE id IN (SELECT a.id FROM articles a WHERE a.published_at < ? "
                    "AND NOT EXISTS (SELECT 1 FROM user_articles ua WHERE ua.article_id = a.id "
                    "AND (ua.starred=1 OR ua.later=1)) LIMIT ?)", (cutoff, PURGE_BATCH)).rowcount
            yield n
            if n < PURGE_BATCH:
                break
        while True:
            # Ohne Trigger und Cascade, also billig: größere Blöcke.
            with self.tx() as c:
                n = c.execute(
                    "DELETE FROM seen_guids WHERE (source_id, guid) IN (SELECT source_id, guid FROM seen_guids "
                    "WHERE first_seen < ? LIMIT ?)", (seen_cutoff, 10 * PURGE_BATCH)).rowcount
            yield 0
            if n < 10 * PURGE_BATCH:
                break

    def stats(self, user_id: int | None = None) -> dict[str, Any]:
        """Kennzahlen der Datenbank; unread und starred gelten für `user_id`. unread zählt hier alle
        ungelesenen Artikel, nicht nur die von heute wie die Zähler der Oberfläche."""
        row = self.one(
            "SELECT COUNT(*) AS total, SUM(COALESCE(ua.read, 0)=0 AND a.paywalled=0) AS unread, "
            "SUM(COALESCE(ua.starred, 0)=1) AS starred, "
            "SUM(a.text_status='pending') AS pending, SUM(a.text_status='full') AS full, "
            "SUM(a.text_status='partial') AS partial, SUM(a.text_status='failed') AS failed, "
            "SUM(a.paywalled=1) AS paywalled, COALESCE(SUM(a.body_len),0) AS body_bytes, "
            f"MIN(a.published_at) AS oldest, MAX(a.published_at) AS newest FROM articles a {USER_JOIN}",
            (user_id,), time_limit=None,  # nur für den Admin; liest bewusst die ganze Tabelle
        )
        size = 0
        for suffix in ("", "-wal"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                size += p.stat().st_size
        return {**dict(row), "db_bytes": size}

    # ---- Geschichten (app/stories.py) -------------------------------------

    def story_window(self, since: str, body_chars: int, body_if_summary_below: int) -> list[sqlite3.Row]:
        """Alle Artikel ab `since` für die Gruppierung: Titel, Anreißer, bisherige story_id und nur bei
        kurzem Anreißer der Anfang des Volltexts (sonst NULL: die Texte bleiben ungelesen).
        Hintergrundarbeit, ohne Zeitlimit."""
        return self.q("SELECT id, source_id, url, title, summary, CASE WHEN length(trim(summary, ?)) < ? "
                      "THEN substr(body_text, 1, ?) END AS body_text, published_at, story_id "
                      "FROM articles WHERE published_at >= ?",
                      (SUMMARY_TRIM, body_if_summary_below, body_chars + 200, since), time_limit=None)

    def short_summaries_with_body(self, ids: list[int], below: int) -> int:
        """Wie viele dieser Artikel haben einen kurzen Anreißer (unter `below` Zeichen) und schon
        Volltext? (Deren Geschichte hängt am Volltext, siehe stories._fields.)"""
        n = 0
        for k in range(0, len(ids), 500):
            chunk = ids[k:k + 500]
            n += self.one(f"SELECT COUNT(*) AS n FROM articles WHERE id IN ({','.join('?' * len(chunk))}) "
                          "AND length(trim(summary, ?)) < ? AND length(body_text) > 0",
                          (*chunk, SUMMARY_TRIM, below), time_limit=None)["n"]
        return n

    def set_story_ids(self, changes: list[tuple[int | None, int]]) -> None:
        """Schreibt (story_id, Artikel-ID) in einer Transaktion (die FTS-Trigger feuern dabei nicht)."""
        if not changes:
            return
        with self.tx() as c:
            c.executemany("UPDATE articles SET story_id=? WHERE id=?", changes)

    def story_id_of(self, article_id: int) -> int | None:
        r = self.one("SELECT story_id FROM articles WHERE id=?", (article_id,))
        return r["story_id"] if r else None

    def story_member_ids(self, story_id: int, time_limit: float | None = _FROM_CONTEXT) -> list[int]:
        """Alle Artikel einer Geschichte (ohne Filter; zum Ausschließen bei verwandten Themen)."""
        return [r["id"] for r in self.q("SELECT id FROM articles WHERE story_id=?", (story_id,),
                                        time_limit=time_limit)]

    def topic_candidates(self, query: str, since: str, limit: int,
                         time_limit: float | None = _FROM_CONTEXT) -> list[sqlite3.Row]:
        """Kandidaten für verwandte Themen aus dem Volltextindex (FTS5-Abfrage `query`, schon sicher
        gebaut), ab `since`, nach bm25."""
        return self.q(
            "SELECT a.id, a.source_id, a.title, a.summary, a.published_at, a.story_id FROM articles_fts f "
            "JOIN articles a ON a.id = f.rowid WHERE articles_fts MATCH ? AND a.published_at >= ? "
            "ORDER BY bm25(articles_fts) LIMIT ?", (query, since, limit), time_limit=time_limit)

    def story_articles(self, user_id: int, story_ids: list[int], *, sources: list[str] | None,
                       since: str | None = None, until: str | None = None,
                       time_limit: float | None = _FROM_CONTEXT) -> list[sqlite3.Row]:
        """Artikel der Geschichten (Listenspalten plus story_id) ohne Paywall, im Quellenbereich und
        innerhalb der Aufbewahrungsgrenze wie die Liste; mit `since`/`until` nur in diesem Zeitraum."""
        if not story_ids:
            return []
        where, params = _article_filter(sources=sources, unread=False, starred=False, free_only=True,
                                        since=since, cutoff=self.retention_cutoff())
        if until:
            where.append("a.published_at <= ?")
            params.append(until)
        where.append(f"a.story_id IN ({','.join('?' * len(story_ids))})")
        # Über den Index der Geschichten (wenige Zeilen je Geschichte), nicht über Quelle und Zeit: Der
        # Planer nähme sonst gern idx_articles_source_counts und läse das ganze Fenster.
        return self.q(f"SELECT {ARTICLE_LIST_COLS}, a.story_id FROM articles a INDEXED BY idx_articles_story "
                      f"{USER_JOIN} WHERE {' AND '.join(where)}", (user_id, *params, *story_ids),
                      time_limit=time_limit)

    def articles_by_ids(self, user_id: int, ids: list[int], *, sources: list[str] | None,
                        time_limit: float | None = _FROM_CONTEXT) -> list[sqlite3.Row]:
        """Artikel (Listenspalten plus story_id) mit diesen IDs, gefiltert wie die Liste (Quellen,
        ohne Paywall, Aufbewahrungsgrenze); Reihenfolge beliebig."""
        if not ids:
            return []
        where, params = _article_filter(sources=sources, unread=False, starred=False, free_only=True,
                                        cutoff=self.retention_cutoff())
        where.append(f"a.id IN ({','.join('?' * len(ids))})")
        return self.q(f"SELECT {ARTICLE_LIST_COLS}, a.story_id FROM articles a {USER_JOIN} "
                      f"WHERE {' AND '.join(where)}", (user_id, *params, *ids), time_limit=time_limit)

    def top_stories(self, *, sources: list[str], since: str, min_sources: int, limit: int,
                    publishers: dict[str, str] | None = None,
                    time_limit: float | None = _FROM_CONTEXT) -> list[sqlite3.Row]:
        """Geschichten mit Artikeln ohne Paywall ab `since` aus `sources`: story_id, Zahl der Zeitungen
        (n; Quellen, die `publishers` einem Verlag zuordnet, zählen als einer) und neuester Artikel,
        meiste Zeitungen zuerst. Eine Abfrage für alle Geschichten."""
        if not sources:
            return []
        marks = ",".join("?" * len(sources))
        cte, join, paper, pparams = _publisher_sql(publishers)
        return self.q(
            f"{cte}SELECT a.story_id, COUNT(DISTINCT {paper}) AS n, MAX(a.published_at) AS newest "
            f"FROM articles a {join}"
            f"WHERE a.story_id IS NOT NULL AND a.published_at >= ? AND a.paywalled=0 AND a.source_id IN ({marks}) "
            "GROUP BY a.story_id HAVING n >= ? ORDER BY n DESC, newest DESC LIMIT ?",
            (*pparams, since, *sources, min_sources, limit), time_limit=time_limit)

    def published_since(self, *, sources: list[str], since: str,
                        time_limit: float | None = _FROM_CONTEXT) -> list[sqlite3.Row]:
        """(published_at, source_id) aller Artikel ohne Paywall ab `since` aus `sources`, nur aus dem
        Index. Ohne Aufbewahrungsgrenze: `since` liegt bei den Aufrufern (heute) immer dahinter."""
        if not sources:
            return []
        return self.q(f"SELECT a.published_at, a.source_id FROM articles a {COUNT_INDEX} "
                      f"WHERE a.source_id IN ({','.join('?' * len(sources))}) AND a.published_at >= ? "
                      "AND a.paywalled=0", (*sources, since), time_limit=time_limit)

    def profile_story_groups(self, user_id: int, *, sources: list[str] | None, terms: list[str] | None,
                             since: str, limit: int, publishers: dict[str, str] | None = None,
                             time_limit: float | None = _FROM_CONTEXT) -> list[sqlite3.Row]:
        """Geschichten (story_id, sonst -Artikel-ID als Einzelartikel) eines Profils seit `since`, je
        Gruppe die Zahl der Zeitungen im Profil (siehe top_stories) und der neueste Artikel; meiste
        zuerst. Gruppiert in SQL über den ganzen Zeitraum, nicht nur über die neuesten Artikel."""
        where, params = _article_filter(sources=sources, unread=False, starred=False, free_only=True,
                                        terms=terms, since=since, cutoff=self.retention_cutoff())
        cte, join, paper, pparams = _publisher_sql(publishers)
        return self.q(f"{cte}SELECT COALESCE(a.story_id, -a.id) AS k, COUNT(DISTINCT {paper}) AS n, "
                      f"MAX(a.published_at) AS newest FROM articles a {USER_JOIN} {join}"
                      f"WHERE {' AND '.join(where)} "
                      "GROUP BY k ORDER BY n DESC, newest DESC LIMIT ?", (*pparams, user_id, *params, limit),
                      time_limit=time_limit)

    def profile_group_articles(self, user_id: int, keys: list[int], *, sources: list[str] | None,
                               terms: list[str] | None, since: str,
                               time_limit: float | None = _FROM_CONTEXT) -> list[sqlite3.Row]:
        """Artikel der Gruppen aus profile_story_groups (Listenspalten plus story_id), gefiltert wie
        das Profil."""
        story_ids = [k for k in keys if k > 0]
        ids = [-k for k in keys if k < 0]
        if not story_ids and not ids:
            return []
        where, params = _article_filter(sources=sources, unread=False, starred=False, free_only=True,
                                        terms=terms, since=since, cutoff=self.retention_cutoff())
        where.append(f"(a.story_id IN ({','.join('?' * len(story_ids)) or 'NULL'}) "
                     f"OR a.id IN ({','.join('?' * len(ids)) or 'NULL'}))")
        return self.q(f"SELECT {ARTICLE_LIST_COLS}, a.story_id FROM articles a {USER_JOIN} "
                      f"WHERE {' AND '.join(where)}", (user_id, *params, *story_ids, *ids), time_limit=time_limit)

    def clear_story_ids(self, story_ids: list[int], before: str) -> int:
        """Löst Geschichten auf, die es nicht mehr gibt: story_id NULL für Artikel vor `before` (außerhalb
        des Fensters), die noch eine dieser IDs tragen."""
        if not story_ids:
            return 0
        with self.tx() as c:
            return c.execute(f"UPDATE articles SET story_id=NULL WHERE published_at < ? AND story_id IN "
                             f"({','.join('?' * len(story_ids))})", (before, *story_ids)).rowcount

    def story_oldest_before(self, story_ids: list[int], before: str) -> dict[int, str]:
        """Je Geschichte die Zeit ihres ältesten Artikels vor `before` (nur Geschichten, die solche haben)."""
        out: dict[int, str] = {}
        for i in range(0, len(story_ids), 500):
            chunk = story_ids[i:i + 500]
            out.update((r["story_id"], r["at"]) for r in self.q(
                f"SELECT story_id, MIN(published_at) AS at FROM articles INDEXED BY idx_articles_story "
                f"WHERE story_id IN ({','.join('?' * len(chunk))}) AND published_at < ? GROUP BY story_id",
                (*chunk, before), time_limit=None))
        return out

    def story_sources_before(self, story_ids: list[int], before: str) -> dict[int, set[str]]:
        """Je Geschichte die Quellen ihrer Artikel vor `before`."""
        out: dict[int, set[str]] = {}
        for i in range(0, len(story_ids), 500):
            chunk = story_ids[i:i + 500]
            for r in self.q(f"SELECT DISTINCT story_id, source_id FROM articles INDEXED BY idx_articles_story "
                            f"WHERE story_id IN ({','.join('?' * len(chunk))}) AND published_at < ?",
                            (*chunk, before), time_limit=None):
                out.setdefault(r["story_id"], set()).add(r["source_id"])
        return out

    def outside_story_ids(self, ids: list[int], before: str) -> set[int]:
        """Welche dieser Artikel-IDs tragen Artikel vor `before` noch als story_id (Geschichten, die
        aus dem Fenster gewandert sind)? Schützt vor der Wiederverwendung einer ID für etwas anderes."""
        out: set[int] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            out.update(r["story_id"] for r in self.q(
                f"SELECT DISTINCT story_id FROM articles WHERE published_at < ? AND story_id IN "
                f"({','.join('?' * len(chunk))})", (before, *chunk), time_limit=None))
        return out

    # ---- Profile ---------------------------------------------------------

    def seed_profiles(self, user_id: int, de_sources: list[str], business_sources: list[str] | None) -> bool:
        """Legt die vier vorgegebenen Profile des Benutzers an, wenn er noch keine hat.
        True, wenn gesät wurde.

        `business_sources` None heißt: keine Wirtschaftsquelle aktiv, dann alle Quellen plus Begriffe.
        """
        now = utcnow()
        seeds = (
            ("Deutschland", "de", de_sources, [], 1),
            ("Weltweit", "world", None, [], 0),
            ("Wirtschaft", "business", business_sources, BUSINESS_TERMS, 1),
            ("Militär", "military", None, MILITARY_TERMS, 0),
        )
        with self.tx() as c:
            if c.execute("SELECT 1 FROM profiles WHERE user_id=? LIMIT 1", (user_id,)).fetchone():
                return False
            for pos, (name, builtin, sources, terms, in_bar) in enumerate(seeds):
                c.execute(
                    "INSERT INTO profiles (user_id, name, builtin, sources_json, terms_json, in_bar, position, "
                    "created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (user_id, name, builtin, None if sources is None else json.dumps(sources),
                     json.dumps(terms, ensure_ascii=False), in_bar, pos, now),
                )
        return True

    def list_profiles(self, user_id: int) -> list[dict[str, Any]]:
        return [_profile(r) for r in self.q("SELECT * FROM profiles WHERE user_id=? ORDER BY position, id",
                                            (user_id,))]

    def get_profile(self, user_id: int, profile_id: int) -> dict[str, Any] | None:
        """Profil des Benutzers; Profile anderer Benutzer gibt es für ihn nicht (None)."""
        r = self.one("SELECT * FROM profiles WHERE id=? AND user_id=?", (profile_id, user_id))
        return _profile(r) if r else None

    def create_profile(self, user_id: int, *, name: str, sources: list[str] | None, terms: list[str],
                       in_bar: bool, in_chips: bool = True) -> int:
        with self.tx() as c:
            pos = c.execute("SELECT COALESCE(MAX(position) + 1, 0) AS p FROM profiles WHERE user_id=?",
                            (user_id,)).fetchone()["p"]
            cur = c.execute(
                "INSERT INTO profiles (user_id, name, sources_json, terms_json, in_bar, in_chips, position, "
                "created_at) VALUES (?,?,?,?,?,?,?,?)",
                (user_id, name, None if sources is None else json.dumps(sources),
                 json.dumps(terms, ensure_ascii=False), 1 if in_bar else 0, 1 if in_chips else 0, pos, utcnow()),
            )
            return cur.lastrowid

    def update_profile(self, user_id: int, profile_id: int, fields: dict[str, Any]) -> None:
        """Ändert nur die übergebenen Felder (name, sources, terms, in_bar, in_chips)."""
        cols = {"name": ("name", lambda v: v),
                "sources": ("sources_json", lambda v: None if v is None else json.dumps(v)),
                "terms": ("terms_json", lambda v: json.dumps(v, ensure_ascii=False)),
                "in_bar": ("in_bar", lambda v: 1 if v else 0),
                "in_chips": ("in_chips", lambda v: 1 if v else 0)}
        sets = [(cols[k][0], cols[k][1](v)) for k, v in fields.items() if k in cols]
        if not sets:
            return
        with self.tx() as c:
            c.execute(f"UPDATE profiles SET {', '.join(f'{col}=?' for col, _ in sets)} "
                      "WHERE id=? AND user_id=?", (*[v for _, v in sets], profile_id, user_id))

    def delete_profile(self, user_id: int, profile_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM profiles WHERE id=? AND user_id=?", (profile_id, user_id))

    def reorder(self, user_id: int, table: str, key: str, ids: list) -> None:
        """Setzt position nach der Reihenfolge in `ids`; nicht genannte Einträge des Benutzers
        folgen dahinter in ihrer bisherigen Reihenfolge."""
        assert (table, key) in (("profiles", "id"), ("favorite_sources", "source_id"))
        with self.tx() as c:
            rest = [r[key] for r in c.execute(
                f"SELECT {key} FROM {table} WHERE user_id=? ORDER BY position, {key}", (user_id,))
                if r[key] not in ids]
            for pos, k in enumerate([*ids, *rest]):
                c.execute(f"UPDATE {table} SET position=? WHERE {key}=? AND user_id=?", (pos, k, user_id))

    # ---- Lieblingszeitungen ---------------------------------------------

    def list_favorites(self, user_id: int) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM favorite_sources WHERE user_id=? ORDER BY position, created_at, source_id",
                      (user_id,))

    def put_favorite(self, user_id: int, source_id: str, in_bar: bool | None) -> None:
        """Legt eine Lieblingszeitung an (hinten, standardmäßig in der Leiste) oder ändert in_bar."""
        with self.tx() as c:
            if c.execute("SELECT 1 FROM favorite_sources WHERE user_id=? AND source_id=?",
                         (user_id, source_id)).fetchone():
                if in_bar is not None:
                    c.execute("UPDATE favorite_sources SET in_bar=? WHERE user_id=? AND source_id=?",
                              (1 if in_bar else 0, user_id, source_id))
                return
            pos = c.execute("SELECT COALESCE(MAX(position) + 1, 0) AS p FROM favorite_sources WHERE user_id=?",
                            (user_id,)).fetchone()["p"]
            c.execute("INSERT INTO favorite_sources (user_id, source_id, in_bar, position, created_at) "
                      "VALUES (?,?,?,?,?)", (user_id, source_id, 0 if in_bar is False else 1, pos, utcnow()))

    def delete_favorite(self, user_id: int, source_id: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM favorite_sources WHERE user_id=? AND source_id=?", (user_id, source_id))

    # ---- Einstellungen --------------------------------------------------

    def get_settings(self, user_id: int) -> dict[str, Any]:
        """Gespeicherte Einstellungen des Benutzers (nur gesetzte Schlüssel), Werte aus JSON."""
        out = {}
        for r in self.q("SELECT key, value FROM settings WHERE user_id=?", (user_id,)):
            try:
                out[r["key"]] = json.loads(r["value"])
            except ValueError:
                continue  # kaputter Wert: Standard gilt
        return out

    def set_settings(self, user_id: int, values: dict[str, Any]) -> None:
        with self.tx() as c:
            for k, v in values.items():
                c.execute("INSERT OR REPLACE INTO settings (user_id, key, value) VALUES (?, ?, ?)",
                          (user_id, k, json.dumps(v, ensure_ascii=False)))

    def get_meta(self, key: str) -> str | None:
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # ---- Benutzer --------------------------------------------------------

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        r = self.one("SELECT * FROM users WHERE id=?", (user_id,))
        return dict(r) if r else None

    def get_user_by_name(self, username: str) -> dict[str, Any] | None:
        r = self.one("SELECT * FROM users WHERE username=?", (username,))  # COLLATE NOCASE
        return dict(r) if r else None

    def list_users(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.q("SELECT * FROM users ORDER BY id")]

    def user_count(self) -> int:
        return self.one("SELECT COUNT(*) AS n FROM users")["n"]

    def create_user(self, username: str, password_hash: str, *, display_name: str | None = None,
                    is_admin: bool = False) -> int:
        """Legt einen Benutzer an (Name schon geprüft und klein geschrieben); sqlite3.IntegrityError,
        wenn es den Namen schon gibt."""
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO users (username, display_name, password_hash, is_admin, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (username, display_name or username, password_hash, 1 if is_admin else 0, utcnow()))
            return cur.lastrowid

    def update_user(self, user_id: int, *, end_sessions: bool = False, **fields: Any) -> None:
        """Ändert password_hash, display_name, is_admin, disabled; `end_sessions` erhöht die
        Session-Epoche und beendet damit alle Sitzungen des Benutzers."""
        allowed = {"password_hash", "display_name", "is_admin", "disabled", "last_login_at"}
        assert set(fields) <= allowed, fields
        sets = [f"{k}=?" for k in fields] + (["session_epoch=session_epoch+1"] if end_sessions else [])
        if not sets:
            return
        with self.tx() as c:
            c.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", (*fields.values(), user_id))

    def end_sessions(self, user_id: int) -> None:
        """Beendet alle Sitzungen des Benutzers (Abmelden)."""
        self.update_user(user_id, end_sessions=True)

    def delete_user(self, user_id: int) -> None:
        """Löscht den Benutzer samt Lesestand, Profilen, Favoriten und Einstellungen (Cascade)."""
        with self.tx() as c:
            c.execute("DELETE FROM users WHERE id=?", (user_id,))

    def active_admin_count(self) -> int:
        return self.one("SELECT COUNT(*) AS n FROM users WHERE is_admin=1 AND disabled=0")["n"]

    def users_without_profiles(self) -> list[int]:
        return [r["id"] for r in self.q(
            "SELECT id FROM users u WHERE NOT EXISTS (SELECT 1 FROM profiles p WHERE p.user_id = u.id)")]

    def ping(self) -> bool:
        """True, wenn die Datenbank antwortet."""
        try:
            return self.one("SELECT 1 AS ok")["ok"] == 1
        except sqlite3.Error:
            return False


BUSINESS_TERMS = ["Wirtschaft", "Konjunktur", "Inflation", "Zinsen", "Börse",
                  "economy", "inflation", "interest rates", "markets"]
MILITARY_TERMS = ["Bundeswehr", "NATO", "Rüstung", "Militär", "Verteidigung",
                  "military", "defence", "defense", "army"]


def _statements(script: str) -> list[str]:
    """Zerlegt ein DDL-Skript (ohne Trigger) in einzelne Anweisungen für die Migration, die in
    einer Transaktion läuft (executescript würde sie beenden)."""
    lines = [line for line in script.splitlines() if not line.lstrip().startswith("--")]
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


def _profile(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"], "name": r["name"], "builtin": r["builtin"],
        "sources": None if r["sources_json"] is None else json.loads(r["sources_json"]),
        "terms": json.loads(r["terms_json"] or "[]"),
        "in_bar": bool(r["in_bar"]), "in_chips": bool(r["in_chips"]), "position": r["position"],
    }


def _article_filter(*, sources: list[str] | None, unread: bool, starred: bool, free_only: bool,
                    later: bool = False, terms: list[str] | None = None,
                    since: str | None = None, cutoff: str | None = None) -> tuple[list[str], list[Any]]:
    """Gemeinsame WHERE-Bedingungen (Alias a, dazu ua aus USER_JOIN) für Liste, Zählung und
    "alles gelesen".

    sources None = alle Quellen, leere Liste = keine. terms: Profil-Begriffe, ODER-verknüpft,
    als Präfix über den Volltextindex. cutoff: Aufbewahrungsgrenze; ältere Artikel (die purge nur
    behält, weil jemand sie gemerkt hat) sieht nur, wer sie selbst als Favorit/„Später lesen“ hat.
    """
    where: list[str] = []
    params: list[Any] = []
    if cutoff:
        where.append("(a.published_at >= ? OR ua.starred=1 OR ua.later=1)")
        params.append(cutoff)
    if sources is not None:
        if sources:
            where.append(f"a.source_id IN ({','.join('?' * len(sources))})")
            params += sources
        else:
            where.append("0")
    # Flags aus user_articles (Alias ua, per LEFT JOIN des Aufrufers); keine Zeile heißt ungelesen.
    if unread:
        where.append("COALESCE(ua.read, 0)=0")
    if starred:
        where.append("ua.starred=1")
    if later:
        where.append("ua.later=1")
    if free_only:
        where.append("a.paywalled=0")
    if since:
        where.append("a.published_at >= ?")
        params.append(since)
    if terms:
        where.append("a.id IN (SELECT rowid FROM articles_fts WHERE articles_fts MATCH ?)")
        params.append(_terms_query(terms))
    return where, params


def _same_title(title: str | None) -> str:
    """Titel zum Vergleich: klein, Leerraum zusammengefasst."""
    return " ".join((title or "").casefold().split())


def _publisher_sql(publishers: dict[str, str] | None) -> tuple[str, str, str, tuple]:
    """Bausteine, um Zeitungen statt Quellen zu zählen: (WITH-Klausel, JOIN, Ausdruck, Parameter).
    `publishers`: Quelle -> Verlag, nur für Quellen, deren Verlag anders heißt (The Local, heise)."""
    pairs = sorted((s, p) for s, p in (publishers or {}).items() if s != p)
    if not pairs:
        return "", "", "a.source_id", ()
    values = ",".join("(?, ?)" for _ in pairs)
    return (f"WITH pubs(src, pub) AS (VALUES {values}) ", "LEFT JOIN pubs ON pubs.src = a.source_id ",
            "COALESCE(pubs.pub, a.source_id)", tuple(x for pair in pairs for x in pair))


def _phrase(text: str) -> str:
    """Text als FTS5-Phrase (Anführungszeichen verdoppelt); mit Präfix-Stern nur, wenn das letzte
    Wort mindestens PREFIX_MIN_LEN Zeichen hat, kürzere Wörter zählen exakt."""
    tokens = _TOKEN_RE.findall(text)
    star = "*" if tokens and len(tokens[-1]) >= PREFIX_MIN_LEN else ""
    return '"' + text.replace('"', '""') + '"' + star


def _terms_query(terms: list[str]) -> str:
    """Profil-Begriffe als FTS5-Abfrage: jeder Begriff als Phrase (mit Präfix, siehe _phrase),
    ODER-verknüpft."""
    return " OR ".join(_phrase(t) for t in terms)


def _has_text(c: sqlite3.Connection, article_id: int) -> bool:
    row = c.execute(
        "SELECT body_text IS NOT NULL AND body_text != '' OR body_html IS NOT NULL AND body_html != '' AS t "
        "FROM articles WHERE id=?", (article_id,),
    ).fetchone()
    return bool(row and row["t"])


def _fts_query(raw: str) -> str:
    """Macht aus freier Eingabe eine sichere FTS5-Abfrage: jedes Wort als Phrase, UND-verknüpft,
    höchstens SEARCH_MAX_WORDS Wörter; Präfix-Suche nur ab PREFIX_MIN_LEN Zeichen (siehe _phrase).

    Jedes Wort wird als FTS5-String in doppelte Anführungszeichen gesetzt; eingebettete
    Anführungszeichen werden dabei verdoppelt.
    """
    words = [w.strip('"\'*()^:-') for w in raw.split()]
    # Nur Stücke mit Wortzeichen: "–", "&", "/" oder "+" allein ergäben eine leere Phrase, und die
    # findet nichts ("Merz – Macron" hätte keinen Treffer). Die Obergrenze gilt nach dem Filtern.
    words = [w for w in words if _TOKEN_RE.search(w)][:SEARCH_MAX_WORDS]
    if not words:
        return '""'
    return " AND ".join(_phrase(w) for w in words)
