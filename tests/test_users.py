"""Mehrbenutzer (Plan Abschnitt 8): Migration v5 -> v6, getrennte Lesestände und Profile,
Anmeldung, Rechte, Kommandozeile, keine Zugriffe mehr auf articles.read/starred/later."""
from __future__ import annotations

import ast
import io
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.users as users_cli
from app.auth import LOGIN_ERROR, hash_password, verify_password
from app.db import FTS_DDL, Database, MigrationError
from app.main import app, state
from app.users import seed_builtin_profiles
from conftest import ADMIN_PASSWORD, UID, ago, make_row

ROOT = Path(__file__).resolve().parent.parent
ORIGIN = "https://testserver"
PW = "anna-passwort-123"


# ---- kein Code benutzt mehr die alten Flag-Spalten -------------------------------

# Alte Zugriffe auf articles.read/starred/later (Alias a, Tabellenname oder unqualifiziert in
# Anweisungen auf articles). "ua.read" usw. (user_articles) ist erlaubt.
FORBIDDEN = [
    re.compile(r"(?<![\w.])a\.(read|starred|later)\b"),
    re.compile(r"\barticles\.(read|starred|later)\b"),
    re.compile(r"UPDATE articles[^\"]*\b(read|starred|later)\s*=", re.I),
    re.compile(r"FROM articles\b[^\"]*WHERE[^\"]*(?<![\w.])(read|starred|later)\s*[=<>]", re.I),
    re.compile(r"SUM\((read|starred|later)\s*="),
    re.compile(r"SELECT \* FROM articles"),
]


def _source_without_migration(path: Path) -> str:
    """Quelltext ohne _migrate_v6 (die Migration liest die alten Spalten ein letztes Mal)."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_migrate_v6":
            for i in range(node.lineno - 1, node.end_lineno):
                lines[i] = ""
    return "\n".join(lines)


def test_app_no_longer_uses_article_flag_columns():
    hits = []
    for path in sorted((ROOT / "app").glob("*.py")):
        src = _source_without_migration(path)
        for pattern in FORBIDDEN:
            hits += [f"{path.name}: {m.group(0)}" for m in pattern.finditer(src)]
    assert not hits, hits
    # Das Muster erkennt die alten Anweisungen tatsächlich
    old = ('"UPDATE articles SET read=1 WHERE id=?" "a.starred=1" '
           '"SELECT source_id, COUNT(*) FROM articles WHERE read=0 AND paywalled=0" "SUM(read=0 AND x)" '
           '"articles.later"')
    assert all(p.search(old) for p in FORBIDDEN[:5])
    assert "_migrate_v6" in (ROOT / "app" / "db.py").read_text(encoding="utf-8")


# ---- Migration v5 -> v6 ---------------------------------------------------------

V5_SCHEMA = """
CREATE TABLE source_state (id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, etag TEXT,
    last_modified TEXT, last_fetch_at TEXT, last_ok_at TEXT, last_error TEXT, last_new INTEGER NOT NULL DEFAULT 0);
CREATE TABLE articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, guid TEXT NOT NULL, url TEXT NOT NULL,
    title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', author TEXT, image_url TEXT,
    published_at TEXT NOT NULL, fetched_at TEXT NOT NULL, body_html TEXT, body_text TEXT,
    body_len INTEGER NOT NULL DEFAULT 0, text_status TEXT NOT NULL DEFAULT 'pending', text_error TEXT,
    extracted_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
    paywalled INTEGER NOT NULL DEFAULT 0, read INTEGER NOT NULL DEFAULT 0,
    starred INTEGER NOT NULL DEFAULT 0, later INTEGER NOT NULL DEFAULT 0, UNIQUE (source_id, guid));
CREATE INDEX idx_articles_read_paywalled_source ON articles (read, paywalled, source_id);
CREATE TABLE seen_guids (source_id TEXT NOT NULL, guid TEXT NOT NULL, first_seen TEXT NOT NULL,
    PRIMARY KEY (source_id, guid)) WITHOUT ROWID;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE profiles (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, builtin TEXT,
    sources_json TEXT, terms_json TEXT NOT NULL DEFAULT '[]', in_bar INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE favorite_sources (source_id TEXT PRIMARY KEY, in_bar INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES ('schema_version', '5'), ('session_epoch', '3');
"""


def _make_v5(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(V5_SCHEMA)
    for stmt in FTS_DDL[4:]:
        conn.execute(stmt)
    conn.execute("INSERT INTO source_state (id, enabled) VALUES ('alpha', 1), ('beta', 1), ('gamma', 0)")
    for i in range(6):
        conn.execute(
            "INSERT INTO articles (source_id, guid, url, title, published_at, fetched_at, read, starred, later) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("alpha" if i % 2 else "beta", f"g{i}", f"https://x.example/{i}", f"Titel {i}", ago(hours=i),
             ago(hours=i), int(i < 3), int(i == 1), int(i == 4)))
    conn.execute("INSERT INTO profiles (id, name, builtin, sources_json, terms_json, in_bar, position, created_at) "
                 "VALUES (5, 'Deutschland', 'de', '[\"alpha\"]', '[]', 1, 0, 'x'), "
                 "(9, 'Eigen', NULL, NULL, '[\"Zins\"]', 1, 1, 'x')")
    conn.execute("INSERT INTO favorite_sources VALUES ('beta', 1, 0, 'x')")
    conn.execute("INSERT INTO settings VALUES ('theme', '\"light\"'), ('bar_order', '[\"profile:9\", \"source:beta\"]')")
    conn.commit()
    conn.close()


def _legacy_counts(path: Path) -> dict:
    conn = sqlite3.connect(path)
    try:
        q = lambda sql: conn.execute(sql).fetchall()  # noqa: E731
        return {
            "articles": q("SELECT COUNT(*) FROM articles")[0][0],
            "flags": sorted(q("SELECT id, read, starred, later FROM articles WHERE read=1 OR starred=1 OR later=1")),
            "profiles": sorted(q("SELECT id, name, builtin, sources_json, terms_json, in_bar, position FROM profiles")),
            "favorites": sorted(q("SELECT source_id, in_bar, position FROM favorite_sources")),
            "settings": sorted(q("SELECT key, value FROM settings")),
        }
    finally:
        conn.close()


def _v6_counts(db: Database, user_id: int) -> dict:
    return {
        "articles": db.one("SELECT COUNT(*) AS n FROM articles")["n"],
        "flags": sorted(tuple(r) for r in db.q(
            "SELECT article_id, read, starred, later FROM user_articles WHERE user_id=?", (user_id,))),
        "profiles": sorted(tuple(r) for r in db.q(
            "SELECT id, name, builtin, sources_json, terms_json, in_bar, position FROM profiles WHERE user_id=?",
            (user_id,))),
        "favorites": sorted(tuple(r) for r in db.q(
            "SELECT source_id, in_bar, position FROM favorite_sources WHERE user_id=?", (user_id,))),
        "settings": sorted(tuple(r) for r in db.q("SELECT key, value FROM settings WHERE user_id=?", (user_id,))),
    }


def _check_migration(path: Path) -> dict:
    """Migriert eine Kopie (v4/v5) und prüft: alles liegt beim Admin, Zahlen vorher = nachher."""
    before = _legacy_counts(path)
    if any(before[k] for k in ("flags", "profiles", "favorites", "settings")):
        with pytest.raises(MigrationError):  # ohne KIOSK_PASSWORD: Abbruch, Datenbank unverändert
            Database(path)
        assert _legacy_counts(path) == before
    db = Database(path, admin=("admin", ADMIN_PASSWORD))
    try:
        admin = db.get_user_by_name("admin")
        assert db.admin_created and admin["is_admin"] == 1 and db.user_count() == 1
        assert verify_password(ADMIN_PASSWORD, admin["password_hash"])
        assert db.get_meta("schema_version") == "6"
        after = _v6_counts(db, admin["id"])
        assert after == before
        tables = {r["name"] for r in db.q("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not {t for t in tables if t.endswith("_v5")}
        db.q("INSERT INTO articles_fts(articles_fts, rank) VALUES('integrity-check', 1)")
        assert db.q("PRAGMA foreign_key_check") == []
        assert db.one("PRAGMA integrity_check")[0] == "ok"
    finally:
        db.close()
    db = Database(path, admin=("admin", "ein-anderes-passwort"))  # zweites Öffnen: nichts passiert
    try:
        assert not db.admin_created and _v6_counts(db, 1) == before
    finally:
        db.close()
    return before


def test_migration_v5_to_v6_moves_everything_to_admin(tmp_path):
    path = tmp_path / "kiosk.db"
    _make_v5(path)
    before = _check_migration(path)
    assert len(before["flags"]) == 4 and len(before["profiles"]) == 2 and len(before["settings"]) == 2
    db = Database(path)
    try:
        # Lesestand über die normale Abfrage sichtbar, alte Index weg
        rows = db.list_articles(1, sources=None, query=None, unread=False, starred=True, free_only=False,
                                before=None, limit=10)
        assert [r["url"] for r in rows] == ["https://x.example/1"]
        assert db.get_settings(1) == {"theme": "light", "bar_order": ["profile:9", "source:beta"]}
        idx = {r["name"] for r in db.q("PRAGMA index_list(articles)")}
        assert "idx_articles_read_paywalled_source" not in idx
    finally:
        db.close()


@pytest.mark.parametrize("var", ["KIOSK_PRODCOPY_DB", "KIOSK_PRODCOPY_DB_V4"])
def test_migration_on_production_copy(tmp_path, var):
    """Kopie der Produktionsdatenbank (Pfad per Umgebungsvariable, sonst übersprungen)."""
    src = os.environ.get(var)
    if not src or not Path(src).is_file():
        pytest.skip(f"{var} nicht gesetzt")
    path = tmp_path / "kiosk.db"
    shutil.copy(src, path)
    for suffix in ("-wal", "-shm"):
        if Path(src + suffix).exists():
            shutil.copy(src + suffix, str(path) + suffix)
    _check_migration(path)


def test_app_start_on_v5_database_keeps_admin_login_and_state(app_env):
    _make_v5(app_env / "kiosk.db")
    with TestClient(app, base_url=ORIGIN) as c:
        assert _login(c, "admin", ADMIN_PASSWORD).status_code == 204
        assert c.get("/api/counts").json()["starred"] == 1
        assert c.get("/api/settings").json()["theme"] == "light"
        names = [p["name"] for p in c.get("/api/profiles").json()]
        assert names == ["Deutschland", "Eigen"]  # hatte schon Profile: kein erneutes Säen


# ---- zwei Benutzer über die API ------------------------------------------------

def _login(c, username, password):
    return c.post("/api/login", json={"username": username, "password": password})


@pytest.fixture
def two(app_env):
    """(admin, anna): zwei angemeldete Clients derselben App; anna ist kein Admin."""
    with TestClient(app, base_url=ORIGIN) as admin:
        uid = state.db.create_user("anna", hash_password(PW), display_name="Anna")
        seed_builtin_profiles(state.db, state.catalog, uid)
        anna = TestClient(app, base_url=ORIGIN)  # ohne with: teilt Zustand und Lifespan
        assert _login(admin, "admin", ADMIN_PASSWORD).status_code == 204
        assert _login(anna, "Anna", PW).status_code == 204  # Name ohne Groß/klein
        yield admin, anna


def _articles(n=3):
    state.db.upsert_articles([make_row("alpha", i, ago(hours=i + 1)) for i in range(n)])
    return [r["id"] for r in state.db.q("SELECT id FROM articles ORDER BY id")]


def _ids(c, **params):
    return [it["id"] for it in c.get("/api/articles", params=params).json()["items"]]


def test_old_articles_only_visible_to_who_kept_them(two):
    """Hinter der Aufbewahrungsgrenze (90 Tage) behält purge Artikel, die jemand gemerkt hat;
    sehen darf sie nur, wer sie selbst als Favorit oder „Später lesen“ hat."""
    admin, anna = two
    assert state.db.retention_days == state.settings.retention_days == 90
    state.db.upsert_articles([make_row("alpha", 1, ago(hours=2)), make_row("alpha", 2, ago(days=100)),
                              make_row("alpha", 3, ago(days=120))])
    new, old, older = [r["id"] for r in state.db.q("SELECT id FROM articles ORDER BY id")]
    # Gemerkt, solange sie noch jung waren (hier ohne Grenze nachgestellt)
    state.db.retention_days = None
    assert admin.post(f"/api/articles/{old}/star", json={"value": True}).status_code == 200
    assert admin.post(f"/api/articles/{older}/later", json={"value": True}).status_code == 200
    state.db.retention_days = 90
    assert state.db.purge(90) == 0  # beide bleiben wegen des Admins
    # Admin sieht beide alten Artikel, anna nur den neuen
    assert set(_ids(admin, unread="false")) == {new, old, older}
    assert _ids(anna, unread="false") == [new]
    assert _ids(anna) == [new] and _ids(anna, q="Titel") == [new]
    assert anna.get("/api/counts").json()["unread"] == 1
    for aid in (old, older):
        assert anna.get(f"/api/articles/{aid}").status_code == 404
        # auch nicht über Merken per ID
        assert anna.post(f"/api/articles/{aid}/star", json={"value": True}).status_code == 404
        assert anna.post(f"/api/articles/{aid}/later", json={"value": True}).status_code == 404
    assert anna.get(f"/api/articles/{new}/next", params={"unread": "false"}).json()["item"] is None
    assert admin.get(f"/api/articles/{old}").status_code == 200
    assert _ids(admin, starred="true", unread="false") == [old]
    # "alles gelesen" markiert für anna nichts Altes
    assert anna.post("/api/articles/read-all", json={}).status_code == 200
    assert not state.db.q("SELECT 1 FROM user_articles WHERE user_id=2 AND article_id IN (?, ?)", (old, older))


def test_session_and_me(two):
    admin, anna = two
    me = {"id": 2, "username": "anna", "display_name": "Anna", "is_admin": False}
    assert anna.get("/api/session").json() == {"authenticated": True, "user": me}
    assert anna.get("/api/me").json() == me
    assert admin.get("/api/me").json()["is_admin"] is True
    assert TestClient(app, base_url=ORIGIN).get("/api/me").status_code == 401


def test_read_starred_later_are_per_user(two):
    admin, anna = two
    ids = _articles()
    assert admin.post(f"/api/articles/{ids[0]}/read", json={"value": True}).json() == {"id": ids[0], "read": True}
    admin.post(f"/api/articles/{ids[1]}/star", json={"value": True})
    admin.post(f"/api/articles/{ids[2]}/later", json={"value": True})
    assert sorted(_ids(admin, unread="true")) == ids[1:]
    assert sorted(_ids(anna, unread="true")) == ids
    assert _ids(admin, starred="true") == [ids[1]] and _ids(anna, starred="true") == []
    assert _ids(admin, later="true") == [ids[2]] and _ids(anna, later="true") == []
    assert admin.get(f"/api/articles/{ids[1]}").json()["starred"] == 1
    assert anna.get(f"/api/articles/{ids[1]}").json()["starred"] == 0
    item = next(it for it in anna.get("/api/articles").json()["items"] if it["id"] == ids[0])
    assert (item["read"], item["starred"], item["later"]) == (0, 0, 0)
    a, b = admin.get("/api/counts").json(), anna.get("/api/counts").json()
    assert (a["unread"], a["starred"], a["later"]) == (2, 1, 1)
    assert (b["unread"], b["starred"], b["later"]) == (3, 0, 0)
    assert admin.get("/api/bar").json()["items"][0]["unread"] == 2
    assert anna.get("/api/bar").json()["items"][0]["unread"] == 3
    src = {s["id"]: s["unread"] for s in anna.get("/api/sources").json()}
    assert src["alpha"] == 3
    # Profil-Zähler (Zwischenspeicher pro Benutzer)
    de = lambda c: next(p for p in c.get("/api/profiles").json() if p["builtin"] == "de")  # noqa: E731
    assert de(admin)["unread"] == 2 and de(anna)["unread"] == 3
    # "Alles gelesen" wirkt nur für den eigenen Benutzer
    assert anna.post("/api/articles/read-all", json={}).json() == {"marked": 3}
    assert de(anna)["unread"] == 0 and de(admin)["unread"] == 2
    assert sorted(_ids(admin, unread="true")) == ids[1:]
    # Zurücksetzen: Zeile ohne Flags verschwindet
    admin.post(f"/api/articles/{ids[1]}/star", json={"value": False})
    admin.post(f"/api/articles/{ids[2]}/later", json={"value": False})
    assert [r[0] for r in state.db.q("SELECT article_id FROM user_articles WHERE user_id=1")] == [ids[0]]


def test_read_all_watermark_upsert_keeps_other_flags(two):
    admin, _ = two
    ids = _articles(4)
    admin.post(f"/api/articles/{ids[1]}/star", json={"value": True})
    assert admin.post("/api/articles/read-all", json={"max_id": ids[2]}).json() == {"marked": 3}
    row = state.db.one("SELECT read, starred FROM user_articles WHERE user_id=1 AND article_id=?", (ids[1],))
    assert tuple(row) == (1, 1)
    assert _ids(admin, unread="true") == [ids[3]]


def test_profiles_favorites_settings_bar_are_per_user(two):
    admin, anna = two
    mine = admin.post("/api/profiles", json={"name": "Nur meins", "terms": ["Zins"], "in_bar": True}).json()
    assert "Nur meins" not in [p["name"] for p in anna.get("/api/profiles").json()]
    assert len(anna.get("/api/profiles").json()) == 4  # die eigenen vorgegebenen
    pid = mine["id"]
    assert anna.patch(f"/api/profiles/{pid}", json={"name": "x"}).status_code == 404
    assert anna.delete(f"/api/profiles/{pid}").status_code == 404
    assert anna.get("/api/articles", params={"profile": pid}).status_code == 404
    assert anna.post("/api/articles/read-all", json={"profile": pid}).status_code == 404
    assert anna.patch("/api/bar", json={"order": [f"profile:{pid}"]}).status_code == 400
    assert anna.post("/api/profiles/reorder", json={"ids": [pid]}).status_code == 400
    assert admin.patch("/api/bar", json={"order": [f"profile:{pid}"]}).status_code == 200
    assert f"profile:{pid}" in [i["key"] for i in admin.get("/api/bar").json()["items"]]
    assert f"profile:{pid}" not in [i["key"] for i in anna.get("/api/bar").json()["items"]]

    assert admin.put("/api/favorites/sources/alpha", json={}).status_code == 200
    assert anna.get("/api/favorites/sources").json() == []
    assert [s["is_favorite"] for s in anna.get("/api/sources").json()] == [False, False, False]
    assert admin.get("/api/counts").json()["favorites"] == 1 and anna.get("/api/counts").json()["favorites"] == 0
    anna.put("/api/favorites/sources/beta", json={"in_bar": False})
    assert anna.delete("/api/favorites/sources/alpha").status_code == 204  # fremde bleibt
    assert [f["source_id"] for f in admin.get("/api/favorites/sources").json()] == ["alpha"]

    admin.patch("/api/settings", json={"theme": "light", "text_size": "l"})
    assert anna.get("/api/settings").json()["theme"] == "dark"
    assert admin.get("/api/settings").json()["text_size"] == "l"


def test_purge_keeps_articles_saved_by_any_user(db):
    other = db.create_user("anna", "x")
    db.upsert_articles([make_row("alpha", i, ago(days=200)) for i in range(4)])
    ids = [r["id"] for r in db.q("SELECT id FROM articles ORDER BY id")]
    db.set_flag(other, ids[0], "starred", True)   # nur B hat gemerkt
    db.set_flag(UID, ids[1], "later", True)
    db.set_flag(UID, ids[2], "read", True)        # gelesen hält nicht fest
    assert db.purge(90) == 2
    assert [r["id"] for r in db.q("SELECT id FROM articles ORDER BY id")] == ids[:2]
    # Lesestand gelöschter Artikel ist per Cascade weg
    assert db.one("SELECT COUNT(*) AS n FROM user_articles WHERE article_id=?", (ids[2],))["n"] == 0
    db.q("INSERT INTO articles_fts(articles_fts, rank) VALUES('integrity-check', 1)")


# ---- Anmeldung ------------------------------------------------------------------

def test_login_errors_are_identical(app_env):
    with TestClient(app, base_url=ORIGIN) as c:
        state.db.create_user("anna", hash_password(PW))
        unknown = _login(c, "niemand", PW)
        wrong = _login(c, "anna", "falsches-passwort")
        invalid = _login(c, "x", PW)  # ungültiger Name
        for r in (unknown, wrong, invalid):
            assert r.status_code == 401 and r.json() == {"detail": LOGIN_ERROR}
        state.db.update_user(2, disabled=1)
        r = _login(c, "anna", PW)
        assert r.status_code == 401 and r.json() == {"detail": LOGIN_ERROR}
        assert c.post("/api/login", json={"password": PW}).status_code == 422  # Benutzername ist Pflicht
        assert state.db.get_user(1)["last_login_at"] is None
        assert _login(c, "admin", ADMIN_PASSWORD).status_code == 204
        assert state.db.get_user(1)["last_login_at"]


def test_disabled_user_loses_session(two):
    _, anna = two
    assert anna.get("/api/counts").status_code == 200
    state.db.update_user(2, disabled=1)
    assert anna.get("/api/counts").status_code == 401
    assert anna.get("/api/session").json() == {"authenticated": False, "user": None}
    state.db.update_user(2, disabled=0)  # ohne CLI (die beendet beim Sperren die Sitzungen)
    assert anna.get("/api/counts").status_code == 200


def test_logout_ends_only_own_sessions(two):
    admin, anna = two
    token = anna.cookies.get("__Host-kiosk_session")
    assert anna.post("/api/logout").status_code == 204
    anna.cookies.set("__Host-kiosk_session", token)  # kopiertes Cookie
    assert anna.get("/api/counts").status_code == 401
    assert admin.get("/api/counts").status_code == 200
    assert state.db.get_user(2)["session_epoch"] == 2 and state.db.get_user(1)["session_epoch"] == 1
    anna.cookies.clear()
    assert _login(anna, "anna", PW).status_code == 204 and anna.get("/api/counts").status_code == 200


def test_cli_passwd_ends_sessions(two, monkeypatch):
    admin, anna = two
    _cli_env(monkeypatch, state.settings.db_path.parent)
    monkeypatch.setattr(sys, "stdin", io.StringIO("ganz-neues-passwort\n"))
    assert users_cli.main(["passwd", "anna", "--password-stdin"]) == 0
    assert anna.get("/api/counts").status_code == 401
    assert admin.get("/api/counts").status_code == 200
    assert _login(anna, "anna", PW).status_code == 401
    assert _login(anna, "anna", "ganz-neues-passwort").status_code == 204


def test_login_brake_per_username(app_env):
    from test_main import _behind_proxy
    headers = {"X-Forwarded-Proto": "https", "Origin": "https://testserver"}
    with TestClient(_behind_proxy("172.19.0.1"), base_url="http://testserver") as c:
        state.db.create_user("anna", hash_password(PW))

        def attempt(user, pw, ip):
            return c.post("/api/login", json={"username": user, "password": pw},
                          headers={**headers, "X-Forwarded-For": ip})

        for i in range(30):  # rotierende IPs gegen einen Namen (MAX_USER_ATTEMPTS)
            assert attempt("anna", "falsch-falsch", f"203.0.113.{i}").status_code == 401
        assert attempt("anna", PW, "198.51.100.1").status_code == 429
        assert attempt("admin", ADMIN_PASSWORD, "198.51.100.1").status_code == 204


# ---- Rechte ---------------------------------------------------------------------

def test_only_admin_may_switch_sources_and_refresh(two):
    admin, anna = two
    assert anna.patch("/api/sources/gamma", json={"enabled": True}).status_code == 403
    assert anna.post("/api/sources/bulk", json={"enabled": ["gamma"]}).status_code == 403
    assert anna.post("/api/refresh").status_code == 403
    assert "gamma" not in state.db.enabled_source_ids()
    assert anna.get("/api/sources").status_code == 200  # lesen darf jeder
    assert admin.patch("/api/sources/gamma", json={"enabled": True}).status_code == 200
    assert admin.post("/api/sources/bulk", json={"disabled": ["gamma"]}).status_code == 200


def test_status_reduced_for_non_admins(two):
    admin, anna = two
    assert set(anna.get("/api/status").json()) == {"build", "fetcher"}
    assert {"build", "fetcher", "db", "settings"} <= set(admin.get("/api/status").json())


# ---- Kommandozeile ----------------------------------------------------------------

def _cli_env(monkeypatch, data_dir: Path) -> None:
    monkeypatch.setenv("KIOSK_DATA_DIR", str(data_dir))


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """Datenbank mit Admin "admin" (wie nach dem ersten Start) und Aufruf-Helfer für die CLI."""
    (tmp_path / "sources.yaml").write_text(
        (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8").split('SOURCES_YAML = """')[1].split('"""')[0],
        encoding="utf-8")
    monkeypatch.setenv("KIOSK_SOURCES", str(tmp_path / "sources.yaml"))
    monkeypatch.delenv("KIOSK_PASSWORD", raising=False)
    _cli_env(monkeypatch, tmp_path)
    db = Database(tmp_path / "kiosk.db", admin=("admin", ADMIN_PASSWORD))
    db.ensure_sources([("alpha", True), ("beta", True), ("gamma", False)])
    db.close()

    def run(*argv, stdin=""):
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        return users_cli.main(list(argv))

    def open_db():
        return Database(tmp_path / "kiosk.db")

    return run, open_db


def test_cli_add_list_and_validation(cli, capsys):
    run, open_db = cli
    assert run("add", "Anna", "--name", "Anna M.", "--password-stdin", stdin=PW + "\n") == 0
    assert "Benutzer anna angelegt" in capsys.readouterr().out
    db = open_db()
    try:
        anna = db.get_user_by_name("anna")
        assert anna["display_name"] == "Anna M." and anna["is_admin"] == 0
        assert verify_password(PW, anna["password_hash"]) and anna["password_hash"].startswith("scrypt$16384$8$1$")
        assert [p["builtin"] for p in db.list_profiles(anna["id"])] == ["de", "world", "business", "military"]
    finally:
        db.close()
    assert run("add", "anna", "--password-stdin", stdin=PW + "\n") == 1          # gibt es schon
    assert run("add", "a b", "--password-stdin", stdin=PW + "\n") == 1           # ungültiger Name
    assert run("add", "bert", "--password-stdin", stdin="kurz\n") == 1            # zu kurz
    assert run("add", "bert", "--password-stdin", stdin="bitte-aendern\n") == 1
    assert "mindestens 12 Zeichen" in capsys.readouterr().err
    assert run("add", "chef", "--admin", "--password-stdin", stdin=PW + "\n") == 0
    capsys.readouterr()
    assert run("list") == 0
    out = capsys.readouterr().out
    assert "anna" in out and "chef" in out and out.count("Admin") >= 2 and PW not in out


def test_cli_getpass(cli, monkeypatch):
    run, open_db = cli
    answers = iter([PW, PW + "x"])
    monkeypatch.setattr(users_cli.getpass, "getpass", lambda prompt="": next(answers))
    assert run("add", "anna") == 1  # Wiederholung passt nicht
    answers = iter([PW, PW])
    assert run("add", "anna") == 0


def test_cli_passwd_disable_enable(cli):
    run, open_db = cli
    run("add", "anna", "--password-stdin", stdin=PW + "\n")
    assert run("passwd", "anna", "--password-stdin", stdin="neues-passwort-99\n") == 0
    assert run("passwd", "niemand", "--password-stdin", stdin="neues-passwort-99\n") == 1
    assert run("disable", "anna") == 0
    db = open_db()
    try:
        anna = db.get_user_by_name("anna")
        assert verify_password("neues-passwort-99", anna["password_hash"])
        assert anna["disabled"] == 1 and anna["session_epoch"] == 3  # passwd und disable beenden Sitzungen
    finally:
        db.close()
    assert run("enable", "anna") == 0
    db = open_db()
    try:
        assert db.get_user_by_name("anna")["disabled"] == 0
    finally:
        db.close()


def test_cli_last_admin_is_protected(cli):
    run, open_db = cli
    assert run("remove", "admin", "--yes") == 1
    assert run("disable", "admin") == 1
    assert run("admin", "admin", "--off") == 1
    run("add", "anna", "--password-stdin", stdin=PW + "\n")
    assert run("admin", "anna", "--on") == 0
    assert run("admin", "admin", "--off") == 0      # jetzt gibt es einen anderen Admin
    assert run("disable", "anna") == 1               # anna ist jetzt die letzte aktive Admin
    assert run("admin", "admin", "--on") == 0
    assert run("disable", "anna") == 0
    assert run("remove", "admin", "--yes") == 1      # gesperrte Admins zählen nicht


def test_cli_remove_deletes_user_data_but_keeps_articles(cli):
    run, open_db = cli
    run("add", "anna", "--password-stdin", stdin=PW + "\n")
    db = open_db()
    try:
        uid = db.get_user_by_name("anna")["id"]
        db.upsert_articles([make_row("alpha", 1, ago(hours=1))])
        db.set_flag(uid, 1, "starred", True)
        db.put_favorite(uid, "alpha", None)
        db.set_settings(uid, {"theme": "light"})
    finally:
        db.close()
    assert run("remove", "anna") == 1  # ohne Terminal und ohne --yes: Abbruch
    assert run("remove", "anna", "--yes") == 0
    db = open_db()
    try:
        assert db.get_user_by_name("anna") is None
        for table in ("user_articles", "profiles", "favorite_sources", "settings"):
            assert db.one(f"SELECT COUNT(*) AS n FROM {table} WHERE user_id=?", (uid,))["n"] == 0, table
        assert db.one("SELECT COUNT(*) AS n FROM articles")["n"] == 1
    finally:
        db.close()


def test_cli_as_module_with_password_stdin(cli, tmp_path):
    env = {**os.environ, "KIOSK_DATA_DIR": str(tmp_path), "KIOSK_SOURCES": str(tmp_path / "sources.yaml")}
    env.pop("KIOSK_PASSWORD", None)
    r = subprocess.run([sys.executable, "-m", "app.users", "add", "bert", "--password-stdin"], cwd=ROOT,
                       env=env, input=PW + "\n", text=True, capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert PW not in r.stdout + r.stderr
    r = subprocess.run([sys.executable, "-m", "app.users", "list"], cwd=ROOT, env=env, text=True,
                       capture_output=True, timeout=60)
    assert r.returncode == 0 and "bert" in r.stdout


def test_cli_without_database(tmp_path, monkeypatch, capsys):
    _cli_env(monkeypatch, tmp_path / "leer")
    assert users_cli.main(["list"]) == 1
    assert "Keine Datenbank" in capsys.readouterr().err


# ---- deploy/user.sh ---------------------------------------------------------------

def _fake_ssh(tmp_path: Path) -> dict:
    """ssh-Attrappe: schreibt Argumente und stdin in Dateien statt zu verbinden."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{tmp_path}/args"\ncat > "{tmp_path}/stdin"\n', encoding="utf-8")
    (bin_dir / "ssh").chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "BOX": "testbox"}


def test_user_sh_syntax_and_forwarding(tmp_path):
    script = ROOT / "deploy" / "user.sh"
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    env = _fake_ssh(tmp_path)
    r = subprocess.run(["bash", str(script), "admin", "anna", "--on"], env=env, text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    args = (tmp_path / "args").read_text().splitlines()
    assert args[0] == "testbox"
    assert args[1] == "bash -c 'cd /opt/kiosk && docker compose exec -T kiosk python -m app.users admin anna --on '"
    r = subprocess.run(["bash", str(script), "remove", "anna", "--yes"], env=env, text=True, capture_output=True)
    assert r.returncode == 0 and "app.users remove anna --yes" in (tmp_path / "args").read_text()
    # Unbekannter Befehl: kein ssh
    (tmp_path / "args").unlink()
    r = subprocess.run(["bash", str(script), "drop", "anna"], env=env, text=True, capture_output=True)
    assert r.returncode == 2 and not (tmp_path / "args").exists()


def test_user_sh_quotes_for_any_login_shell_and_checks_username(tmp_path):
    """Der Befehl läuft per bash -c; die Login-Shell (hier sh) muss ihn nur als ein Wort weiterreichen."""
    script = ROOT / "deploy" / "user.sh"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # ssh-Attrappe: führt den entfernten Befehl mit sh aus, docker compose ... wird zu printf.
    (bin_dir / "ssh").write_text(r"""#!/usr/bin/env bash
cmd=${2/cd \/opt\/kiosk && docker compose exec -T kiosk python -m app.users/printf '[%s]\\n'}
sh -c "$cmd"
""", encoding="utf-8")
    (bin_dir / "ssh").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "BOX": "testbox"}
    tricky = "O'Brien \"x\" $HOME `id` ;ls"
    r = subprocess.run(["bash", str(script), "remove", "Anna", "--yes", tricky], env=env, text=True,
                       capture_output=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["[remove]", "[Anna]", "[--yes]", f"[{tricky}]"]
    for bad in ("a", "anna b", "x" * 33, "änna", "anna;ls"):
        r = subprocess.run(["bash", str(script), "disable", bad], env=env, text=True, capture_output=True)
        assert r.returncode == 2 and "Ungültiger Benutzername" in r.stderr and not r.stdout, bad


def test_cli_password_and_display_name_rules(cli, capsys):
    run, open_db = cli
    assert run("add", "bert", "--password-stdin", stdin="   kurz-rand   \n") == 1   # strip() < 12
    assert "mindestens 12 Zeichen" in capsys.readouterr().err
    assert run("add", "bert.langername", "--password-stdin", stdin="Bert.Langername\n") == 1
    assert "nicht der Benutzername" in capsys.readouterr().err
    assert run("add", "bert", "--name", "Bert\u200bx\x07", "--password-stdin", stdin=PW + "\n") == 1
    assert "Steuerzeichen" in capsys.readouterr().err
    assert run("add", "bert", "--name", "x" * 65, "--password-stdin", stdin=PW + "\n") == 1
    # NFC: "e" + kombinierender Akzent wird zu einem Zeichen
    assert run("add", "bert", "--name", "Rene\u0301", "--password-stdin", stdin=PW + "\n") == 0
    run("add", "carla", "--password-stdin", stdin=PW + "\n")
    assert run("passwd", "carla", "--password-stdin", stdin="carla\n") == 1
    db = open_db()
    try:
        assert db.get_user_by_name("bert")["display_name"] == "Ren\u00e9"
    finally:
        db.close()


def test_cli_add_without_admin_defaults_to_admin(tmp_path, monkeypatch, capsys):
    """Ohne aktiven Admin (und sogar ohne Datenbank) wird der erste Benutzer Admin."""
    monkeypatch.delenv("KIOSK_PASSWORD", raising=False)
    _cli_env(monkeypatch, tmp_path / "neu")
    monkeypatch.setattr(sys, "stdin", io.StringIO(PW + "\n"))
    assert users_cli.main(["add", "erste", "--password-stdin"]) == 0
    out = capsys.readouterr().out
    assert "wird Admin" in out and "(Admin)" in out
    monkeypatch.setattr(sys, "stdin", io.StringIO(PW + "\n"))
    assert users_cli.main(["add", "zweite", "--password-stdin"]) == 0
    assert "(Admin)" not in capsys.readouterr().out
    db = Database(tmp_path / "neu" / "kiosk.db")
    try:
        assert db.get_user_by_name("erste")["is_admin"] == 1 and db.get_user_by_name("zweite")["is_admin"] == 0
    finally:
        db.close()


@pytest.mark.parametrize("bad, msg", [("bitte-aendern", "Beispiel"), ("zu-kurz", "mindestens 12 Zeichen")])
def test_cli_rejects_invalid_kiosk_password(tmp_path, monkeypatch, capsys, bad, msg):
    """KIOSK_PASSWORD gilt in der Kommandozeile mit denselben Regeln wie beim Start der App:
    ungültig -> Abbruch, kein Admin (und hier nicht einmal eine Datenbank)."""
    monkeypatch.setenv("KIOSK_PASSWORD", bad)
    _cli_env(monkeypatch, tmp_path / "neu")
    monkeypatch.setattr(sys, "stdin", io.StringIO(PW + "\n"))
    assert users_cli.main(["add", "erste", "--password-stdin"]) == 1
    err = capsys.readouterr().err
    assert "KIOSK_PASSWORD" in err and msg in err
    assert not (tmp_path / "neu" / "kiosk.db").exists()


def test_cli_creates_admin_from_kiosk_password(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KIOSK_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("KIOSK_ADMIN_USER", "Chefin")
    _cli_env(monkeypatch, tmp_path / "neu")
    monkeypatch.setattr(sys, "stdin", io.StringIO(PW + "\n"))
    assert users_cli.main(["add", "anna", "--password-stdin"]) == 0
    out = capsys.readouterr().out
    assert "Admin 'chefin' aus KIOSK_ADMIN_USER/KIOSK_PASSWORD angelegt" in out
    assert "wird Admin" not in out and "(Admin)" not in out
    assert users_cli.main(["list"]) == 0
    assert "angelegt" not in capsys.readouterr().out   # nur beim ersten Mal


def test_cli_add_on_fresh_db_seeds_sources_first(tmp_path, monkeypatch):
    """Frische Datenbank ohne App-Start: "Wirtschaft" bekommt wie beim Seed der App die
    Wirtschaftsquellen (nicht alle Quellen)."""
    (tmp_path / "sources.yaml").write_text(
        "sources:\n"
        '  - {id: alpha, name: Alpha, country: DE, lang: de, feed: "https://a.example/f", homepage: "https://a.example"}\n'
        '  - {id: geld, name: Geld, country: DE, lang: de, category: business, feed: "https://g.example/f", homepage: "https://g.example"}\n'
        '  - {id: aus, name: Aus, country: GB, lang: en, category: business, default_enabled: false, feed: "https://x.example/f", homepage: "https://x.example"}\n',
        encoding="utf-8")
    monkeypatch.setenv("KIOSK_SOURCES", str(tmp_path / "sources.yaml"))
    monkeypatch.delenv("KIOSK_PASSWORD", raising=False)
    _cli_env(monkeypatch, tmp_path / "neu")
    monkeypatch.setattr(sys, "stdin", io.StringIO(PW + "\n"))
    assert users_cli.main(["add", "erste", "--password-stdin"]) == 0
    db = Database(tmp_path / "neu" / "kiosk.db")
    try:
        assert sorted(db.enabled_source_ids()) == ["alpha", "geld"]
        profiles = {p["builtin"]: p for p in db.list_profiles(db.get_user_by_name("erste")["id"])}
        assert profiles["business"]["sources"] == ["geld", "aus"]
        assert profiles["de"]["sources"] == ["alpha", "geld"]
    finally:
        db.close()


def test_source_counts_respect_retention(db):
    """Alte, nur von einem Benutzer gemerkte Artikel zählen in den Quellen-Zählern nur für ihn
    (total und unread_all; unread zählt ohnehin nur heute)."""
    other = db.create_user("anna", "x")
    db.upsert_articles([make_row("alpha", 1, ago(hours=1)), make_row("alpha", 2, ago(days=200))])
    new, old = [r["id"] for r in db.q("SELECT id FROM articles ORDER BY id")]
    db.set_flag(UID, old, "starred", True)
    db.retention_days = 90
    assert db.source_counts(UID)["alpha"] == {"total": 2, "unread": 1, "unread_all": 2}
    assert db.source_counts(other)["alpha"] == {"total": 1, "unread": 1, "unread_all": 1}
    db.retention_days = None   # ohne Grenze zählt alles
    assert db.source_counts(other)["alpha"] == {"total": 2, "unread": 1, "unread_all": 2}
