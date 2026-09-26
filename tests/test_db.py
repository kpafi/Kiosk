from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.db import SCHEMA_VERSION, Database, _fts_query
from conftest import ago, make_row

OLD_SCHEMA = """
CREATE TABLE source_state (
    id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, etag TEXT, last_modified TEXT,
    last_fetch_at TEXT, last_ok_at TEXT, last_error TEXT, last_new INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, guid TEXT NOT NULL, url TEXT NOT NULL,
    title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', author TEXT, image_url TEXT,
    published_at TEXT NOT NULL, fetched_at TEXT NOT NULL, body_html TEXT,
    body_len INTEGER NOT NULL DEFAULT 0, text_status TEXT NOT NULL DEFAULT 'pending',
    text_error TEXT, extracted_at TEXT, paywalled INTEGER NOT NULL DEFAULT 0,
    read INTEGER NOT NULL DEFAULT 0, starred INTEGER NOT NULL DEFAULT 0, UNIQUE (source_id, guid)
);
CREATE INDEX idx_articles_published ON articles (published_at DESC, id DESC);
CREATE VIRTUAL TABLE articles_fts USING fts5(
    title, summary, body, content='articles', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER articles_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(rowid, title, summary, body) VALUES (new.id, new.title, new.summary, '');
END;
CREATE TRIGGER articles_ad AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, summary, body) VALUES ('delete', old.id, old.title, old.summary, old.body_html);
END;
CREATE TRIGGER articles_au AFTER UPDATE OF title, summary, body_html ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, summary, body) VALUES ('delete', old.id, old.title, old.summary, old.body_html);
    INSERT INTO articles_fts(rowid, title, summary, body) VALUES (new.id, new.title, new.summary, new.body_html);
END;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def list_all(db, **kw):
    params = dict(sources=None, query=None, unread=False, starred=False,
                  free_only=False, before=None, limit=50)
    params.update(kw)
    return db.list_articles(**params)


def fts_integrity(db) -> None:
    # Wirft sqlite3.DatabaseError, wenn Index und Inhaltstabelle auseinanderlaufen.
    db.q("INSERT INTO articles_fts(articles_fts, rank) VALUES('integrity-check', 1)")


def store_full(db, article_id: int, text: str, html: str | None = None) -> None:
    db.store_extraction(article_id, status="full", body_html=html or f"<p>{text}</p>", body_text=text,
                        body_len=len(text), paywalled=False)


# ---- Blättern --------------------------------------------------------------

def test_keyset_pagination_with_mixed_dates_and_ids(db):
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    # IDs steigen, Datum nicht: dazwischen gleiche Zeitstempel, damit id als Tiebreaker zählt.
    offsets_h = [5, 1, 9, 1, 3, 1, 7, 0, 5, 2]
    rows = [make_row("alpha", i, (base + timedelta(hours=h)).isoformat()) for i, h in enumerate(offsets_h)]
    db.upsert_articles(rows)
    expected = [(r["published_at"], r["id"]) for r in db.q("SELECT id, published_at FROM articles")]
    expected.sort(reverse=True)

    seen, before = [], None
    for _ in range(20):
        page = list_all(db, before=before, limit=3)
        seen += [(r["published_at"], r["id"]) for r in page]
        if len(page) < 3:
            break
        before = (page[-1]["published_at"], page[-1]["id"])
    assert seen == expected
    assert len(set(seen)) == len(offsets_h)


def test_search_offset_pagination(db):
    db.upsert_articles([make_row("alpha", i, ago(hours=i), title=f"Klima {i}") for i in range(5)])
    first = list_all(db, query="klima", limit=2)
    second = list_all(db, query="klima", limit=2, offset=2)
    third = list_all(db, query="klima", limit=2, offset=4)
    ids = [r["id"] for r in first + second + third]
    assert len(ids) == 5 and len(set(ids)) == 5


# ---- Volltextsuche ---------------------------------------------------------

def test_fts_indexes_plain_text_not_markup(db):
    db.upsert_articles([make_row("alpha", 1, ago(hours=1))])
    aid = db.q("SELECT id FROM articles")[0]["id"]
    store_full(db, aid, "Die Bundesregierung plant Neues", '<p><strong class="x">Die Bundesregierung</strong> plant Neues</p>')
    assert [r["id"] for r in list_all(db, query="bundesreg")] == [aid]
    assert list_all(db, query="strong") == []
    assert list_all(db, query="class") == []


def test_fts_rebuild_works(db):
    db.upsert_articles([make_row("alpha", i, ago(hours=i)) for i in range(3)])
    for r in db.q("SELECT id FROM articles"):
        store_full(db, r["id"], f"Inhalt Nummer{r['id']} Wasserstoff")
    db.q("INSERT INTO articles_fts(articles_fts) VALUES('rebuild')")
    fts_integrity(db)
    assert len(list_all(db, query="wasserstoff")) == 3


def test_fts_query_escapes_quotes():
    assert _fts_query('foo"bar') == '"foo""bar"*'
    assert _fts_query("  ") == '""'
    assert _fts_query('"hallo" welt') == '"hallo"* AND "welt"*'


@pytest.mark.parametrize("q", ['foo"bar', 'a " b', 'NEAR(x y)', 'x AND OR NOT', '(((', 'title:foo', '^start', "o'neil"])
def test_hostile_search_queries_do_not_raise(db, q):
    db.upsert_articles([make_row("alpha", 1, ago(hours=1))])
    list_all(db, query=q)


def test_purge_keeps_starred_and_fts_consistent(db):
    db.upsert_articles([
        make_row("alpha", 1, ago(days=200), title="Uralt"),
        make_row("alpha", 2, ago(days=200), title="Uralt Favorit"),
        make_row("alpha", 3, ago(days=1), title="Frisch"),
    ])
    ids = {r["guid"]: r["id"] for r in db.q("SELECT id, guid FROM articles")}
    for aid in ids.values():
        store_full(db, aid, "Text zum Suchen")
    db.set_flag(ids["alpha-2"], "starred", True)
    assert db.purge(90) == 1
    left = {r["guid"] for r in db.q("SELECT guid FROM articles")}
    assert left == {"alpha-2", "alpha-3"}
    fts_integrity(db)
    assert {r["id"] for r in list_all(db, query="suchen")} == {ids["alpha-2"], ids["alpha-3"]}



def test_purge_keeps_read_later(db):
    db.upsert_articles([
        make_row("alpha", 1, ago(days=200), title="Uralt"),
        make_row("alpha", 2, ago(days=200), title="Uralt Später"),
    ])
    ids = {r["guid"]: r["id"] for r in db.q("SELECT id, guid FROM articles")}
    db.set_flag(ids["alpha-2"], "later", True)
    assert db.purge(90) == 1
    assert {r["guid"] for r in db.q("SELECT guid FROM articles")} == {"alpha-2"}

# ---- Wiederholungen --------------------------------------------------------

def test_retry_backoff_and_final_failure(db):
    db.upsert_articles([make_row("alpha", 1, ago(hours=1))])
    aid = db.q("SELECT id FROM articles")[0]["id"]
    now = datetime.now(timezone.utc)
    for hours in (1, 4, 12):
        assert db.schedule_retry(aid, "HTTP 503") == "pending"
        row = db.get_article(aid)
        due = datetime.fromisoformat(row["next_attempt_at"])
        assert timedelta(hours=hours) - timedelta(minutes=1) <= due - now <= timedelta(hours=hours, minutes=1)
        assert db.pending_articles(10) == []  # noch nicht fällig
        db.q("UPDATE articles SET next_attempt_at=? WHERE id=?", (ago(seconds=1), aid))
        assert [r["id"] for r in db.pending_articles(10)] == [aid]
    assert db.schedule_retry(aid, "HTTP 503") == "failed"
    row = db.get_article(aid)
    assert row["text_status"] == "failed" and row["next_attempt_at"] is None


def test_retry_after_is_respected_and_capped(db):
    db.upsert_articles([make_row("alpha", 1, ago(hours=1))])
    aid = db.q("SELECT id FROM articles")[0]["id"]
    now = datetime.now(timezone.utc)
    db.schedule_retry(aid, "HTTP 429", retry_after=7200)
    due = datetime.fromisoformat(db.get_article(aid)["next_attempt_at"])
    assert timedelta(hours=2) - timedelta(minutes=1) <= due - now <= timedelta(hours=2, minutes=1)
    db.schedule_retry(aid, "HTTP 429", retry_after=10 * 86400)
    due = datetime.fromisoformat(db.get_article(aid)["next_attempt_at"])
    assert due - now <= timedelta(hours=12, minutes=1)


def test_pending_only_for_enabled_sources(db):
    db.upsert_articles([make_row("alpha", 1, ago(hours=1)), make_row("gamma", 1, ago(hours=1))])
    assert {r["source_id"] for r in db.pending_articles(10)} == {"alpha"}
    db.set_source_enabled("alpha", False)
    assert db.pending_articles(10) == []


def test_store_extraction_resets_attempts(db):
    db.upsert_articles([make_row("alpha", 1, ago(hours=1))])
    aid = db.q("SELECT id FROM articles")[0]["id"]
    db.schedule_retry(aid, "timeout")
    store_full(db, aid, "fertig")
    row = db.get_article(aid)
    assert row["attempts"] == 0 and row["next_attempt_at"] is None and row["text_status"] == "full"


# ---- Migration -------------------------------------------------------------

def test_migration_from_old_schema(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO source_state (id, enabled) VALUES ('alpha', 1)")
    for i, body in enumerate([
        "<p>Die <strong>Energiewende</strong> kommt &amp; bleibt</p>",
        None,
        "<p>Zweiter <a href=\"https://x.example/\">Artikel</a> über Wasserstoff</p>",
    ]):
        conn.execute(
            "INSERT INTO articles (source_id, guid, url, title, summary, published_at, fetched_at) "
            "VALUES ('alpha', ?, ?, ?, 'kurz', ?, ?)",
            (f"g{i}", f"https://a.example/{i}", f"Titel {i}", ago(hours=i), ago(hours=0)),
        )
        if body:
            conn.execute("UPDATE articles SET body_html=?, body_len=?, text_status='full' WHERE guid=?",
                         (body, len(body), f"g{i}"))
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        cols = {r["name"] for r in db.q("PRAGMA table_info(articles)")}
        assert {"body_text", "attempts", "next_attempt_at"} <= cols
        assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
        texts = {r["guid"]: r["body_text"] for r in db.q("SELECT guid, body_text FROM articles")}
        assert texts["g0"] == "Die Energiewende kommt & bleibt"
        assert texts["g1"] is None
        assert texts["g2"] == "Zweiter Artikel über Wasserstoff"
        assert db.one("SELECT COUNT(*) AS n FROM articles")["n"] == 3
        assert db.one("SELECT COUNT(*) AS n FROM seen_guids")["n"] == 3
        fts_integrity(db)
        assert [r["url"] for r in list_all(db, query="energiewende")] == ["https://a.example/0"]
        assert list_all(db, query="strong") == []
        # Neue Trigger funktionieren: Einfügen, Ändern, Löschen hält den Index konsistent.
        db.upsert_articles([make_row("alpha", 9, ago(hours=1), title="Neuer Beitrag")])
        new_id = db.one("SELECT id FROM articles WHERE guid='alpha-9'")["id"]
        store_full(db, new_id, "Solarstrom im Herbst")
        db.q("DELETE FROM articles WHERE guid='g2'")
        fts_integrity(db)
        assert [r["id"] for r in list_all(db, query="solarstrom")] == [new_id]
        assert list_all(db, query="wasserstoff") == []
    finally:
        db.close()

    # Zweites Öffnen: nichts passiert, Daten bleiben.
    db = Database(path)
    try:
        assert db.one("SELECT COUNT(*) AS n FROM articles")["n"] == 3
        fts_integrity(db)
        assert len(list_all(db, query="energiewende")) == 1
    finally:
        db.close()


def test_fresh_database_has_current_schema(tmp_path):
    db = Database(tmp_path / "new.db")
    try:
        assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
        cols = [r["name"] for r in db.q("PRAGMA table_info(articles_fts)")]
        assert cols == ["title", "summary", "body_text"]
    finally:
        db.close()


# ---- Erneute Extraktion ohne Textverlust -----------------------------------

def _partial(db):
    db.upsert_articles([make_row("alpha", 1, ago(hours=1))])
    aid = db.q("SELECT id FROM articles")[0]["id"]
    db.store_extraction(aid, status="partial", body_html="<p>Anfang</p>", body_text="Anfang",
                        body_len=6, paywalled=True)
    return aid


def test_failed_reextraction_keeps_text(db):
    aid = _partial(db)
    db.store_extraction(aid, status="failed", body_html=None, body_len=0, paywalled=False, error="HTTP 403")
    row = db.get_article(aid)
    assert (row["text_status"], row["body_text"], row["body_html"], row["body_len"], row["paywalled"]) == \
        ("partial", "Anfang", "<p>Anfang</p>", 6, 1)
    assert row["text_error"] == "HTTP 403"
    # Leeres Ergebnis ebenso
    db.store_extraction(aid, status="empty", body_html=None, body_text=None, body_len=0, paywalled=False)
    row = db.get_article(aid)
    assert row["text_status"] == "partial" and row["body_text"] == "Anfang" and "ohne Text" in row["text_error"]
    fts_integrity(db)
    assert [r["id"] for r in list_all(db, query="anfang")] == [aid]


def test_transient_retry_keeps_text_and_status(db):
    aid = _partial(db)
    assert db.schedule_retry(aid, "HTTP 503") == "partial"
    row = db.get_article(aid)
    assert row["text_status"] == "partial" and row["body_text"] == "Anfang"
    assert row["attempts"] == 1 and row["next_attempt_at"] is not None and row["text_error"] == "HTTP 503"
    assert db.pending_articles(10) == []


def test_pending_with_text_becomes_partial_instead_of_endless_retries(db):
    aid = _partial(db)
    db.q("UPDATE articles SET text_status='pending' WHERE id=?", (aid,))  # z. B. Altbestand
    assert db.schedule_retry(aid, "HTTP 503") == "partial"
    row = db.get_article(aid)
    assert row["text_status"] == "partial" and row["body_text"] == "Anfang" and row["attempts"] == 1
    assert db.pending_articles(10) == []
    # Ebenso bei einem endgültigen Fehlschlag ohne Text
    db.q("UPDATE articles SET text_status='pending' WHERE id=?", (aid,))
    db.store_extraction(aid, status="failed", body_html=None, body_len=0, paywalled=False, error="HTTP 404")
    assert db.get_article(aid)["text_status"] == "partial"


def test_is_due_pending(db):
    db.upsert_articles([make_row("alpha", 1, ago(hours=1))])
    aid = db.q("SELECT id FROM articles")[0]["id"]
    assert db.is_due_pending(aid)
    db.schedule_retry(aid, "HTTP 503")
    assert not db.is_due_pending(aid)
    db.q("UPDATE articles SET next_attempt_at=? WHERE id=?", (ago(seconds=5), aid))
    assert db.is_due_pending(aid)
    db.store_extraction(aid, status="full", body_html="<p>x</p>", body_text="x", body_len=1, paywalled=False)
    assert not db.is_due_pending(aid) and not db.is_due_pending(999999)


def test_session_epoch(db):
    assert db.session_epoch() == 1
    assert db.bump_session_epoch() == 2
    assert db.session_epoch() == 2
    assert db.ping()


# ---- seen_guids ---------------------------------------------------------------

def test_purged_entry_without_date_does_not_reappear(db):
    # Eintrag ohne Datum: published_at = Zeitpunkt des ersten Abrufs, hier vor 100 Tagen.
    row = make_row("alpha", 1, ago(days=100), fetched_at=ago(days=100))
    assert db.upsert_articles([row]) == 1
    assert db.purge(90) == 1
    # Der Feed führt den Eintrag weiter; beim nächsten Abruf bekäme er wieder "jetzt" als Datum.
    again = dict(row, published_at=ago(seconds=0), fetched_at=ago(seconds=0))
    assert db.upsert_articles([again]) == 0
    assert db.one("SELECT COUNT(*) AS n FROM articles")["n"] == 0
    # seen_guids bleiben 2 x Aufbewahrungsfrist, danach darf der Eintrag wieder erscheinen.
    assert db.purge(90) == 0 and db.one("SELECT COUNT(*) AS n FROM seen_guids")["n"] == 1
    db.q("UPDATE seen_guids SET first_seen=?", (ago(days=181),))
    db.purge(90)
    assert db.one("SELECT COUNT(*) AS n FROM seen_guids")["n"] == 0
    assert db.upsert_articles([again]) == 1


def test_seen_guids_are_per_source(db):
    assert db.upsert_articles([make_row("alpha", 1, ago(hours=1), guid="x")]) == 1
    assert db.upsert_articles([make_row("beta", 1, ago(hours=1), guid="x")]) == 1
    assert db.upsert_articles([make_row("alpha", 2, ago(hours=1), guid="x")]) == 0


def test_migration_v2_to_v3_fills_seen_guids(tmp_path):
    path = tmp_path / "v2.db"
    db = Database(path)
    db.upsert_articles([make_row("alpha", i, ago(hours=i)) for i in range(3)])
    db.q("DELETE FROM seen_guids")
    db.set_meta("schema_version", "2")
    db.close()

    db = Database(path)
    try:
        assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
        assert db.one("SELECT COUNT(*) AS n FROM seen_guids")["n"] == 3
        assert db.upsert_articles([make_row("alpha", 0, ago(hours=0))]) == 0
        fts_integrity(db)
    finally:
        db.close()
    Database(path).close()  # erneutes Öffnen: nichts mehr zu tun


def test_pending_articles_keyset(db):
    same = ago(hours=1)
    db.upsert_articles([make_row("alpha", i, same) for i in range(5)])
    first = db.pending_articles(3)
    rest = db.pending_articles(3, before=(first[-1]["published_at"], first[-1]["id"]))
    ids = [r["id"] for r in first + rest]
    assert len(ids) == 5 and len(set(ids)) == 5


# ---- v4: Profile, Lieblingszeitungen, Einstellungen, "Später lesen" ------------

V4_TABLES = {"profiles", "favorite_sources", "settings"}


def _tables(db) -> set[str]:
    return {r["name"] for r in db.q("SELECT name FROM sqlite_master WHERE type='table'")}


def test_migration_v3_to_v4(tmp_path):
    path = tmp_path / "v3.db"
    db = Database(path)
    db.upsert_articles([make_row("alpha", i, ago(hours=i), title=f"Inflation {i}") for i in range(3)])
    db.set_flag(db.q("SELECT id FROM articles")[0]["id"], "starred", True)
    # Zurück auf den Stand v3: ohne Spalte later und ohne die neuen Tabellen
    db.q("ALTER TABLE articles DROP COLUMN later")
    for table in V4_TABLES:
        db.q(f"DROP TABLE {table}")
    db.set_meta("schema_version", "3")
    db.close()

    db = Database(path)
    try:
        assert db.get_meta("schema_version") == "5" == str(SCHEMA_VERSION)
        assert "later" in {r["name"] for r in db.q("PRAGMA table_info(articles)")}
        assert V4_TABLES <= _tables(db)
        assert db.one("SELECT COUNT(*) AS n FROM articles WHERE later=0 AND starred=1")["n"] == 1
        aid = db.q("SELECT id FROM articles")[1]["id"]
        assert db.set_flag(aid, "later", True)
        assert [r["id"] for r in list_all(db, later=True)] == [aid]
        assert db.seed_profiles(["alpha"], None) is True
        fts_integrity(db)
    finally:
        db.close()
    db = Database(path)  # erneutes Öffnen: nichts mehr zu tun, Daten bleiben
    try:
        assert len(db.list_profiles()) == 4 and db.one("SELECT later FROM articles WHERE id=?", (aid,))["later"] == 1
    finally:
        db.close()


def test_migration_v1_to_v4_has_new_tables(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO articles (source_id, guid, url, title, published_at, fetched_at) "
                 "VALUES ('alpha', 'g', 'https://a.example/', 'Alt', ?, ?)", (ago(hours=1), ago(hours=1)))
    conn.commit()
    conn.close()
    db = Database(path)
    try:
        assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
        assert V4_TABLES <= _tables(db)
        assert db.one("SELECT later FROM articles")["later"] == 0
        assert list_all(db)[0]["later"] == 0
    finally:
        db.close()


def test_seed_profiles(db):
    assert db.seed_profiles(["alpha"], ["beta"]) is True
    assert db.seed_profiles(["x"], None) is False  # nur wenn leer
    profiles = db.list_profiles()
    assert [(p["name"], p["builtin"], p["sources"], p["in_bar"], p["position"]) for p in profiles] == [
        ("Deutschland", "de", ["alpha"], True, 0), ("Weltweit", "world", None, False, 1),
        ("Wirtschaft", "business", ["beta"], True, 2), ("Militär", "military", None, False, 3)]
    assert profiles[2]["terms"][:2] == ["Wirtschaft", "Konjunktur"] and "Börse" in profiles[2]["terms"]
    assert "Bundeswehr" in profiles[3]["terms"] and profiles[0]["terms"] == []


def test_terms_filter_is_prefix_or_and_keeps_chronology(db):
    db.upsert_articles([
        make_row("alpha", 1, ago(hours=1), title="Die Börsen schwanken"),
        make_row("alpha", 2, ago(hours=2), title="Interest rates rise"),
        make_row("alpha", 3, ago(hours=3), title="Wetter"),
        make_row("alpha", 4, ago(hours=4), title="Neue Zinsentscheidung"),
    ])
    rows = list_all(db, terms=["Börse", "interest rates", "Zinsen"])
    # Präfix ("Zinsen" trifft "Zinsentscheidung", "Börse" trifft "Börsen"), ODER, neueste zuerst
    assert [r["url"][-1] for r in rows] == ["1", "2", "4"]
    assert list_all(db, terms=["interest rise"]) == []  # Begriff mit Leerzeichen ist eine Phrase
    rows = list_all(db, terms=["Zins", "Wett"])
    assert [r["url"][-1] for r in rows] == ["3", "4"]
    assert db.count_articles(cap=10, terms=["Zins", "Wett"]) == 2
    assert db.count_articles(cap=1, terms=["Zins", "Wett"]) == 1
    assert list_all(db, sources=[]) == []  # leere Quellenliste: nichts
    assert db.mark_all_read(["alpha"], ["Wett"]) == 1
    assert db.one("SELECT read FROM articles WHERE guid='alpha-3'")["read"] == 1


def test_settings_roundtrip(db):
    assert db.get_settings() == {}
    db.set_settings({"theme": "dark", "bar_order": ["source:alpha"]})
    assert db.get_settings() == {"theme": "dark", "bar_order": ["source:alpha"]}
