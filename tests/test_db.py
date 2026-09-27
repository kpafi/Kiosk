from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.db import SCHEMA_VERSION, Database, MigrationError, _fts_query, day_start
from conftest import UID, ago, make_row

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


def list_all(db, user=UID, **kw):
    params = dict(sources=None, query=None, unread=False, starred=False,
                  free_only=False, before=None, limit=50)
    params.update(kw)
    return db.list_articles(user, **params)


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
    db.set_flag(UID, ids["alpha-2"], "starred", True)
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
    db.set_flag(UID, ids["alpha-2"], "later", True)
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


def test_session_epoch_per_user(db):
    other = db.create_user("anna", "x")
    assert db.get_user(UID)["session_epoch"] == 1
    db.end_sessions(UID)
    assert db.get_user(UID)["session_epoch"] == 2 and db.get_user(other)["session_epoch"] == 1
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


def test_migration_v1_to_v6_in_one_pass(tmp_path):
    """Alte Datenbank (v1) mit Lesestand: in einem Durchlauf auf v6, der Lesestand geht an den Admin."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    for i in range(3):
        conn.execute("INSERT INTO articles (source_id, guid, url, title, published_at, fetched_at, read, starred) "
                     "VALUES ('alpha', ?, ?, 'Alt', ?, ?, ?, ?)",
                     (f"g{i}", f"https://a.example/{i}", ago(hours=i), ago(hours=i), int(i == 0), int(i == 1)))
    conn.commit()
    conn.close()
    # Ohne Admin-Passwort lässt sich der Lesestand keinem Benutzer zuordnen: Abbruch, nichts geändert.
    with pytest.raises(MigrationError, match="KIOSK_PASSWORD"):
        Database(path)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='articles_fts'").fetchone()[0] == 1
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() is None
    conn.close()

    db = Database(path, admin=("chef", "geheim-und-lang"))
    try:
        assert db.admin_created and db.get_meta("schema_version") == "8" == str(SCHEMA_VERSION)
        admin = db.get_user_by_name("chef")
        assert admin["is_admin"] == 1 and admin["display_name"] == "Chef"
        assert V4_TABLES <= _tables(db) and {"users", "user_articles"} <= _tables(db)
        rows = list_all(db, user=admin["id"])
        flags = {r["url"][-1]: (r["read"], r["starred"], r["later"]) for r in rows}
        assert flags == {"0": (1, 0, 0), "1": (0, 1, 0), "2": (0, 0, 0)}
        assert db.set_flag(admin["id"], rows[2]["id"], "later", True)
        assert [r["id"] for r in list_all(db, user=admin["id"], later=True)] == [rows[2]["id"]]
        assert db.seed_profiles(admin["id"], ["alpha"], None) is True
        fts_integrity(db)
    finally:
        db.close()
    db = Database(path, admin=("chef", "anderes-passwort"))  # erneutes Öffnen: nichts mehr zu tun
    try:
        assert not db.admin_created and db.user_count() == 1
        assert len(db.list_profiles(admin["id"])) == 4
        assert all(p["in_chips"] for p in db.list_profiles(admin["id"]))
        assert db.one("SELECT COUNT(*) AS n FROM user_articles")["n"] == 3
    finally:
        db.close()


def test_migration_v1_without_state_needs_no_admin(tmp_path):
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
        assert V4_TABLES <= _tables(db) and db.user_count() == 0
        assert list_all(db)[0]["later"] == 0
    finally:
        db.close()


@pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 35), reason="ALTER TABLE DROP COLUMN erst ab SQLite 3.35")
def test_migration_v6_to_v7_adds_in_chips(tmp_path):
    """v6 -> v7: profiles bekommt in_chips; bestehende Profile bleiben oben sichtbar (1), in_bar,
    Reihenfolge und alles andere unverändert."""
    path = tmp_path / "v6.db"
    db = Database(path)
    uid = db.create_user("anna", "x")
    db.seed_profiles(uid, ["alpha"], None)
    db.create_profile(uid, name="Eigenes", sources=None, terms=["Zins"], in_bar=True)
    before = [{k: v for k, v in p.items() if k != "in_chips"} for p in db.list_profiles(uid)]
    db.close()
    # Stand v6 nachbauen: Spalte weg, Version zurück.
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE profiles DROP COLUMN in_chips")
    conn.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
    conn.commit()
    conn.close()

    for _ in range(2):  # zweites Öffnen: nichts mehr zu tun
        db = Database(path)
        try:
            assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
            profiles = db.list_profiles(uid)
            assert [{k: v for k, v in p.items() if k != "in_chips"} for p in profiles] == before
            assert all(p["in_chips"] is True for p in profiles)
            db.update_profile(uid, profiles[0]["id"], {"in_chips": False})
            assert db.get_profile(uid, profiles[0]["id"])["in_chips"] is False
            db.update_profile(uid, profiles[0]["id"], {"in_chips": True})
        finally:
            db.close()


def test_create_profile_in_chips(db):
    pid = db.create_profile(UID, name="Unten", sources=None, terms=[], in_bar=True, in_chips=False)
    assert db.get_profile(UID, pid)["in_chips"] is False
    pid = db.create_profile(UID, name="Standard", sources=None, terms=[], in_bar=False)
    assert db.get_profile(UID, pid)["in_chips"] is True


def test_seed_profiles(db):
    assert db.seed_profiles(UID, ["alpha"], ["beta"]) is True
    assert db.seed_profiles(UID, ["x"], None) is False  # nur wenn leer
    profiles = db.list_profiles(UID)
    assert [(p["name"], p["builtin"], p["sources"], p["in_bar"], p["position"]) for p in profiles] == [
        ("Deutschland", "de", ["alpha"], True, 0), ("Weltweit", "world", None, False, 1),
        ("Wirtschaft", "business", ["beta"], True, 2), ("Militär", "military", None, False, 3)]
    assert all(p["in_chips"] is True for p in profiles)  # alle oben bei den Filtern
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
    assert db.count_articles(UID, cap=10, terms=["Zins", "Wett"]) == 2
    assert db.count_articles(UID, cap=1, terms=["Zins", "Wett"]) == 1
    assert list_all(db, sources=[]) == []  # leere Quellenliste: nichts
    assert db.mark_all_read(UID, ["alpha"], ["Wett"]) == 1
    assert [r["url"][-1] for r in list_all(db, unread=True)] == ["1", "2", "4"]


def test_settings_roundtrip(db):
    assert db.get_settings(UID) == {}
    db.set_settings(UID, {"theme": "dark", "bar_order": ["source:alpha"]})
    assert db.get_settings(UID) == {"theme": "dark", "bar_order": ["source:alpha"]}


# ---- Tagesbeginn der Ungelesen-Zähler ------------------------------------------

BERLIN = ZoneInfo("Europe/Berlin")


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_day_start_berlin_winter_and_summer_time():
    # Winterzeit (MEZ, UTC+1): der Tag beginnt um 23:00 UTC des Vortags; Sommerzeit (MESZ): 22:00
    assert day_start(BERLIN, utc(2026, 1, 15, 12, 0)) == "2026-01-14T23:00:00+00:00"
    assert day_start(BERLIN, utc(2026, 7, 15, 12, 0)) == "2026-07-14T22:00:00+00:00"
    # Format wie published_at (ohne Mikrosekunden), auch bei einem Jetzt mit Mikrosekunden
    assert day_start(BERLIN, utc(2026, 7, 15, 12, 0, 0, 123456)) == "2026-07-14T22:00:00+00:00"
    assert day_start(ZoneInfo("UTC"), utc(2026, 7, 15, 0, 0)) == "2026-07-15T00:00:00+00:00"


def test_day_start_around_midnight():
    # 23:59:59 Ortszeit gehört noch zum alten Tag, um 00:00 Ortszeit beginnt der neue
    assert day_start(BERLIN, utc(2026, 9, 25, 21, 59, 59)) == "2026-09-24T22:00:00+00:00"
    assert day_start(BERLIN, utc(2026, 9, 25, 22, 0, 0)) == "2026-09-25T22:00:00+00:00"
    # Mitternacht UTC ist in Berlin schon 02:00: derselbe Tag wie zwei Stunden vorher
    assert day_start(BERLIN, utc(2026, 9, 26, 0, 0)) == "2026-09-25T22:00:00+00:00"
    # Ein Jetzt in Ortszeit ergibt dasselbe wie in UTC
    assert day_start(BERLIN, datetime(2026, 9, 26, 0, 30, tzinfo=BERLIN)) == "2026-09-25T22:00:00+00:00"


def test_day_start_on_dst_switch_days():
    # 29.3.2026: um 02:00 auf Sommerzeit, ein Tag mit 23 Stunden; er beginnt noch in MEZ
    assert day_start(BERLIN, utc(2026, 3, 29, 21, 59)) == "2026-03-28T23:00:00+00:00"
    assert day_start(BERLIN, utc(2026, 3, 29, 22, 0)) == "2026-03-29T22:00:00+00:00"
    # 25.10.2026: um 03:00 zurück auf Winterzeit, ein Tag mit 25 Stunden. 23:30 Ortszeit (MEZ) ist
    # 22:30 UTC; „jetzt minus 24 Stunden“ läge hier schon im Vortag.
    assert day_start(BERLIN, utc(2026, 10, 25, 22, 30)) == "2026-10-24T22:00:00+00:00"
    assert day_start(BERLIN, utc(2026, 10, 25, 23, 0)) == "2026-10-25T23:00:00+00:00"


@pytest.mark.real_day
def test_database_today_start_uses_its_time_zone(db):
    assert str(db.tz) == "Europe/Berlin"
    assert db.today_start(now=utc(2026, 7, 15, 12, 0)) == "2026-07-14T22:00:00+00:00"
    db.tz = ZoneInfo("America/New_York")
    assert db.today_start(now=utc(2026, 7, 15, 12, 0)) == "2026-07-15T04:00:00+00:00"
    # Ohne now: der echte heutige Tag (Aufruf zwischen zwei Messungen, falls genau Mitternacht ist)
    before = day_start(db.tz)
    got = db.today_start()
    assert got in (before, day_start(db.tz))


@pytest.mark.real_day
def test_unread_counters_count_only_today(db, monkeypatch):
    """Zähler (Leiste, Zeitungen, Menü) zählen ab 00:00 Ortszeit; Liste, Zählung der Liste,
    „alles gelesen“ und die Status-Kennzahl nehmen weiter alle ungelesenen Artikel."""
    now = utc(2026, 9, 26, 10, 0)            # 12:00 in Berlin
    monkeypatch.setattr(db, "today_start", lambda: Database.today_start(db, now=now))
    db.upsert_articles([
        make_row("alpha", 1, "2026-09-25T21:59:00+00:00"),   # gestern 23:59 in Berlin
        make_row("alpha", 2, "2026-09-25T22:00:00+00:00"),   # heute 00:00 in Berlin
        make_row("beta", 3, "2026-09-26T09:00:00+00:00"),
        make_row("beta", 4, "2026-09-26T09:30:00+00:00"),    # Paywall: zählt nie
        make_row("beta", 5, "2026-09-20T09:00:00+00:00"),
    ])
    db.q("UPDATE articles SET paywalled=1 WHERE guid='beta-4'")
    assert db.unread_free_by_source(UID) == {"alpha": 1, "beta": 1}
    assert db.article_counts(UID)["unread"] == 2
    assert db.article_counts(UID, ["alpha"])["unread"] == 1
    assert db.source_counts(UID) == {"alpha": {"total": 2, "unread": 1, "unread_all": 2},
                                     "beta": {"total": 2, "unread": 1, "unread_all": 2}}
    # Unverändert: Ungelesen-Liste, ihre Zählung und die Status-Kennzahl
    assert len(list_all(db, unread=True, free_only=True)) == 4
    assert db.count_articles(UID, cap=100, unread=True, free_only=True) == 4
    assert db.stats(UID)["unread"] == 4
    # Heutiger Artikel gelesen: Zähler sinkt, der gestrige zählt weiter nur in unread_all
    db.set_flag(UID, db.one("SELECT id FROM articles WHERE guid='alpha-2'")["id"], "read", True)
    assert db.unread_free_by_source(UID) == {"beta": 1}
    assert db.source_counts(UID)["alpha"] == {"total": 2, "unread": 0, "unread_all": 1}
    # „alles gelesen“ markiert auch die älteren
    assert db.mark_all_read(UID, ["alpha"]) == 1
    assert db.source_counts(UID)["alpha"] == {"total": 2, "unread": 0, "unread_all": 0}


# ---- Schutz der Event-Loop: Suche, Profil-Begriffe, Zeitlimit -----------------------

def test_fts_prefix_only_for_words_of_three_or_more_chars_and_word_cap():
    from app.db import SEARCH_MAX_WORDS, _terms_query
    assert _fts_query("a bc abc") == '"a" AND "bc" AND "abc"*'
    assert _fts_query("U.S. Börse") == '"U.S." AND "Börse"*'      # letztes Wort "s": exakt
    many = " ".join(f"w{i:02d}x" for i in range(20))
    assert _fts_query(many).count(" AND ") == SEARCH_MAX_WORDS - 1  # der Rest zählt nicht
    assert _fts_query("a " * 100).count('"a"') == SEARCH_MAX_WORDS
    assert _terms_query(["EU", "NATO", "interest rates", "g 7"]) == '"EU" OR "NATO"* OR "interest rates"* OR "g 7"'


def test_short_search_word_matches_exactly(db):
    db.upsert_articles([make_row("alpha", 1, ago(hours=1), title="EU beschließt"),
                        make_row("alpha", 2, ago(hours=2), title="Europa wählt")])
    assert [r["title"] for r in list_all(db, query="eu")] == ["EU beschließt"]
    assert {r["title"] for r in list_all(db, query="eur")} == {"Europa wählt"}


SLOW_SQL = ("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < ?) "
            "SELECT COUNT(*) AS c FROM n")


def test_slow_read_query_is_aborted(db):
    import time
    from app.db import QueryTimeout
    t0 = time.monotonic()
    with pytest.raises(QueryTimeout):
        db.q(SLOW_SQL, (10**10,), time_limit=0.2)
    assert time.monotonic() - t0 < 3
    # Die Verbindung bleibt benutzbar, ohne Frist läuft dieselbe (kurze) Abfrage durch.
    assert db.one(SLOW_SQL, (1000,))["c"] == 1000
    assert db.one(SLOW_SQL, (200_000,), time_limit=None)["c"] == 200_000


def test_time_limit_does_not_apply_inside_transactions(db):
    with db.tx():
        assert db.one(SLOW_SQL, (300_000,), time_limit=1e-9)["c"] == 300_000


def test_count_queries_use_covering_source_index(db):
    """source_counts und unread_free_by_source lesen nur den Index, nicht die Zeilen samt Volltext."""
    seen: list[tuple] = []
    real = db.q

    def spy(sql, params=(), **kw):
        seen.append((sql, params))
        return real(sql, params, **kw)

    db.q = spy
    db.source_counts(UID)
    db.unread_free_by_source(UID)
    assert len(seen) == 2
    for sql, params in seen:
        plan = " | ".join(r["detail"] for r in real("EXPLAIN QUERY PLAN " + sql, params))
        assert "COVERING INDEX idx_articles_source_counts" in plan, plan


def test_existing_database_gets_new_source_index(tmp_path):
    d = Database(tmp_path / "k.db")
    d.q("DROP INDEX idx_articles_source_counts")
    d.q("CREATE INDEX idx_articles_source ON articles (source_id, published_at DESC)")
    d.close()
    d = Database(tmp_path / "k.db")
    idx = {r["name"] for r in d.q("PRAGMA index_list(articles)")}
    assert "idx_articles_source_counts" in idx and "idx_articles_source" not in idx
    d.close()


# ---- seen_guids und neue Artikel je Lauf ---------------------------------------------

def test_seen_guid_is_refreshed_while_entry_stays_in_feed(db):
    """Ein Eintrag ohne Datum, der dauerhaft im Feed steht, kommt nach purge nicht als neu zurück."""
    row = make_row("alpha", 1, ago(hours=1))
    assert db.upsert_articles([row]) == 1
    db.q("UPDATE seen_guids SET first_seen=?", (ago(days=170),))
    db.q("UPDATE articles SET published_at=?", (ago(days=100),))
    assert db.upsert_articles([{**row, "fetched_at": ago(seconds=0)}]) == 0
    assert db.one("SELECT first_seen FROM seen_guids")["first_seen"] >= ago(minutes=1)
    assert db.purge(90) == 1                     # der Artikel ist weg ...
    assert db.one("SELECT COUNT(*) AS n FROM seen_guids")["n"] == 1   # ... der Eintrag bleibt bekannt
    assert db.upsert_articles([{**row, "fetched_at": ago(seconds=0)}]) == 0


def test_seen_guid_refresh_at_most_daily(db):
    row = make_row("alpha", 1, ago(hours=1))
    db.upsert_articles([row])
    recent = ago(hours=3)
    db.q("UPDATE seen_guids SET first_seen=?", (recent,))
    db.upsert_articles([{**row, "fetched_at": ago(seconds=0)}])
    assert db.one("SELECT first_seen FROM seen_guids")["first_seen"] == recent


def test_upsert_max_new_leaves_rest_unseen(db):
    rows = [make_row("alpha", i, ago(minutes=i)) for i in range(5)]
    assert db.upsert_articles(rows, max_new=2) == 2
    assert db.one("SELECT COUNT(*) AS n FROM seen_guids")["n"] == 2
    assert db.upsert_articles(rows, max_new=2) == 2   # die nächsten beiden
    assert db.upsert_articles(rows) == 1


# ---- Validatoren eines Feeds -----------------------------------------------------------

def test_record_fetch_validators(db):
    db.record_fetch("alpha", ok=True, validators=('"v1"', "Mon, 01 Sep 2026 10:00:00 GMT"))
    st = db.source_states()["alpha"]
    assert (st["etag"], st["last_modified"]) == ('"v1"', "Mon, 01 Sep 2026 10:00:00 GMT")
    db.record_fetch("alpha", ok=True)                  # 304: bleiben
    assert db.source_states()["alpha"]["etag"] == '"v1"'
    db.record_fetch("alpha", ok=True, validators=(None, "Mon, 01 Sep 2026 11:00:00 GMT"))
    st = db.source_states()["alpha"]
    assert st["etag"] is None and st["last_modified"] == "Mon, 01 Sep 2026 11:00:00 GMT"
    db.record_fetch("alpha", ok=False, error="HTTP 500")  # Fehlschlag löscht beide
    st = db.source_states()["alpha"]
    assert st["etag"] is None and st["last_modified"] is None and st["last_error"] == "HTTP 500"


def test_same_article_under_a_new_guid_is_not_new(db):
    """BBC führt denselben Artikel mit wechselnder Nummer in der GUID („…#1“, „…#8“): gleiche Quelle,
    gleiche Adresse, gleicher Titel, weniger als 6 h auseinander ist kein zweiter Artikel. Andere Quelle,
    anderer Titel unter derselben Adresse (tägliche Presseschau) oder genau 6 h später: neu."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    base = make_row("bbc", 1, (now - timedelta(hours=10)).isoformat())
    url = base["url"]
    assert db.upsert_articles([{**base, "guid": url + "#1"}]) == 1
    assert db.upsert_articles([{**base, "guid": url + "#8", "title": "  " + base["title"].upper() + " ",
                                "image_url": "https://img.example/b.jpg"}]) == 0
    assert db.upsert_articles([{**make_row("guardian", 1, base["published_at"]), "url": url}]) == 1
    assert db.upsert_articles([{**base, "guid": url + "#9", "title": "Presseschau 27.9."}]) == 1
    edge = (now - timedelta(hours=4)).isoformat()            # genau 6 h später: nicht mehr „dasselbe“
    assert db.upsert_articles([{**base, "guid": url + "#10", "published_at": edge}]) == 1
    near = (now - timedelta(hours=10) + timedelta(hours=5, minutes=59)).isoformat()
    assert db.upsert_articles([{**base, "guid": url + "#11", "published_at": near}]) == 0
    assert db.one("SELECT COUNT(*) AS n FROM articles WHERE source_id='bbc'")["n"] == 3


def test_hourly_bulletin_with_fixed_url_and_title_is_not_deduped(db):
    """Nur das BBC-Muster („…#1“, „…#8“) gilt als derselbe Artikel: Eine stündliche Sendung einer anderen
    Quelle mit fester Adresse und festem Titel, aber eigener GUID je Ausgabe, bleibt je Ausgabe neu."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    first = make_row("tagesschau", 1, (now - timedelta(hours=3)).isoformat(), title="tagesschau in 100 Sekunden")
    url = "https://www.tagesschau.de/multimedia/sendung/tagesschau_in_100_sekunden"
    assert db.upsert_articles([{**first, "url": url, "guid": "ts100-1800"}]) == 1
    second = make_row("tagesschau", 2, (now - timedelta(hours=2)).isoformat(), title="tagesschau in 100 Sekunden")
    assert db.upsert_articles([{**second, "url": url, "guid": "ts100-1900"}]) == 1
    # ... auch mit „#“ in der GUID, solange ihr Stamm weder die Adresse noch der gespeicherte Stamm ist
    third = make_row("tagesschau", 3, (now - timedelta(hours=1)).isoformat(), title="tagesschau in 100 Sekunden")
    assert db.upsert_articles([{**third, "url": url, "guid": "ts100#2000"}]) == 1
