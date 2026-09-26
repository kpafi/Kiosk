"""Routen für die Oberfläche (Plan Abschnitt 2): Profile, Lieblingszeitungen, Leiste,
Einstellungen, Zähler sowie die Erweiterungen der Artikelliste."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.api_ui as api_ui
from app.main import app, state
from conftest import UID, ago, make_row

ORIGIN = "https://testserver"


@pytest.fixture
def authed(app_env):
    with TestClient(app, base_url=ORIGIN) as c:
        assert c.post("/api/login", json={"username": "admin", "password": "geheim-und-lang"}).status_code == 204
        yield c


def _unread(cols: str = "a.title", where: str = "1") -> list:
    """Ungelesene Artikel des Admins (UID) direkt aus der Datenbank."""
    return [r[0] for r in state.db.q(
        f"SELECT {cols} FROM articles a LEFT JOIN user_articles ua ON ua.article_id=a.id AND ua.user_id=? "
        f"WHERE COALESCE(ua.read, 0)=0 AND {where} ORDER BY a.id", (UID,))]


def _set_read(where: str, value: int = 1) -> None:
    for aid in state.db.q(f"SELECT id FROM articles WHERE {where}"):
        state.db.set_flag(UID, aid[0], "read", bool(value))


def _ids(body):
    return [it["id"] for it in body["items"]]


def _profile(c, builtin):
    return next(p for p in c.get("/api/profiles").json() if p["builtin"] == builtin)


def _seed_terms() -> None:
    """10 Artikel, abwechselnd alpha/beta, viele mit gleichem Zeitstempel; jeder dritte zum Thema Inflation."""
    same = ago(hours=2)
    rows = []
    for i in range(10):
        title = f"Inflation steigt {i}" if i % 3 == 0 else f"Wetter {i}"
        src = "alpha" if i % 2 == 0 else "beta"
        rows.append(make_row(src, i, same if i < 6 else ago(hours=i), title=title))
    state.db.upsert_articles(rows)


# ---- vorgegebene Profile -----------------------------------------------------

def test_builtin_profiles_are_seeded(authed):
    profiles = authed.get("/api/profiles").json()
    assert [p["name"] for p in profiles] == ["Deutschland", "Weltweit", "Wirtschaft", "Militär"]
    assert [p["position"] for p in profiles] == [0, 1, 2, 3]
    de, world, business, military = profiles
    assert de["builtin"] == "de" and de["sources"] == ["alpha"] and de["in_bar"] is True
    assert de["source_count"] == 1 and de["terms"] == []
    assert world["sources"] is None and world["source_count"] == 2  # alpha, beta aktiv; gamma aus
    # Keine Wirtschaftsquelle im Testkatalog: alle Quellen, gefiltert über die Begriffe
    assert business["sources"] is None and "Inflation" in business["terms"] and business["in_bar"] is True
    assert military["in_bar"] is False and "NATO" in military["terms"]
    assert set(de) >= {"id", "name", "builtin", "sources", "terms", "in_bar", "position", "source_count", "unread"}


def test_seeding_happens_only_once(app_env):
    with TestClient(app, base_url=ORIGIN):
        state.db.delete_profile(UID, state.db.list_profiles(UID)[-1]["id"])  # direkt in der DB
    with TestClient(app, base_url=ORIGIN):
        assert len(state.db.list_profiles(UID)) == 3  # nicht leer: kein erneutes Säen


def test_business_profile_uses_business_sources_when_active(app_env):
    yaml = (app_env / "sources.yaml").read_text(encoding="utf-8")
    yaml += ('  - {id: money, name: Money Times, country: GB, lang: en, category: business, '
             'feed: "https://money.example/feed", homepage: "https://money.example"}\n')
    (app_env / "sources.yaml").write_text(yaml, encoding="utf-8")
    with TestClient(app, base_url=ORIGIN):
        business = next(p for p in state.db.list_profiles(UID) if p["builtin"] == "business")
        assert business["sources"] == ["money"]


# ---- Artikelliste: Profil, Begriffe, Keyset ------------------------------------

def test_profile_terms_keyset_paging_without_gaps_or_duplicates(authed):
    _seed_terms()
    business = _profile(authed, "business")
    expected = sorted(
        ((r["published_at"], r["id"]) for r in state.db.q("SELECT id, published_at, title FROM articles")
         if r["title"].startswith("Inflation")), reverse=True)
    seen, before = [], None
    while True:
        params = {"profile": business["id"], "limit": 2, **({"before": before} if before else {})}
        body = authed.get("/api/articles", params=params).json()
        seen += [(it["published_at"], it["id"]) for it in body["items"]]
        assert body["next_offset"] is None and body["total"] is None
        before = body["next_before"]
        if not before:
            break
    assert seen == expected and len(expected) == 4


def test_profile_sources_and_disabled_sources(authed):
    _seed_terms()
    de = _profile(authed, "de")
    items = authed.get("/api/articles", params={"profile": de["id"]}).json()["items"]
    assert items and {it["source_id"] for it in items} == {"alpha"}
    # Deaktivierte Quelle fällt aus dem Profil
    authed.patch("/api/sources/alpha", json={"enabled": False})
    assert authed.get("/api/articles", params={"profile": de["id"]}).json()["items"] == []
    assert _profile(authed, "de")["source_count"] == 0
    assert authed.get("/api/articles", params={"profile": 999}).status_code == 404


def test_profile_combined_with_query_uses_offset(authed):
    _seed_terms()
    business = _profile(authed, "business")
    body = authed.get("/api/articles", params={"profile": business["id"], "q": "steigt", "limit": 3}).json()
    assert len(body["items"]) == 3 and body["next_offset"] == 3 and body["next_before"] is None
    assert body["total"] == 4


def test_search_total_is_capped(authed, monkeypatch):
    _seed_terms()
    body = authed.get("/api/articles", params={"q": "wetter", "limit": 2}).json()
    assert body["total"] == 6 and len(body["items"]) == 2
    real = state.db.count_articles
    monkeypatch.setattr(state.db, "count_articles", lambda uid, **kw: real(uid, **{**kw, "cap": 3}))
    assert authed.get("/api/articles", params={"q": "wetter"}).json()["total"] == 3


def test_days_filter(authed):
    state.db.upsert_articles([make_row("alpha", 1, ago(hours=5)), make_row("alpha", 2, ago(days=3)),
                              make_row("alpha", 3, ago(days=20))])
    titles = lambda d: {it["title"] for it in authed.get("/api/articles", params={"days": d}).json()["items"]}  # noqa: E731
    assert titles(1) == {"Titel 1"}
    assert titles(7) == {"Titel 1", "Titel 2"}
    assert titles(30) == {"Titel 1", "Titel 2", "Titel 3"}
    assert authed.get("/api/articles", params={"days": 0}).status_code == 422
    assert authed.get("/api/articles", params={"q": "titel", "days": 1}).json()["total"] == 1


def test_later_flag_list_and_counts(authed):
    state.db.upsert_articles([make_row("alpha", i, ago(hours=i)) for i in range(3)])
    aid = state.db.one("SELECT id FROM articles WHERE guid='alpha-1'")["id"]
    r = authed.post(f"/api/articles/{aid}/later", json={"value": True})
    assert r.json() == {"id": aid, "later": True}
    body = authed.get("/api/articles", params={"later": True}).json()
    assert _ids(body) == [aid] and body["items"][0]["later"] == 1
    assert authed.get("/api/counts").json()["later"] == 1
    authed.post(f"/api/articles/{aid}/later", json={"value": False})
    assert authed.get("/api/articles", params={"later": True}).json()["items"] == []
    assert authed.post("/api/articles/999999/later", json={"value": True}).status_code == 404
    assert authed.post(f"/api/articles/{aid}/later", json={"value": True},
                       headers={"Origin": "https://evil.example"}).status_code == 403


def test_paywalled_always_hidden(authed):
    state.db.upsert_articles([make_row("alpha", 1, ago(hours=1)), make_row("alpha", 2, ago(hours=2))])
    state.db.q("UPDATE articles SET paywalled=1 WHERE guid='alpha-2'")
    assert len(authed.get("/api/articles").json()["items"]) == 1           # Standard: ohne Paywall
    assert len(authed.get("/api/articles", params={"free": False}).json()["items"]) == 2  # nur ausdrücklich
    # free_only=false wird angenommen, ändert aber nichts an den Listen
    r = authed.patch("/api/settings", json={"free_only": False})
    assert r.status_code == 200 and r.json()["free_only"] is False
    assert len(authed.get("/api/articles").json()["items"]) == 1
    newest = state.db.q("SELECT id FROM articles WHERE guid='alpha-1'")[0]["id"]
    assert authed.get(f"/api/articles/{newest}/next", params={"unread": False}).json()["item"] is None
    assert len(authed.get("/api/articles", params={"free": True}).json()["items"]) == 1


def test_read_minutes(authed):
    state.db.upsert_articles([make_row("alpha", i, ago(hours=i)) for i in range(3)])
    ids = [r["id"] for r in state.db.q("SELECT id FROM articles ORDER BY id")]
    for aid, n in zip(ids[1:], (300, 5000)):
        state.db.store_extraction(aid, status="full", body_html="<p>x</p>", body_text="x", body_len=n,
                                  paywalled=False)
    items = {it["id"]: it["read_minutes"] for it in authed.get("/api/articles").json()["items"]}
    assert items == {ids[0]: 0, ids[1]: 1, ids[2]: 5}
    assert authed.get(f"/api/articles/{ids[2]}").json()["read_minutes"] == 5


# ---- nächster Artikel -------------------------------------------------------

def test_next_in_profile_context(authed):
    _seed_terms()
    business = _profile(authed, "business")
    order = _ids(authed.get("/api/articles", params={"profile": business["id"]}).json())
    assert len(order) == 4
    nxt = authed.get(f"/api/articles/{order[0]}/next", params={"profile": business["id"]}).json()
    assert nxt["item"]["id"] == order[1] and nxt["item"]["read_minutes"] == 0
    # Ungelesen ist Standard: ein gelesener Artikel wird übersprungen
    authed.post(f"/api/articles/{order[1]}/read", json={"value": True})
    assert authed.get(f"/api/articles/{order[0]}/next", params={"profile": business["id"]}).json()["item"]["id"] == order[2]
    assert authed.get(f"/api/articles/{order[0]}/next",
                      params={"profile": business["id"], "unread": False}).json()["item"]["id"] == order[1]
    assert authed.get(f"/api/articles/{order[-1]}/next", params={"profile": business["id"]}).json() == {"item": None}
    assert authed.get("/api/articles/999999/next").status_code == 404


def test_next_with_source(authed):
    _seed_terms()
    beta = _ids(authed.get("/api/articles", params={"source": "beta"}).json())
    nxt = authed.get(f"/api/articles/{beta[0]}/next", params={"source": "beta"}).json()["item"]
    assert nxt["id"] == beta[1] and nxt["source_id"] == "beta"


# ---- Profile: Schreiben ---------------------------------------------------------

def test_profile_crud_and_validation(authed):
    r = authed.post("/api/profiles", json={"name": " Klima ", "sources": ["alpha", "beta", "alpha"],
                                           "terms": ["Klima", "Hitze", "Klima"], "in_bar": True})
    assert r.status_code == 201
    p = r.json()
    assert (p["name"], p["sources"], p["terms"], p["in_bar"], p["builtin"], p["position"]) == \
        ("Klima", ["alpha", "beta"], ["Klima", "Hitze"], True, None, 4)
    assert p["source_count"] == 2 and p["unread"] == 0
    bad = [
        {"name": ""}, {"name": "x" * 41}, {"name": "x", "sources": ["gibtsnicht"]},
        {"name": "x", "sources": [f"s{i}" for i in range(201)]},
        {"name": "x", "terms": ['a"b']}, {"name": "x", "terms": ["t"] * 21}, {"name": "x", "terms": ["x" * 41]},
        {"name": "x", "terms": [""]}, {"name": "x", "in_bar": "ja"}, {"name": "x", "extra": 1},
        {"name": "x", "sources": []},
    ]
    for body in bad:
        assert authed.post("/api/profiles", json=body).status_code == 422, body
    # PATCH: Teilfelder, sources null = alle Quellen
    r = authed.patch(f"/api/profiles/{p['id']}", json={"sources": None, "in_bar": False})
    assert r.status_code == 200 and r.json()["sources"] is None and r.json()["name"] == "Klima"
    assert r.json()["in_bar"] is False and r.json()["terms"] == ["Klima", "Hitze"]
    assert authed.patch(f"/api/profiles/{p['id']}", json={"name": None}).status_code == 422
    assert authed.patch(f"/api/profiles/{p['id']}", json={"terms": None}).status_code == 422
    assert authed.patch(f"/api/profiles/{p['id']}", json={"sources": []}).status_code == 422
    assert authed.patch("/api/profiles/999", json={"name": "x"}).status_code == 404
    # Vorgegebene Profile: änderbar, nicht löschbar
    de = _profile(authed, "de")
    assert authed.patch(f"/api/profiles/{de['id']}", json={"name": "Inland"}).json()["name"] == "Inland"
    assert authed.delete(f"/api/profiles/{de['id']}").status_code == 409
    assert authed.delete(f"/api/profiles/{p['id']}").status_code == 204
    assert authed.delete(f"/api/profiles/{p['id']}").status_code == 404
    assert len(authed.get("/api/profiles").json()) == 4


def test_reorder_profiles(authed):
    ids = [p["id"] for p in authed.get("/api/profiles").json()]
    r = authed.post("/api/profiles/reorder", json={"ids": [ids[3], ids[1]]})
    assert r.status_code == 200
    assert [p["id"] for p in r.json()] == [ids[3], ids[1], ids[0], ids[2]]
    assert [p["position"] for p in r.json()] == [0, 1, 2, 3]
    assert authed.post("/api/profiles/reorder", json={"ids": [ids[0], ids[0]]}).status_code == 400
    assert authed.post("/api/profiles/reorder", json={"ids": [999]}).status_code == 400


def test_profile_unread_is_cached_and_invalidated(authed, monkeypatch):
    _seed_terms()
    business = _profile(authed, "business")
    assert business["unread"] == 4
    _set_read("title LIKE 'Inflation%' AND source_id='alpha'")
    assert _profile(authed, "business")["unread"] == 4           # aus dem Zwischenspeicher
    monkeypatch.setattr(api_ui, "UNREAD_TTL", 0.0)
    assert _profile(authed, "business")["unread"] == 2            # abgelaufen
    monkeypatch.setattr(api_ui, "UNREAD_TTL", 60.0)
    _set_read("1", 0)
    monkeypatch.setitem(state.fetcher.state, "last_finished", ago(seconds=0))  # Sammellauf beendet
    assert _profile(authed, "business")["unread"] == 4
    authed.post("/api/articles/read-all", json={"profile": business["id"]})
    assert _profile(authed, "business")["unread"] == 0            # read-all leert den Speicher


def test_profile_unread_cap(authed, monkeypatch):
    _seed_terms()
    monkeypatch.setattr(api_ui, "UNREAD_CAP", 3)
    api_ui.invalidate_unread()
    assert _profile(authed, "world")["unread"] == 3


# ---- read-all mit Kontext -----------------------------------------------------

def test_read_all_scoped_to_profile_and_source(authed):
    _seed_terms()
    business = _profile(authed, "business")
    assert authed.post("/api/articles/read-all", json={"profile": business["id"]}).json() == {"marked": 4}
    unread = set(_unread())
    assert all(t.startswith("Wetter") for t in unread) and len(unread) == 6
    assert authed.post("/api/articles/read-all", json={"source": "beta"}).json()["marked"] == 3
    assert set(_unread("a.source_id")) == {"alpha"}
    assert authed.post("/api/articles/read-all", json={"source": "beta", "sources": ["alpha"]}).json()["marked"] == 0
    assert authed.post("/api/articles/read-all", json={"profile": 999}).status_code == 404
    assert authed.post("/api/articles/read-all", json={}).json()["marked"] == 3


def test_read_all_max_id_and_paywalled_count(authed):
    state.db.upsert_articles([make_row("alpha", i, ago(hours=10 - i)) for i in range(4)])
    ids = [r["id"] for r in state.db.q("SELECT id FROM articles ORDER BY id")]
    state.db.q("UPDATE articles SET paywalled=1 WHERE id=?", (ids[0],))
    # Nur bis max_id; der Paywall-Artikel wird markiert, zählt aber nicht mit
    assert authed.post("/api/articles/read-all", json={"max_id": ids[2]}).json() == {"marked": 2}
    assert _unread("a.id") == [ids[3]]
    assert authed.post("/api/articles/read-all", json={"max_id": 0}).status_code == 422
    assert authed.post("/api/articles/read-all", json={"max_id": ids[3]}).json() == {"marked": 1}


def test_read_flag_invalidates_profile_unread_and_source_counts(authed):
    state.db.upsert_articles([make_row("alpha", i, ago(hours=i + 1)) for i in range(3)])
    ids = [r["id"] for r in state.db.q("SELECT id FROM articles ORDER BY id")]
    state.db.q("UPDATE articles SET paywalled=1 WHERE id=?", (ids[2],))
    assert _profile(authed, "de")["unread"] == 2
    authed.post(f"/api/articles/{ids[0]}/read", json={"value": True})
    assert _profile(authed, "de")["unread"] == 1          # sofort, nicht erst nach 60 s
    sources = {s["id"]: s for s in authed.get("/api/sources").json()}
    assert sources["alpha"]["unread"] == 1 and sources["alpha"]["total"] == 2  # ohne Paywall


# ---- Lieblingszeitungen und Leiste --------------------------------------------

def test_favorites_crud_and_sources_flags(authed):
    state.db.upsert_articles([make_row("beta", 1, ago(hours=1)), make_row("beta", 2, ago(hours=2))])
    state.db.q("UPDATE articles SET paywalled=1 WHERE guid='beta-2'")
    assert authed.get("/api/favorites/sources").json() == []
    r = authed.put("/api/favorites/sources/beta", json={})
    assert r.status_code == 200
    assert r.json() == {"source_id": "beta", "name": "Beta", "short": "BETA", "country": "GB",
                        "enabled": True, "in_bar": True, "position": 0, "unread": 1}
    authed.put("/api/favorites/sources/alpha", json={"in_bar": False})
    assert authed.put("/api/favorites/sources/gibtsnicht", json={}).status_code == 404
    favs = authed.get("/api/favorites/sources").json()
    assert [(f["source_id"], f["in_bar"], f["position"]) for f in favs] == [("beta", True, 0), ("alpha", False, 1)]
    # Ändern ohne in_bar lässt den Wert stehen; mit in_bar ändert er sich
    assert authed.put("/api/favorites/sources/alpha").json()["in_bar"] is False
    assert authed.put("/api/favorites/sources/alpha", json={"in_bar": True}).json()["in_bar"] is True
    sources = {s["id"]: s for s in authed.get("/api/sources").json()}
    assert sources["beta"]["is_favorite"] is True and sources["gamma"]["is_favorite"] is False
    assert sources["alpha"]["short"] == "ALPHA"
    r = authed.post("/api/favorites/sources/reorder", json={"ids": ["alpha"]})
    assert [f["source_id"] for f in r.json()] == ["alpha", "beta"]
    assert authed.post("/api/favorites/sources/reorder", json={"ids": ["gamma"]}).status_code == 400
    assert authed.delete("/api/favorites/sources/beta").status_code == 204
    assert [f["source_id"] for f in authed.get("/api/favorites/sources").json()] == ["alpha"]
    assert authed.get("/api/counts").json()["favorites"] == 1


def test_bar_order(authed):
    state.db.upsert_articles([make_row("beta", 1, ago(hours=1)), make_row("alpha", 1, ago(hours=1))])
    profiles = {p["builtin"]: p["id"] for p in authed.get("/api/profiles").json()}
    authed.put("/api/favorites/sources/beta", json={})
    authed.put("/api/favorites/sources/alpha", json={"in_bar": False})
    keys = lambda body: [it["key"] for it in body["items"]]  # noqa: E731
    body = authed.get("/api/bar").json()
    # Ohne gespeicherte Reihenfolge: ALLE, Quellen, dann Profile (in_bar=1: Deutschland, Wirtschaft)
    assert keys(body) == ["all", "source:beta", f"profile:{profiles['de']}", f"profile:{profiles['business']}"]
    assert body["items"][0] == {"type": "all", "key": "all", "label": "ALLE", "unread": 2}
    assert body["items"][1] == {"type": "source", "id": "beta", "key": "source:beta", "label": "BETA", "unread": 1}
    assert body["items"][2]["label"] == "DEUTSCHLAND" and body["items"][2]["unread"] == 1
    # Gemeinsame Reihenfolge; fehlende Einträge hinten, nicht in der Leiste stehende ignoriert
    order = [f"profile:{profiles['business']}", "source:beta", "source:alpha"]
    r = authed.patch("/api/bar", json={"order": order})
    assert r.status_code == 200
    assert keys(r.json()) == ["all", f"profile:{profiles['business']}", "source:beta", f"profile:{profiles['de']}"]
    assert authed.get("/api/settings").json()["bar_order"] == order
    for bad in (["quelle:beta"], ["source:"], ["profile:x"], "source:beta", [1]):
        assert authed.patch("/api/bar", json={"order": bad}).status_code == 422, bad
    # Unbekannte Zeitungen oder Profile: 400, gespeicherte Reihenfolge bleibt
    for bad in (["profile:999"], ["source:gibtsnicht", "source:beta"]):
        assert authed.patch("/api/bar", json={"order": bad}).status_code == 400, bad
    assert authed.get("/api/settings").json()["bar_order"] == order
    # Profil aus der Leiste nehmen
    authed.patch(f"/api/profiles/{profiles['business']}", json={"in_bar": False})
    assert keys(authed.get("/api/bar").json()) == ["all", "source:beta", f"profile:{profiles['de']}"]


# ---- Einstellungen und Zähler ----------------------------------------------------

def test_settings_defaults_and_validation(authed):
    assert authed.get("/api/settings").json() == {
        "theme": "dark", "text_size": "m", "mark_read_on_open": True, "show_images": True,
        "free_only": True, "bar_order": []}
    assert authed.patch("/api/settings", json={"theme": "system"}).json()["theme"] == "system"
    r = authed.patch("/api/settings", json={"theme": "light", "show_images": False})
    assert r.status_code == 200 and r.json()["theme"] == "light" and r.json()["show_images"] is False
    assert r.json()["text_size"] == "m"
    for bad in ({"theme": "blau"}, {"text_size": "xl"}, {"mark_read_on_open": "yes"}, {"free_only": 1},
                {"unbekannt": True}, {"theme": None}, {"bar_order": ["x"]}):
        assert authed.patch("/api/settings", json=bad).status_code == 422, bad
    assert authed.get("/api/settings").json()["theme"] == "light"
    # Kaputter gespeicherter Wert: Standard gilt
    state.db.q("INSERT OR REPLACE INTO settings (user_id, key, value) VALUES (?, 'text_size', '42')", (UID,))
    assert authed.get("/api/settings").json()["text_size"] == "m"


def test_counts(authed):
    state.db.upsert_articles([make_row("alpha", i, ago(hours=i)) for i in range(4)])
    ids = [r["id"] for r in state.db.q("SELECT id FROM articles ORDER BY id")]
    state.db.q("UPDATE articles SET paywalled=1 WHERE id=?", (ids[0],))
    state.db.set_flag(UID, ids[0], "starred", True)
    state.db.set_flag(UID, ids[1], "read", True)
    state.db.set_flag(UID, ids[2], "starred", True)
    state.db.record_fetch("beta", ok=False, error="HTTP 500")
    state.db.record_fetch("gamma", ok=False, error="HTTP 500")  # aus: zählt nicht als fehlerhaft
    assert authed.get("/api/counts").json() == {
        # starred zählt auch den Bezahlartikel: Gemerkt zeigt ihn (mit Paywall-Hinweis)
        "unread": 2, "later": 0, "starred": 2, "profiles": 4, "favorites": 0,
        "sources_enabled": 2, "sources_total": 3, "sources_failing": 1}
    authed.patch("/api/settings", json={"free_only": False})
    assert authed.get("/api/counts").json()["starred"] == 2   # free_only wirkt nicht mehr


def test_new_routes_require_login(app_env):
    with TestClient(app, base_url=ORIGIN) as c:
        for path in ("/api/profiles", "/api/favorites/sources", "/api/bar", "/api/settings", "/api/counts",
                     "/api/articles/1/next"):
            assert c.get(path).status_code == 401, path
        assert c.patch("/api/settings", json={"theme": "dark"}).status_code == 401


def test_writes_reject_foreign_origin(authed):
    evil = {"Origin": "https://evil.example"}
    assert authed.post("/api/profiles", json={"name": "x"}, headers=evil).status_code == 403
    assert authed.patch("/api/settings", json={"theme": "dark"}, headers=evil).status_code == 403
    assert authed.put("/api/favorites/sources/alpha", json={}, headers=evil).status_code == 403
    assert authed.patch("/api/bar", json={"order": []}, headers=evil).status_code == 403



# ---- ALLE nur aus aktivierten Quellen ------------------------------------------

def test_disabled_source_leaves_all_list_counts_bar_next_and_read_all(authed):
    state.db.upsert_articles([make_row("alpha", 1, ago(hours=1)), make_row("beta", 2, ago(hours=2)),
                              make_row("alpha", 3, ago(hours=3))])
    ids = {r["guid"]: r["id"] for r in state.db.q("SELECT id, guid FROM articles")}
    assert len(authed.get("/api/articles").json()["items"]) == 3
    assert authed.patch("/api/sources/beta", json={"enabled": False}).status_code == 200

    items = authed.get("/api/articles").json()["items"]
    assert {it["source_id"] for it in items} == {"alpha"}
    assert authed.get("/api/counts").json()["unread"] == 2
    assert authed.get("/api/bar").json()["items"][0]["unread"] == 2
    # Nächster nach alpha-1 überspringt beta-2
    nxt = authed.get(f"/api/articles/{ids['alpha-1']}/next").json()["item"]
    assert nxt["id"] == ids["alpha-3"]
    # Ausdrücklich gewählte Quelle bleibt abrufbar
    assert len(authed.get("/api/articles", params={"source": "beta"}).json()["items"]) == 1
    # read-all ohne Kontext markiert nur aktivierte Quellen
    assert authed.post("/api/articles/read-all", json={}).json()["marked"] == 2
    assert _unread("a.id", "a.source_id='beta'") == [ids["beta-2"]]


# ---- Wasserstand für read-all --------------------------------------------------

def test_articles_response_carries_global_max_id_for_read_all(authed):
    state.db.upsert_articles([make_row("alpha", i, ago(hours=i + 1)) for i in range(3)])
    body = authed.get("/api/articles", params={"source": "beta"}).json()   # leere Liste
    assert body["items"] == [] and body["max_id"] == state.db.max_article_id() > 0
    watermark = body["max_id"]
    state.db.upsert_articles([make_row("alpha", 9, ago(minutes=1))])        # kommt später dazu
    assert authed.post("/api/articles/read-all", json={"max_id": watermark}).json()["marked"] == 3
    assert authed.get("/api/counts").json()["unread"] == 1
    assert authed.get("/api/articles").json()["max_id"] == watermark + 1


# ---- Bezahlartikel ---------------------------------------------------------------

def _paywalled_article() -> int:
    state.db.upsert_articles([make_row("alpha", 1, ago(hours=1), summary="Anriss")])
    aid = state.db.q("SELECT id FROM articles")[0]["id"]
    state.db.store_extraction(aid, status="partial", body_html="<p>Geheim</p>", body_text="Geheim",
                              body_len=6, paywalled=True)
    return aid


def test_paywalled_article_detail_has_no_text(authed):
    aid = _paywalled_article()
    item = authed.get(f"/api/articles/{aid}").json()
    assert item["paywalled"] == 1 and item["body_html"] is None and item["body_text"] is None
    assert item["summary"] == "" and item["read_minutes"] == 0 and item["url"]


def test_saved_lists_keep_paywalled_articles(authed):
    aid = _paywalled_article()
    state.db.set_flag(UID, aid, "starred", True)
    state.db.set_flag(UID, aid, "later", True)
    assert _ids(authed.get("/api/articles").json()) == []
    assert _ids(authed.get("/api/articles", params={"starred": "true"}).json()) == [aid]
    assert _ids(authed.get("/api/articles", params={"later": "true"}).json()) == [aid]
    counts = authed.get("/api/counts").json()
    assert counts["starred"] == 1 and counts["later"] == 1 and counts["unread"] == 0


def test_source_total_and_status_unread_exclude_paywalled(authed):
    _paywalled_article()
    state.db.upsert_articles([make_row("alpha", 2, ago(hours=2))])
    alpha = next(s for s in authed.get("/api/sources").json() if s["id"] == "alpha")
    assert alpha["total"] == 1 and alpha["unread"] == 1
    assert authed.get("/api/status").json()["db"]["unread"] == 1


# ---- Einstellungen, HEAD, Cache -------------------------------------------------

def test_settings_bar_order_rejects_unknown_keys(authed):
    assert authed.patch("/api/settings", json={"bar_order": ["source:nope"]}).status_code == 400
    assert authed.patch("/api/settings", json={"bar_order": ["profile:999"]}).status_code == 400
    r = authed.patch("/api/settings", json={"bar_order": ["source:alpha"]})
    assert r.status_code == 200 and r.json()["bar_order"] == ["source:alpha"]


def test_head_for_favicon_and_build_and_no_store_on_api(authed):
    for path in ("/favicon.ico", "/api/build"):
        assert authed.head(path).status_code == 200, path
    assert authed.get("/api/counts").headers["cache-control"] == "no-store"


def test_head_for_healthz_and_get_api_routes(authed):
    r = authed.head("/healthz")
    assert r.status_code == 200 and r.content == b""
    for path in ("/api/session", "/api/counts", "/api/status", "/api/articles", "/api/sources"):
        r = authed.head(path)
        assert r.status_code == 200 and r.content == b"", path
        assert int(r.headers["content-length"]) > 0, path
    # Nur GET-Routen: HEAD auf eine reine POST-Route löst nichts aus
    assert authed.head("/api/refresh").status_code in (404, 405)
    assert state.fetcher.state.get("running") is not True


def test_saved_list_hides_summary_of_paywalled_articles(authed):
    state.db.upsert_articles([make_row("alpha", 1, ago(hours=1)), make_row("alpha", 2, ago(hours=2))])
    ids = {r["guid"]: r["id"] for r in state.db.q("SELECT id, guid FROM articles")}
    state.db.store_extraction(ids["alpha-1"], status="full", body_html=None, body_text=None, body_len=0,
                              paywalled=True)
    for aid in ids.values():
        state.db.set_flag(UID, aid, "later", True)
    items = {it["id"]: it for it in authed.get("/api/articles", params={"later": "true"}).json()["items"]}
    assert items[ids["alpha-1"]]["paywalled"] == 1 and items[ids["alpha-1"]]["summary"] == ""
    assert items[ids["alpha-2"]]["summary"] == "Zusammenfassung 2"
