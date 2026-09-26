from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.db as db_mod
from app.main import app, state
from conftest import ago, make_row

ORIGIN = "https://testserver"


@pytest.fixture
def client(app_env):
    with TestClient(app, base_url=ORIGIN) as c:
        yield c


def login(c, password="geheim-und-lang", username="admin", **headers):
    return c.post("/api/login", json={"username": username, "password": password}, headers=headers)


@pytest.fixture
def authed(client):
    assert login(client).status_code == 204
    return client


def test_fetcher_disabled_in_tests(client):
    assert state.task is None and state.watchdog is None  # ohne Sammler kein Wachhund


def test_401_without_cookie(client):
    for path in ("/api/articles", "/api/sources", "/api/status", "/docs", "/openapi.json"):
        assert client.get(path).status_code == 401, path
    assert client.post("/api/refresh").status_code == 401
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/session").json() == {"authenticated": False, "user": None}


def test_static_page_has_no_inline_script(client):
    import re
    html = client.get("/").text
    scripts = re.findall(r"<script\b[^>]*>", html)
    assert scripts and all(" src=" in tag for tag in scripts)  # nur externe Skripte (CSP script-src 'self')
    assert "<style" not in html


def test_static_files_are_revalidated(client):
    for path in ("/", "/js/app.js", "/css/app.css", "/sw.js", "/manifest.webmanifest", "/js/theme-boot.js"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["cache-control"] == "no-cache", path
    assert client.get("/api/session").headers["cache-control"] == "no-store"
    assert client.get("/api/build").headers["cache-control"] == "no-store"


def test_favicon(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r.content[:4] == b"\x00\x00\x01\x00"
    assert r.headers["cache-control"] == "no-cache"


def test_build_is_open(client, monkeypatch):
    monkeypatch.setenv("KIOSK_BUILD", "20260926-1200")
    assert client.get("/api/build").json() == {"build": "20260926-1200"}


def test_login_sets_host_prefixed_secure_cookie(client):
    r = login(client)
    assert r.status_code == 204
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-kiosk_session=")
    low = cookie.lower()
    assert "secure" in low and "httponly" in low and "path=/" in low and "domain" not in low
    assert client.get("/api/session").json() == {
        "authenticated": True, "user": {"id": 1, "username": "admin", "display_name": "Admin", "is_admin": True}}
    assert client.get("/api/sources").status_code == 200


def test_login_insecure_cookie_for_local_dev(app_env, monkeypatch):
    monkeypatch.setenv("KIOSK_COOKIE_SECURE", "false")
    with TestClient(app, base_url="http://testserver") as c:
        r = login(c)
        assert r.headers["set-cookie"].startswith("kiosk_session=")
        assert "secure" not in r.headers["set-cookie"].lower()
        assert c.get("/api/sources").status_code == 200


def test_wrong_password_and_rate_limit(client):
    for _ in range(8):
        assert login(client, "falsch").status_code == 401
    assert login(client, "falsch").status_code == 429
    assert login(client).status_code == 429


def test_success_does_not_reset_ip_failures(client):
    """7 Fehlversuche + 1 erfolgreicher Login: Der Erfolg gibt nur die eigene Reservierung zurück,
    die IP hat danach noch 8 - 7 = 1 Versuch."""
    for _ in range(7):
        assert login(client, "falsch").status_code == 401
    assert login(client).status_code == 204
    assert login(client, "falsch").status_code == 401
    assert login(client, "falsch").status_code == 429
    assert login(client).status_code == 429


def test_login_brake_holds_under_parallel_requests(app_env, monkeypatch):
    """40 parallele Fehlversuche von einer IP: höchstens 8 echte scrypt-Prüfungen, höchstens zwei
    gleichzeitig, der Rest 429 (der Versuch wird vor dem await gezählt)."""
    import threading

    import httpx

    import app.main as main_mod
    calls, running, peak = [], [0], [0]
    lock = threading.Lock()
    real_verify, real_burn = main_mod.verify_password, main_mod.burn_time

    def counted(fn):
        def wrapper(*args):
            with lock:
                calls.append(1)
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            try:
                time.sleep(0.02)  # Prüfungen überlappen sicher
                return fn(*args)
            finally:
                with lock:
                    running[0] -= 1
        return wrapper

    monkeypatch.setattr(main_mod, "verify_password", counted(real_verify))
    monkeypatch.setattr(main_mod, "burn_time", counted(real_burn))

    async def run():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 1234))
            async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as c:
                async def bad(i):
                    r = await c.post("/api/login", json={"username": "admin" if i % 2 else f"nobody{i}",
                                                         "password": f"falsch-{i}"})
                    return r.status_code
                codes = await asyncio.gather(*[bad(i) for i in range(40)])
                after = (await c.post("/api/login", json={"username": "admin",
                                                          "password": "geheim-und-lang"})).status_code
                return codes, after

    codes, after = asyncio.run(run())
    assert codes.count(401) == len(calls) <= 8 and codes.count(429) == 40 - len(calls)
    assert codes.count(401) == 8
    assert peak[0] <= 2
    assert after == 429  # auch das richtige Passwort wartet, bis die IP-Sperre abläuft


def test_logout(authed):
    assert authed.post("/api/logout").status_code == 204
    assert authed.get("/api/sources").status_code == 401


def test_logout_revokes_copied_session(authed):
    token = authed.cookies.get("__Host-kiosk_session")
    assert authed.post("/api/logout").status_code == 204
    authed.cookies.set("__Host-kiosk_session", token)  # z. B. ein gestohlenes Cookie
    assert authed.get("/api/sources").status_code == 401
    assert state.db.get_user_by_name("admin")["session_epoch"] == 2
    authed.cookies.clear()
    assert login(authed).status_code == 204  # neue Anmeldung funktioniert wieder
    assert authed.get("/api/sources").status_code == 200


def test_session_survives_restart(app_env):
    with TestClient(app, base_url=ORIGIN) as c:
        login(c)
        token = c.cookies.get("__Host-kiosk_session")
    with TestClient(app, base_url=ORIGIN) as c:
        c.cookies.set("__Host-kiosk_session", token)
        assert c.get("/api/sources").status_code == 200


def test_secure_mode_ignores_plain_cookie(authed):
    token = authed.cookies.get("__Host-kiosk_session")
    authed.cookies.clear()
    authed.cookies.set("kiosk_session", token)
    assert authed.get("/api/sources").status_code == 401


def test_docs_disabled_by_default(authed):
    assert app.docs_url is None and app.openapi_url is None
    assert authed.get("/docs").status_code == 404
    assert authed.get("/openapi.json").status_code == 404


def test_healthz_checks_db_and_fetcher(client, monkeypatch):
    from dataclasses import replace
    assert client.get("/healthz").json() == {"ok": True}
    monkeypatch.setattr(state.db, "ping", lambda: False)
    r = client.get("/healthz")
    assert r.status_code == 503 and r.json()["failed"] == ["db"]
    monkeypatch.undo()

    class DeadTask:
        def done(self):
            return True

    monkeypatch.setattr(state, "settings", replace(state.settings, disable_fetcher=False))
    monkeypatch.setattr(state, "task", DeadTask())
    r = client.get("/healthz")
    assert r.status_code == 503 and r.json()["failed"] == ["fetcher"]
    monkeypatch.undo()  # vor dem Herunterfahren der App zurücksetzen


def test_healthz_detects_stalled_fetcher(client, monkeypatch):
    from dataclasses import replace

    class LiveTask:
        def done(self):
            return False

    monkeypatch.setattr(state, "settings", replace(state.settings, disable_fetcher=False, fetch_interval=60))
    monkeypatch.setattr(state, "task", LiveTask())
    limit = 3 * 60 + 600
    # Frisch gestartet, noch kein Lauf fertig: Schonfrist
    monkeypatch.setattr(state, "started_at", datetime.now(timezone.utc) - timedelta(seconds=limit - 30))
    monkeypatch.setitem(state.fetcher.state, "last_finished", None)
    assert client.get("/healthz").status_code == 200
    # Seit dem Start nie ein Lauf fertig geworden
    monkeypatch.setattr(state, "started_at", datetime.now(timezone.utc) - timedelta(seconds=limit + 30))
    r = client.get("/healthz")
    assert r.status_code == 503 and r.json()["failed"] == ["fetcher_stalled"]
    # Letzter Lauf kürzlich fertig: gesund, auch lange nach dem Start
    monkeypatch.setitem(state.fetcher.state, "last_finished", ago(seconds=30))
    assert client.get("/healthz").status_code == 200
    # Letzter Lauf zu lange her (Schleife lebt, hängt aber)
    monkeypatch.setitem(state.fetcher.state, "last_finished", ago(seconds=limit + 30))
    r = client.get("/healthz")
    assert r.status_code == 503 and r.json()["failed"] == ["fetcher_stalled"]
    monkeypatch.undo()


def test_stall_logic_running_and_idle():
    from app.main import RUN_MAX, fetcher_stalled

    now = datetime.now(timezone.utc)
    started = now - timedelta(days=1)
    limit = timedelta(seconds=3 * 900 + 600)

    def iso(dt):
        return dt.isoformat()

    # Lauf aktiv: nur der Beginn des Laufs zählt, gegen RUN_MAX (kein Fehlalarm bei langen Läufen).
    long_run = {"running": True, "last_started": iso(now - timedelta(hours=2)),
                "last_finished": iso(now - timedelta(hours=5))}
    assert not fetcher_stalled(long_run, started, 900, now)
    hung_run = dict(long_run, last_started=iso(now - RUN_MAX - timedelta(minutes=1)))
    assert fetcher_stalled(hung_run, started, 900, now)
    # Kein Lauf aktiv: Ende des letzten Laufs gegen 3 Intervalle + 10 min.
    idle = {"running": False, "last_started": None}
    assert not fetcher_stalled(dict(idle, last_finished=iso(now - limit + timedelta(seconds=30))), started, 900, now)
    assert fetcher_stalled(dict(idle, last_finished=iso(now - limit - timedelta(seconds=30))), started, 900, now)
    # Noch nie ein Lauf fertig: Start der App zählt.
    assert not fetcher_stalled(dict(idle, last_finished=None), now - timedelta(minutes=5), 900, now)
    assert fetcher_stalled(dict(idle, last_finished=None), now - limit - timedelta(seconds=1), 900, now)
    assert not fetcher_stalled(dict(idle, last_finished=None), None, 900, now)


def test_healthz_long_running_run_is_not_stalled(client, monkeypatch):
    from dataclasses import replace

    class LiveTask:
        def done(self):
            return False

    monkeypatch.setattr(state, "settings", replace(state.settings, disable_fetcher=False, fetch_interval=60))
    monkeypatch.setattr(state, "task", LiveTask())
    monkeypatch.setitem(state.fetcher.state, "running", True)
    monkeypatch.setitem(state.fetcher.state, "last_started", ago(hours=1))
    monkeypatch.setitem(state.fetcher.state, "last_finished", ago(hours=2))
    assert client.get("/healthz").status_code == 200
    monkeypatch.setitem(state.fetcher.state, "last_started", ago(hours=3, minutes=1))
    r = client.get("/healthz")
    assert r.status_code == 503 and r.json()["failed"] == ["fetcher_stalled"]
    monkeypatch.undo()


def test_watchdog_exits_only_on_fetcher_problem(client, monkeypatch):
    import asyncio
    from dataclasses import replace

    import app.main as main_mod

    exits: list[int] = []
    monkeypatch.setattr(main_mod, "WATCHDOG_INTERVAL", 0.01)
    monkeypatch.setattr(main_mod, "_hard_exit", exits.append)

    class Task:
        def __init__(self, dead):
            self.dead = dead

        def done(self):
            return self.dead

    monkeypatch.setattr(state, "settings", replace(state.settings, disable_fetcher=False, fetch_interval=60))
    monkeypatch.setitem(state.fetcher.state, "running", False)
    monkeypatch.setitem(state.fetcher.state, "last_finished", ago(seconds=10))
    monkeypatch.setattr(state, "task", Task(dead=False))
    assert main_mod._fetcher_problem() is None
    with pytest.raises(asyncio.TimeoutError):  # gesund: läuft weiter, kein Exit
        asyncio.run(asyncio.wait_for(main_mod._watchdog(), 0.1))
    assert exits == []

    monkeypatch.setitem(state.fetcher.state, "last_finished", ago(hours=2))
    assert main_mod._fetcher_problem() == "fetcher_stalled"
    asyncio.run(asyncio.wait_for(main_mod._watchdog(), 1))
    assert exits == [3]

    monkeypatch.setitem(state.fetcher.state, "last_finished", ago(seconds=10))
    monkeypatch.setattr(state, "task", Task(dead=True))
    assert main_mod._fetcher_problem() == "fetcher"
    # Ohne Sammler (KIOSK_DISABLE_FETCHER) nie ein Problem.
    monkeypatch.setattr(state, "settings", replace(state.settings, disable_fetcher=True))
    assert main_mod._fetcher_problem() is None
    monkeypatch.undo()


@pytest.mark.parametrize("password,message", [
    ("bitte-aendern", "Beispiel"), ("elf-zeichen", "mindestens 12 Zeichen"),
])
def test_config_rejects_weak_passwords(app_env, monkeypatch, password, message):
    from app.config import load_settings
    monkeypatch.setenv("KIOSK_PASSWORD", password)
    with pytest.raises(RuntimeError, match=message):
        load_settings()


def test_config_accepts_twelve_char_password(app_env, monkeypatch):
    from app.config import load_settings
    monkeypatch.setenv("KIOSK_PASSWORD", "zwoelf-zeich")
    assert load_settings().password == "zwoelf-zeich"


def test_status_includes_build(authed, monkeypatch):
    assert "build" in authed.get("/api/status").json()
    monkeypatch.setenv("KIOSK_BUILD", "20260925T120000Z")
    assert authed.get("/api/status").json()["build"] == "20260925T120000Z"


def test_source_id_lists_are_capped(authed):
    many = [f"s{i}" for i in range(201)]
    assert authed.get("/api/articles", params={"source": ",".join(many)}).status_code == 400
    assert authed.get("/api/articles", params={"source": ",".join(many[:200])}).status_code == 200
    assert authed.post("/api/articles/read-all", json={"sources": many}).status_code == 400
    assert authed.post("/api/sources/bulk", json={"enabled": many}).status_code == 400
    assert authed.post("/api/sources/bulk", json={"disabled": many}).status_code == 400


@pytest.mark.parametrize("var,value", [
    ("KIOSK_FETCH_INTERVAL", "59"), ("KIOSK_FETCH_INTERVAL", "0"),
    ("KIOSK_EXTRACT_CONCURRENCY", "0"), ("KIOSK_EXTRACT_CONCURRENCY", "33"),
    ("KIOSK_RETENTION_DAYS", "0"), ("KIOSK_RETENTION_DAYS", "3651"),
    ("KIOSK_SESSION_DAYS", "0"), ("KIOSK_SESSION_DAYS", "366"),
    ("KIOSK_FETCH_INTERVAL", "15m"),
])
def test_config_rejects_out_of_range_values(app_env, monkeypatch, var, value):
    from app.config import load_settings
    monkeypatch.setenv(var, value)
    with pytest.raises(RuntimeError, match=var):
        load_settings()


def test_config_accepts_bounds(app_env, monkeypatch):
    from app.config import load_settings
    for var, value in [("KIOSK_FETCH_INTERVAL", "60"), ("KIOSK_EXTRACT_CONCURRENCY", "32"),
                       ("KIOSK_RETENTION_DAYS", "3650"), ("KIOSK_SESSION_DAYS", "365")]:
        monkeypatch.setenv(var, value)
    s = load_settings()
    assert (s.fetch_interval, s.extract_concurrency, s.retention_days, s.session_max_age) == (60, 32, 3650, 365 * 86400)


def test_kiosk_password_only_bootstraps_the_admin(app_env, monkeypatch, caplog):
    """Gibt es schon Benutzer, hat KIOSK_PASSWORD keine Wirkung mehr (mit Hinweis im Log)."""
    with TestClient(app, base_url=ORIGIN) as c:
        login(c)
        token = c.cookies.get("__Host-kiosk_session")
    monkeypatch.setenv("KIOSK_PASSWORD", "anderes-passwort")
    with caplog.at_level("INFO", logger="kiosk"), TestClient(app, base_url=ORIGIN) as c:
        assert "KIOSK_PASSWORD ist gesetzt, wird aber ignoriert" in caplog.text
        c.cookies.set("__Host-kiosk_session", token)
        assert c.get("/api/sources").status_code == 200
        assert login(c, "anderes-passwort").status_code == 401
        assert login(c).status_code == 204
    monkeypatch.setenv("KIOSK_PASSWORD", "")
    with TestClient(app, base_url=ORIGIN) as c:  # ohne KIOSK_PASSWORD startet die App weiter
        assert login(c).status_code == 204


def test_fresh_database_without_password_does_not_start(app_env, monkeypatch):
    monkeypatch.setenv("KIOSK_PASSWORD", "")
    with pytest.raises(RuntimeError, match="Keine Benutzer"):
        with TestClient(app, base_url=ORIGIN):
            pass


def test_admin_user_from_env(app_env, monkeypatch):
    monkeypatch.setenv("KIOSK_ADMIN_USER", "Chefin")
    with TestClient(app, base_url=ORIGIN) as c:
        assert login(c).status_code == 401
        assert login(c, username="chefin").status_code == 204
        assert c.get("/api/me").json()["username"] == "chefin"
    monkeypatch.setenv("KIOSK_ADMIN_USER", "nicht erlaubt!")
    from app.config import load_settings
    with pytest.raises(RuntimeError, match="KIOSK_ADMIN_USER"):
        load_settings()


# ---- CSRF -----------------------------------------------------------------

def test_csrf_foreign_origin_rejected(authed):
    r = authed.post("/api/refresh", headers={"Origin": "https://evil.203-0-113-10.sslip.io"})
    assert r.status_code == 403
    assert authed.post("/api/articles/read-all", headers={"Origin": "http://testserver"}).status_code == 403
    assert authed.post("/api/articles/read-all", headers={"Origin": "null"}).status_code == 403
    assert login(authed, Origin="https://evil.example").status_code == 403


def test_csrf_same_origin_allowed(authed):
    assert authed.post("/api/articles/read-all", headers={"Origin": ORIGIN}).status_code == 200
    assert authed.post("/api/articles/read-all", headers={"Origin": "https://testserver:443"}).status_code == 200


def test_csrf_sec_fetch_site(authed):
    assert authed.post("/api/articles/read-all", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert authed.post("/api/articles/read-all", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert authed.post("/api/articles/read-all", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert authed.post("/api/articles/read-all", headers={"Sec-Fetch-Site": "none"}).status_code == 200


def test_csrf_no_headers_allowed_for_scripts(authed):
    assert authed.post("/api/articles/read-all").status_code == 200


def test_csrf_not_applied_to_get(authed):
    assert authed.get("/api/sources", headers={"Origin": "https://evil.example"}).status_code == 200


def test_csrf_honors_forwarded_host_as_seen_by_app(app_env):
    # Hinter Caddy sieht die App Host und (dank --proxy-headers) das Schema des Originals.
    with TestClient(app, base_url="https://kiosk.example") as c:
        assert login(c, Origin="https://kiosk.example").status_code == 204
        assert login(c, Origin="http://kiosk.example").status_code == 403


# ---- Artikel ----------------------------------------------------------------

def _seed(n=7):
    rows = [make_row("alpha", i, ago(hours=i % 3), title=f"Wahl {i}") for i in range(n)]
    state.db.upsert_articles(rows)


def test_article_pagination_cursor(authed):
    _seed()
    seen, before = [], None
    while True:
        params = {"limit": 3, **({"before": before} if before else {})}
        body = authed.get("/api/articles", params=params).json()
        seen += [(it["published_at"], it["id"]) for it in body["items"]]
        before = body["next_before"]
        assert body["next_offset"] is None
        if not before:
            break
        pub, _, aid = before.rpartition("|")
        assert (pub, int(aid)) == seen[-1]
    assert seen == sorted(seen, reverse=True) and len(seen) == 7


def test_article_pagination_legacy_integer_cursor(authed):
    _seed()
    first = authed.get("/api/articles", params={"limit": 3}).json()["items"]
    legacy = authed.get("/api/articles", params={"limit": 3, "before": str(first[-1]["id"])}).json()
    modern = authed.get("/api/articles", params={"limit": 3, "before": f'{first[-1]["published_at"]}|{first[-1]["id"]}'}).json()
    assert legacy["items"] == modern["items"]
    assert authed.get("/api/articles", params={"before": "999999"}).status_code == 400
    assert authed.get("/api/articles", params={"before": "kaputt"}).status_code == 400


def test_search_offset_pagination(authed):
    _seed()
    ids, offset = [], 0
    while offset is not None:
        body = authed.get("/api/articles", params={"q": "wahl", "limit": 3, "offset": offset}).json()
        assert body["next_before"] is None
        ids += [it["id"] for it in body["items"]]
        offset = body["next_offset"]
    assert len(ids) == 7 and len(set(ids)) == 7


def test_search_with_quotes_is_fine(authed):
    _seed(2)
    assert authed.get("/api/articles", params={"q": 'wahl" OR x NEAR('}).status_code == 200


def test_bad_fts_query_returns_400(authed, monkeypatch):
    monkeypatch.setattr(db_mod, "_fts_query", lambda raw: raw)  # Escaping aus: rohe FTS5-Syntax
    _seed(1)
    assert authed.get("/api/articles", params={"q": '"unbalanciert'}).status_code == 400


def test_get_article_and_flags(authed):
    _seed(1)
    aid = state.db.one("SELECT id FROM articles")["id"]
    state.db.store_extraction(aid, status="full", body_html="<p>x</p>", body_text="x", body_len=1, paywalled=False)
    item = authed.get(f"/api/articles/{aid}").json()
    assert item["source_name"] == "Alpha" and item["body_text"] == "x"
    assert authed.post(f"/api/articles/{aid}/star", json={"value": True}).json()["starred"] is True
    assert authed.get("/api/articles", params={"starred": True}).json()["items"][0]["id"] == aid
    assert authed.get("/api/articles/999999").status_code == 404


def test_cursor_with_unencoded_plus_and_canonical_form(authed):
    _seed()
    first = authed.get("/api/articles", params={"limit": 3}).json()
    pub, aid = first["items"][-1]["published_at"], first["items"][-1]["id"]
    expected = authed.get("/api/articles", params={"limit": 3, "before": first["next_before"]}).json()["items"]
    assert expected
    # Unkodiertes "+" kommt als Leerzeichen an
    raw_plus = authed.get(f"/api/articles?limit=3&before={pub}|{aid}").json()
    assert raw_plus["items"] == expected
    # Andere Schreibweise desselben Zeitpunkts wird in das gespeicherte Format gebracht
    other_tz = (datetime.fromisoformat(pub).astimezone(timezone(timedelta(hours=2)))).isoformat()
    assert authed.get("/api/articles", params={"limit": 3, "before": f"{other_tz}|{aid}"}).json()["items"] == expected


@pytest.mark.parametrize("before", ["²", "12²", "2026-09-01T10:00:00+00:00|²", "2026-09-01T10:00:00+00:00|١٢",
                                    "gestern|5", "2026-13-01T10:00:00|5", "|5", "0001-01-01T00:00:00+01:00|5"])
def test_bad_cursors_are_400(authed, before):
    _seed(1)
    assert authed.get("/api/articles", params={"before": before}).status_code == 400


def test_huge_ids_are_client_errors(authed):
    _seed(1)
    huge = "99999999999999999999"
    assert authed.get(f"/api/articles/{huge}").status_code == 422
    assert authed.get("/api/articles/0").status_code == 422
    assert authed.post(f"/api/articles/{huge}/read", json={"value": True}).status_code == 422
    assert authed.post(f"/api/articles/{huge}/star", json={"value": True}).status_code == 422
    for before in (huge, f"2026-09-01T10:00:00+00:00|{huge}"):
        assert authed.get("/api/articles", params={"before": before}).status_code == 400
    # Größte gültige ID: normal unbekannt
    assert authed.get(f"/api/articles/{2**63 - 1}").status_code == 404
    assert authed.get("/api/articles", params={"before": str(2**63 - 1)}).status_code == 400


# ---- Extraktion auf Anforderung ---------------------------------------------

@pytest.fixture
def extract_calls(monkeypatch):
    calls = []

    def fake(article_id, url):
        calls.append((article_id, True))

    monkeypatch.setattr(state.fetcher, "start_extraction", fake)
    return calls


def test_article_detail_does_not_block_on_extraction(authed, monkeypatch):
    """Ein fälliger pending-Artikel startet die Extraktion im Hintergrund; die Antwort kommt sofort."""
    _seed(1)
    aid = state.db.one("SELECT id FROM articles")["id"]

    release = []

    async def hang(article_id, url):  # hängt, bis der Test ihn freigibt
        while not release:
            await asyncio.sleep(0.01)
        state.db.store_extraction(article_id, status="full", body_html="<p>x</p>", body_text="x",
                                  body_len=1, paywalled=False)

    monkeypatch.setattr(state.fetcher, "_extract_priority", hang)
    t0 = time.monotonic()
    r = authed.get(f"/api/articles/{aid}")
    assert time.monotonic() - t0 < 5
    body = r.json()
    assert r.status_code == 200 and body["text_status"] == "pending" and body["extracting"] is True
    assert body["title"] == "Wahl 0"
    assert state.fetcher.is_extracting(aid)
    # Zweiter Abruf während der laufenden Extraktion: kein zweiter Start, weiterhin pending.
    assert authed.get(f"/api/articles/{aid}").json()["extracting"] is True
    # Fertig extrahiert: extracting=false und der echte Status.
    release.append(True)
    deadline = time.monotonic() + 5
    while state.fetcher.is_extracting(aid) and time.monotonic() < deadline:
        time.sleep(0.02)
    body = authed.get(f"/api/articles/{aid}").json()
    assert body["extracting"] is False and body["text_status"] == "full"


def test_on_demand_extraction_respects_schedule(authed, extract_calls):
    _seed(1)
    aid = state.db.one("SELECT id FROM articles")["id"]
    state.db.schedule_retry(aid, "HTTP 503")  # nächster Versuch in 1 h
    authed.get(f"/api/articles/{aid}")
    authed.get(f"/api/articles/{aid}", params={"retry": True})  # pending: retry übergeht den Plan nicht
    assert extract_calls == []
    state.db.q("UPDATE articles SET next_attempt_at=? WHERE id=?", (ago(seconds=5), aid))
    authed.get(f"/api/articles/{aid}")
    assert extract_calls == [(aid, True)]


def test_retry_overrides_schedule_for_finished_articles(authed, extract_calls):
    _seed(1)
    aid = state.db.one("SELECT id FROM articles")["id"]
    state.db.store_extraction(aid, status="partial", body_html="<p>a</p>", body_text="a", body_len=1, paywalled=True)
    state.db.schedule_retry(aid, "HTTP 503")
    assert authed.get(f"/api/articles/{aid}").json()["text_status"] == "partial"
    assert extract_calls == []
    authed.get(f"/api/articles/{aid}", params={"retry": True})
    assert extract_calls == [(aid, True)]
    state.db.store_extraction(aid, status="full", body_html="<p>a</p>", body_text="a", body_len=1, paywalled=False)
    authed.get(f"/api/articles/{aid}", params={"retry": True})  # "full" wird nie neu geholt
    assert len(extract_calls) == 1


# ---- Quellen ------------------------------------------------------------------

def test_patch_source(authed):
    r = authed.patch("/api/sources/gamma", json={"enabled": True})
    assert r.status_code == 200 and r.json()["id"] == "gamma" and r.json()["enabled"] is True
    assert "gamma" in state.db.enabled_source_ids()
    assert authed.patch("/api/sources/gibtsnicht", json={"enabled": True}).status_code == 404
    assert authed.patch("/api/sources/alpha", json={}).status_code == 422
    assert authed.patch("/api/sources/alpha", json={"enabled": False}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_bulk_sources(authed):
    r = authed.post("/api/sources/bulk", json={"enabled": ["gamma", "gibtsnicht"], "disabled": ["alpha"]})
    assert r.status_code == 200
    enabled = {s["id"]: s["enabled"] for s in r.json()}
    assert enabled == {"alpha": False, "beta": True, "gamma": True}
    assert set(state.db.enabled_source_ids()) == {"beta", "gamma"}


# ---- Echte Proxy-Header-Kette (uvicorn ProxyHeadersMiddleware wie im Container) ---

def _forwarded_allow_ips() -> str:
    """Liest --forwarded-allow-ips aus der CMD-Zeile des Dockerfiles (so wie im Container)."""
    import json
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text(encoding="utf-8")
    m = re.search(r"^CMD (\[.*?\])\s*$", text.replace("\\\n", ""), re.M | re.S)
    assert m, "CMD-Zeile im Dockerfile nicht gefunden"
    args = json.loads(m.group(1))
    return args[args.index("--forwarded-allow-ips") + 1]


def test_dockerfile_trusts_only_loopback_and_gateway():
    assert _forwarded_allow_ips() == "127.0.0.1,172.19.0.1"


def _behind_proxy(peer: str):
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    inner = ProxyHeadersMiddleware(app, trusted_hosts=_forwarded_allow_ips())

    async def with_peer(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (peer, 40000)}
        await inner(scope, receive, send)

    return with_peer


def test_proxy_chain_from_docker_gateway(app_env):
    headers = {"X-Forwarded-Proto": "https", "Origin": "https://testserver"}
    with TestClient(_behind_proxy("172.19.0.1"), base_url="http://testserver") as c:
        assert login(c, **headers, **{"X-Forwarded-For": "203.0.113.7"}).status_code == 204
        for i in range(8):  # wechselnde Namen: greift die Bremse pro IP, nicht die pro Benutzername
            assert login(c, "falsch", f"x{i}", **headers, **{"X-Forwarded-For": "203.0.113.7"}).status_code == 401
        assert login(c, **headers, **{"X-Forwarded-For": "203.0.113.7"}).status_code == 429
        # Andere Client-IP hinter demselben Proxy ist nicht betroffen
        assert login(c, **headers, **{"X-Forwarded-For": "203.0.113.8"}).status_code == 204


@pytest.mark.parametrize("peer", ["192.0.2.10", "172.17.0.1", "172.19.0.5"])
def test_proxy_headers_ignored_from_untrusted_peer(app_env, peer):
    with TestClient(_behind_proxy(peer), base_url="http://testserver") as c:
        r = login(c, **{"X-Forwarded-Proto": "https", "Origin": "https://testserver", "X-Forwarded-For": "203.0.113.7"})
        assert r.status_code == 403  # Schema bleibt http, Origin passt nicht


def test_font_and_manifest_content_types(client):
    """Schriften und Manifest muessen den richtigen MIME-Typ tragen (schlankes Image ohne mime.types)."""
    r = client.get("/fonts/doto-700-latin.woff2")
    assert r.status_code == 200 and r.headers["content-type"].startswith("font/woff2")
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
