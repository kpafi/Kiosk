"""Routen für die Oberfläche (Plan Abschnitt 2): Profile, Lieblingszeitungen, Leiste,
Einstellungen, Zähler sowie die Erweiterungen der Artikelliste."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    assert set(de) >= {"id", "name", "builtin", "sources", "terms", "in_bar", "in_chips", "position",
                       "source_count", "unread"}
    assert all(p["in_chips"] is True for p in profiles)  # vorgegebene Profile oben bei den Filtern


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
    assert p["source_count"] == 2 and p["unread"] == 0 and p["in_chips"] is True  # Standard: oben
    bad = [
        {"name": ""}, {"name": "x" * 41}, {"name": "x", "sources": ["gibtsnicht"]},
        {"name": "x", "sources": [f"s{i}" for i in range(201)]},
        {"name": "x", "terms": ['a"b']}, {"name": "x", "terms": ["t"] * 21}, {"name": "x", "terms": ["x" * 41]},
        {"name": "x", "terms": [""]}, {"name": "x", "in_bar": "ja"}, {"name": "x", "extra": 1},
        {"name": "x", "sources": []}, {"name": "x", "in_chips": "ja"}, {"name": "x", "in_chips": 1},
        {"name": "x", "in_chips": None},
    ]
    for body in bad:
        assert authed.post("/api/profiles", json=body).status_code == 422, body
    # PATCH: Teilfelder, sources null = alle Quellen
    r = authed.patch(f"/api/profiles/{p['id']}", json={"sources": None, "in_bar": False})
    assert r.status_code == 200 and r.json()["sources"] is None and r.json()["name"] == "Klima"
    assert r.json()["in_bar"] is False and r.json()["terms"] == ["Klima", "Hitze"]
    assert authed.patch(f"/api/profiles/{p['id']}", json={"name": None}).status_code == 422
    assert authed.patch(f"/api/profiles/{p['id']}", json={"terms": None}).status_code == 422
    for bad in (None, "nein", 0):
        assert authed.patch(f"/api/profiles/{p['id']}", json={"in_chips": bad}).status_code == 422, bad
    assert authed.patch(f"/api/profiles/{p['id']}", json={"sources": []}).status_code == 422
    assert authed.patch("/api/profiles/999", json={"name": "x"}).status_code == 404
    # Vorgegebene Profile: änderbar, nicht löschbar
    de = _profile(authed, "de")
    assert authed.patch(f"/api/profiles/{de['id']}", json={"name": "Inland"}).json()["name"] == "Inland"
    assert authed.delete(f"/api/profiles/{de['id']}").status_code == 409
    assert authed.delete(f"/api/profiles/{p['id']}").status_code == 204
    assert authed.delete(f"/api/profiles/{p['id']}").status_code == 404
    assert len(authed.get("/api/profiles").json()) == 4


def test_profile_in_chips_is_independent_of_in_bar(authed):
    """Oben bei den Filtern (in_chips) und unten in der Leiste (in_bar) sind getrennte Schalter."""
    r = authed.post("/api/profiles", json={"name": "Nur unten", "in_bar": True, "in_chips": False})
    assert r.status_code == 201 and (r.json()["in_bar"], r.json()["in_chips"]) == (True, False)
    pid = r.json()["id"]
    # PATCH nur eines Schalters lässt den anderen stehen
    r = authed.patch(f"/api/profiles/{pid}", json={"in_chips": True})
    assert r.status_code == 200 and (r.json()["in_bar"], r.json()["in_chips"]) == (True, True)
    r = authed.patch(f"/api/profiles/{pid}", json={"in_bar": False})
    assert (r.json()["in_bar"], r.json()["in_chips"]) == (False, True)
    r = authed.patch(f"/api/profiles/{pid}", json={"in_chips": False, "name": "Versteckt"})
    assert (r.json()["name"], r.json()["in_bar"], r.json()["in_chips"]) == ("Versteckt", False, False)
    # Liste und Umsortieren liefern den Wert mit
    listed = {p["id"]: p for p in authed.get("/api/profiles").json()}
    assert listed[pid]["in_chips"] is False
    reordered = authed.post("/api/profiles/reorder", json={"ids": [pid]}).json()
    assert reordered[0]["id"] == pid and reordered[0]["in_chips"] is False
    # Die Leiste richtet sich nur nach in_bar
    authed.patch(f"/api/profiles/{pid}", json={"in_bar": True})
    assert f"profile:{pid}" in [it["key"] for it in authed.get("/api/bar").json()["items"]]


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


def test_profile_unread_cache_ends_with_the_day(authed, monkeypatch):
    """Nach Mitternacht kein Wert von gestern aus dem Zwischenspeicher, auch innerhalb der 60 s."""
    _seed_terms()                                                  # Artikel von vor 2 bis 9 Stunden
    assert _profile(authed, "business")["unread"] == 4
    monkeypatch.setattr(state.db, "today_start", lambda: ago(minutes=1))   # neuer Tag
    assert _profile(authed, "business")["unread"] == 0
    monkeypatch.setattr(state.db, "today_start", lambda: ago(hours=5))
    assert _profile(authed, "business")["unread"] == 2           # Inflation 0 und 3; 6 und 9 sind älter


def test_counters_count_today_lists_keep_all_unread(authed, monkeypatch):
    """Zähler nur ab Tagesbeginn; Ungelesen-Liste, „Nächster ungelesener“ und „alles gelesen“
    sowie unread_all in /api/sources weiter mit allen ungelesenen Artikeln."""
    state.db.upsert_articles([make_row("alpha", 1, ago(hours=1)), make_row("alpha", 2, ago(hours=5)),
                              make_row("beta", 3, ago(hours=6))])
    monkeypatch.setattr(state.db, "today_start", lambda: ago(hours=3))
    authed.put("/api/favorites/sources/alpha", json={})
    sources = {s["id"]: s for s in authed.get("/api/sources").json()}
    assert (sources["alpha"]["total"], sources["alpha"]["unread"], sources["alpha"]["unread_all"]) == (2, 1, 2)
    assert (sources["beta"]["unread"], sources["beta"]["unread_all"]) == (0, 1)
    assert (sources["gamma"]["unread"], sources["gamma"]["unread_all"]) == (0, 0)   # ohne Artikel
    bar = {i["key"]: i["unread"] for i in authed.get("/api/bar").json()["items"]}
    assert bar["all"] == 1 and bar["source:alpha"] == 1
    assert authed.get("/api/counts").json()["unread"] == 1
    assert authed.get("/api/favorites/sources").json()[0]["unread"] == 1
    assert _profile(authed, "de")["unread"] == 1 and _profile(authed, "world")["unread"] == 1
    assert authed.get("/api/status").json()["db"]["unread"] == 3
    assert len(authed.get("/api/articles", params={"unread": "true"}).json()["items"]) == 3
    newest = authed.get("/api/articles").json()["items"][0]["id"]
    assert authed.get(f"/api/articles/{newest}/next").json()["item"]["title"] == "Titel 2"   # von „gestern“
    assert authed.post("/api/articles/read-all", json={"source": "alpha"}).json() == {"marked": 2}
    assert authed.patch("/api/sources/alpha", json={"enabled": True}).json()["unread_all"] == 0


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
        "free_only": True, "bar_order": [], "desktop_empty": "overview", "focus_mode": False}
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


def test_profiles_per_user_are_capped(authed):
    existing = len(authed.get("/api/profiles").json())
    for i in range(api_ui.MAX_PROFILES - existing):
        assert authed.post("/api/profiles", json={"name": f"P{i}"}).status_code == 201
    r = authed.post("/api/profiles", json={"name": "zu viel"})
    assert r.status_code == 409 and f"Höchstens {api_ui.MAX_PROFILES} Profile" in r.json()["detail"]
    pid = authed.get("/api/profiles").json()[-1]["id"]
    assert authed.delete(f"/api/profiles/{pid}").status_code == 204
    assert authed.post("/api/profiles", json={"name": "wieder Platz"}).status_code == 201


def test_too_expensive_profile_counter_is_null_and_not_retried(authed, monkeypatch):
    from app.db import QueryTimeout
    api_ui.invalidate_unread()
    calls = []

    def slow(*a, time_limit=None, **kw):
        calls.append(time_limit)
        raise QueryTimeout("Abfrage nach 2 s abgebrochen")

    monkeypatch.setattr(state.db, "count_articles", slow)
    r = authed.get("/api/profiles")
    assert r.status_code == 200 and [p["unread"] for p in r.json()] == [None] * 4
    assert len(calls) == 4
    # Markiert als zu teuer: weder die Liste noch die Leiste noch ein Lesestand-Wechsel zählen neu.
    assert authed.get("/api/profiles").status_code == 200
    bar = authed.get("/api/bar").json()["items"]
    assert [it["unread"] for it in bar if it["type"] == "profile"] == [None, None]
    api_ui.invalidate_unread(UID)
    authed.get("/api/profiles")
    assert len(calls) == 4
    # Wer das Profil ändert, bekommt einen neuen Versuch.
    pid = authed.get("/api/profiles").json()[0]["id"]
    authed.patch(f"/api/profiles/{pid}", json={"name": "Neu"})
    assert len(calls) == 5


def test_profile_counts_share_one_time_budget(authed, monkeypatch):
    from types import SimpleNamespace
    from app.db import QueryTimeout
    api_ui.invalidate_unread()
    monkeypatch.setattr(api_ui, "PROFILE_COUNT_BUDGET", 0.3)
    # Simulierte Uhr statt echter Wartezeit: Der Test hängt nicht von der Last des Rechners ab.
    clock = [1000.0]
    monkeypatch.setattr(api_ui, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def count(*a, time_limit=None, **kw):
        if time_limit < 0.2:    # die Zählung braucht 0,2 s
            clock[0] += time_limit
            raise QueryTimeout("Abfrage abgebrochen")
        clock[0] += 0.2
        return 5

    monkeypatch.setattr(state.db, "count_articles", count)
    first = [p["unread"] for p in authed.get("/api/profiles").json()]
    assert first == [5, None, None, None]
    # Nicht Gezähltes wird nicht als „zu teuer“ gemerkt: Die nächste Anfrage zählt weiter.
    second = [p["unread"] for p in authed.get("/api/profiles").json()]
    assert second == [5, 5, None, None]


# ---- in_all: false (Quelle nur ausdrücklich, nicht in ALLE) ------------------------

@pytest.fixture
def sec_client(app_env):
    """Katalog mit zusätzlicher Quelle sec (DE, in_all: false) und je einem Artikel von alpha, beta, sec."""
    from conftest import SOURCES_YAML
    (app_env / "sources.yaml").write_text(
        SOURCES_YAML + '  - {id: sec, name: Sec, country: DE, lang: de, feed: "https://sec.example/feed", '
        'homepage: "https://sec.example", in_all: false}\n', encoding="utf-8")
    with TestClient(app, base_url=ORIGIN) as c:
        assert c.post("/api/login", json={"username": "admin", "password": "geheim-und-lang"}).status_code == 204
        state.db.upsert_articles([make_row("alpha", 1, ago(hours=1), title="Patchday alpha"),
                                  make_row("sec", 1, ago(hours=2), title="Patchday sec"),
                                  make_row("beta", 1, ago(hours=3), title="Wetter beta")])
        yield c


def _sources_of(body) -> list[str]:
    return [it["source_id"] for it in body["items"]]


def test_in_all_false_source_is_not_in_all_list_counts_next_or_read_all(sec_client):
    c = sec_client
    ids = {r["guid"]: r["id"] for r in state.db.q("SELECT id, guid FROM articles")}
    assert _sources_of(c.get("/api/articles").json()) == ["alpha", "beta"]
    assert c.get("/api/counts").json()["unread"] == 2
    assert c.get("/api/counts").json()["sources_enabled"] == 3          # geholt wird sec trotzdem
    assert c.get("/api/bar").json()["items"][0]["unread"] == 2
    assert c.get(f"/api/articles/{ids['alpha-1']}/next").json()["item"]["id"] == ids["beta-1"]
    assert c.post("/api/articles/read-all", json={}).json()["marked"] == 2
    assert _unread("a.source_id") == ["sec"]


def test_in_all_false_source_is_visible_when_named(sec_client):
    c = sec_client
    assert _sources_of(c.get("/api/articles", params={"source": "sec"}).json()) == ["sec"]
    assert _sources_of(c.get("/api/articles", params={"source": "sec,alpha"}).json()) == ["alpha", "sec"]
    sec = next(s for s in c.get("/api/sources").json() if s["id"] == "sec")
    assert sec["in_all"] is False and sec["unread"] == 1 and sec["enabled"] is True
    assert next(s for s in c.get("/api/sources").json() if s["id"] == "alpha")["in_all"] is True
    # Lieblingszeitung mit Chip in der Leiste: eigener Zähler
    assert c.put("/api/favorites/sources/sec", json={"in_bar": True}).status_code == 200
    chip = next(it for it in c.get("/api/bar").json()["items"] if it["key"] == "source:sec")
    assert chip["unread"] == 1
    assert c.post("/api/articles/read-all", json={"source": "sec"}).json()["marked"] == 1
    assert _unread("a.source_id") == ["alpha", "beta"]


def test_in_all_false_source_in_profiles(sec_client):
    c = sec_client
    de, world = _profile(c, "de"), _profile(c, "world")
    assert de["sources"] == ["alpha"]                     # Deutschland lässt sec beim Anlegen weg
    assert world["sources"] is None and world["source_count"] == 2
    assert _sources_of(c.get("/api/articles", params={"profile": world["id"]}).json()) == ["alpha", "beta"]
    assert world["unread"] == 2
    own = c.post("/api/profiles", json={"name": "Sicherheit", "sources": ["sec", "alpha"]}).json()
    assert own["source_count"] == 2 and own["unread"] == 2
    assert _sources_of(c.get("/api/articles", params={"profile": own["id"]}).json()) == ["alpha", "sec"]
    assert c.post("/api/articles/read-all", json={"profile": world["id"]}).json()["marked"] == 2
    assert c.post("/api/articles/read-all", json={"profile": own["id"]}).json()["marked"] == 1


def test_in_all_false_source_in_search_and_country_filter(sec_client):
    c = sec_client
    assert _sources_of(c.get("/api/articles", params={"q": "Patchday"}).json()) == ["alpha"]
    assert _sources_of(c.get("/api/articles", params={"q": "Patchday", "source": "sec"}).json()) == ["sec"]
    # Länderfilter ohne Quelle wie ALLE; mit ausdrücklicher Quelle oder Profil-Liste sichtbar.
    assert _sources_of(c.get("/api/articles", params={"country": "DE"}).json()) == ["alpha"]
    assert _sources_of(c.get("/api/articles", params={"country": "DE", "source": "sec"}).json()) == ["sec"]
    own = c.post("/api/profiles", json={"name": "Sicherheit", "sources": ["sec"]}).json()
    assert _sources_of(c.get("/api/articles", params={"country": "DE", "profile": own["id"]}).json()) == ["sec"]


def test_in_all_false_source_stays_in_saved_lists(sec_client):
    c = sec_client
    sec_id = state.db.q("SELECT id FROM articles WHERE source_id='sec'")[0][0]
    state.db.set_flag(UID, sec_id, "starred", True)
    state.db.set_flag(UID, sec_id, "later", True)
    assert _ids(c.get("/api/articles", params={"starred": "true"}).json()) == [sec_id]
    assert _ids(c.get("/api/articles", params={"later": "true"}).json()) == [sec_id]
    counts = c.get("/api/counts").json()
    assert counts["starred"] == 1 and counts["later"] == 1


# ---- Geschichten: rechte Seite am Desktop (related, overview, frontpage) -------------------

STORY_SOURCES = (
    '  - {id: delta, name: Delta, short: "DEL", country: DE, lang: de, feed: "https://delta.example/feed", '
    'homepage: "https://delta.example"}\n'
    '  - {id: eps, name: Epsilon, country: GB, lang: en, feed: "https://eps.example/feed", '
    'homepage: "https://eps.example"}\n'
    '  - {id: zeta, name: Zeta, country: DE, lang: de, feed: "https://zeta.example/feed", '
    'homepage: "https://zeta.example", in_all: false}\n'
    '  - {id: thelocalde, name: The Local DE, country: DE, lang: en, feed: "https://tlde.example/feed", '
    'homepage: "https://tlde.example"}\n'
    '  - {id: thelocalat, name: The Local AT, country: AT, lang: en, feed: "https://tlat.example/feed", '
    'homepage: "https://tlat.example"}\n'
)


@pytest.fixture
def story_client(app_env):
    """Katalog alpha (de), beta (en), gamma (aus), delta (de), eps (en), zeta (de, in_all: false)."""
    from conftest import SOURCES_YAML
    from app import stories
    (app_env / "sources.yaml").write_text(SOURCES_YAML + STORY_SOURCES, encoding="utf-8")
    _STORIES.clear()
    stories._model = None               # kein Modell eines früheren Tests (fremde Artikel-IDs)
    with TestClient(app, base_url=ORIGIN) as c:
        assert c.post("/api/login", json={"username": "admin", "password": "geheim-und-lang"}).status_code == 204
        stories._model = None           # auch nicht das des Starts (leere Datenbank)
        yield c
    _STORIES.clear()
    stories._model = None


def _add(source: str, n: int, hours: float, *, story: str | None = None, image: bool = False,
         paywalled: bool = False, title: str | None = None, summary: str | None = None) -> int:
    """Legt einen Artikel an (story: Name der Geschichte, siehe _link_stories) und gibt die ID zurück."""
    state.db.upsert_articles([make_row(source, n, ago(hours=hours), title=title or f"{source} {n} {story or ''}",
                                       summary=summary or f"Anreißer {source} {n}",
                                       image_url="https://img.example/x.jpg" if image else None)])
    aid = state.db.one("SELECT id FROM articles WHERE guid=?", (f"{source}-{n}",))["id"]
    if paywalled:
        state.db.q("UPDATE articles SET paywalled=1 WHERE id=?", (aid,))
    if story:
        _STORIES.setdefault(story, []).append(aid)
    return aid


_STORIES: dict[str, list[int]] = {}


def _link_stories() -> dict[str, int]:
    """story_id wie der Lauf in app/stories.py: kleinste Artikel-ID der Geschichte."""
    out = {}
    for name, ids in _STORIES.items():
        out[name] = min(ids)
        state.db.set_story_ids([(min(ids), i) for i in ids])
    _STORIES.clear()
    return out


def test_related_same_story_order_scope_and_paywall(story_client):
    c = story_client
    me = _add("alpha", 1, 2, story="s")
    older_same = _add("alpha", 2, 5, story="s")
    en_new = _add("beta", 1, 1, story="s")
    _add("beta", 2, 4, story="s")                      # älter, gleiche Quelle: nur der neueste zählt
    de_other = _add("delta", 1, 6, story="s")
    _add("eps", 1, 1, story="s", paywalled=True)       # Paywall: ausgeblendet
    _add("gamma", 1, 1, story="s")                     # Quelle ausgeschaltet
    _add("zeta", 1, 1, story="s")                      # nicht in ALLE
    sid = _link_stories()["s"]
    body = c.get(f"/api/articles/{me}/related").json()
    assert set(body) == {"story_id", "sources_count", "same_story", "topic"}
    assert body["story_id"] == sid and body["sources_count"] == 3          # alpha, beta, delta
    # Nur andere Zeitungen (die eigene fehlt), deutschsprachige vor englischen, dann die neuesten; je
    # Zeitung einer.
    assert [it["id"] for it in body["same_story"]] == [de_other, en_new]
    assert older_same not in {it["id"] for it in body["topic"]}
    assert me not in {it["id"] for it in body["topic"]}
    # Gleiche Form wie die Liste
    listed = {it["id"]: it for it in c.get("/api/articles").json()["items"]}
    assert set(body["same_story"][0]) == set(listed[de_other])
    assert body["same_story"][0] == listed[de_other]


def test_related_without_story_and_unknown_article(story_client):
    c = story_client
    alone = _add("alpha", 1, 1)
    assert c.get(f"/api/articles/{alone}/related").json() == {
        "story_id": None, "sources_count": 1, "same_story": [], "topic": []}
    assert c.get("/api/articles/999999/related").status_code == 404
    assert c.get("/api/articles/0/related").status_code == 422


def test_related_counts_newspapers_and_hides_copies_of_the_open_article(story_client):
    """Eine wortgleiche Kopie des offenen Artikels erscheint nicht als „andere Zeitung“; die Zahl der
    Zeitungen zählt, was die Seite zeigt (die eigene und die gelisteten), damit Kopf und Liste passen."""
    c = story_client
    text = "Wortgleicher Agenturtext über das Ereignis, lang genug für eine Kopie."
    me = _add("alpha", 1, 2, story="s", title="Gleiche Meldung", summary=text)
    copy = _add("delta", 1, 1, story="s", title="Gleiche Meldung", summary=text)
    other = _add("beta", 1, 1, story="s", title="Other report", summary="Another text about it.")
    _link_stories()
    body = c.get(f"/api/articles/{me}/related").json()
    assert body["sources_count"] == 2
    assert [it["id"] for it in body["same_story"]] == [other] and copy not in {it["id"] for it in body["topic"]}


def test_related_counts_only_members_near_the_article(story_client):
    """Zur Geschichte zählen nur Artikel höchstens 36 h vom offenen Artikel entfernt (alte, übernommene
    Mitglieder einer weiterlaufenden Geschichte blähen die Zahl nicht auf)."""
    c = story_client
    me = _add("alpha", 1, 2, story="s")
    near = _add("beta", 1, 20, story="s")
    far = _add("delta", 1, 24 * 4, story="s")
    _link_stories()
    body = c.get(f"/api/articles/{me}/related").json()
    assert body["sources_count"] == 2 and [it["id"] for it in body["same_story"]] == [near]
    assert far not in {it["id"] for it in body["topic"]}


def test_frontpage_sections_cover_the_whole_day(story_client):
    """Die Rubriken wählen aus allen Artikeln der letzten 24 h (in SQL gruppiert), nicht nur aus den
    neuesten: eine Geschichte von vor 20 h mit zwei Zeitungen schlägt 320 neuere Einzelmeldungen."""
    c = story_client
    state.db.upsert_articles([make_row("alpha", 1000 + i, ago(minutes=5 + i)) for i in range(320)])
    story = [_add("alpha", 1, 20, story="old"), _add("delta", 1, 20.5, story="old")]
    for src in ("beta", "eps", "delta"):                      # größere Geschichte: der Aufmacher
        _add(src, 2, 3, story="lead")
    sid = _link_stories()["old"]
    de = next(s for s in c.get("/api/frontpage").json()["sections"] if s["name"] == "Deutschland")
    first = de["items"][0]
    assert first["sources_count"] == 2 and state.db.story_id_of(first["article"]["id"]) == sid
    assert first["article"]["id"] in story


def test_story_pages_degrade_instead_of_failing_on_timeouts(story_client, monkeypatch):
    from app.db import QueryTimeout
    c = story_client
    _add("alpha", 1, 2, story="s")
    _add("beta", 1, 1, story="s")
    _add("delta", 1, 1, story="s")
    _link_stories()

    def slow(*a, **kw):
        raise QueryTimeout("zu langsam")

    monkeypatch.setattr(state.db, "top_stories", slow)
    monkeypatch.setattr(state.db, "profile_story_groups", slow)
    ov = c.get("/api/overview")
    assert ov.status_code == 200 and ov.json()["top_stories"] == [] and ov.json()["counts"]["new_today"] == 3
    fp = c.get("/api/frontpage")
    assert fp.status_code == 200 and fp.json()["lead"] is None and fp.json()["sections"] == []


def test_story_pages_keep_what_fits_into_a_partly_used_budget(story_client, monkeypatch):
    """Frisst eine Abfrage das Zeitbudget der Seite fast auf, fehlt nur, was danach käme."""
    import time as time_mod

    from app import main
    c = story_client
    for src in ("alpha", "beta", "delta"):
        _add(src, 1, 1, story="s")
    _link_stories()
    monkeypatch.setattr(main, "STORY_PAGE_BUDGET", 0.3)
    real = state.db.published_since

    def slow(*a, **kw):
        out = real(*a, **kw)
        time_mod.sleep(0.35)
        return out

    monkeypatch.setattr(state.db, "published_since", slow)
    ov = c.get("/api/overview")
    assert ov.status_code == 200
    body = ov.json()
    assert body["counts"]["new_today"] == 3 and sum(body["hourly"]) == 3 and body["top_stories"] == []


def test_story_pages_survive_timeouts_of_counts_and_members(story_client, monkeypatch):
    from app.db import QueryTimeout
    c = story_client
    me = _add("alpha", 1, 2, story="s")
    _add("beta", 1, 1, story="s")
    _link_stories()

    def slow(*a, **kw):
        raise QueryTimeout("zu langsam")

    monkeypatch.setattr(state.db, "article_counts", slow)
    ov = c.get("/api/overview")
    assert ov.status_code == 200 and ov.json()["counts"]["unread_today"] is None
    monkeypatch.setattr(state.db, "story_articles", slow)
    monkeypatch.setattr(state.db, "story_member_ids", slow)
    rel = c.get(f"/api/articles/{me}/related")
    assert rel.status_code == 200 and rel.json()["same_story"] == [] and rel.json()["story_id"] is not None


def test_top_stories_count_newspapers_in_sql(story_client):
    """Ausgaben eines Verlags (The Local DE und AT) zählen schon in der Abfrage als eine Zeitung: 16
    neuere Geschichten aus Alpha und zwei The-Local-Ausgaben (zwei Zeitungen) verdrängen die ältere mit
    drei Zeitungen nicht aus der Vorauswahl."""
    c = story_client
    for k in range(16):
        for src in ("alpha", "thelocalde", "thelocalat"):
            _add(src, 100 + k, 0.5, story=f"tl{k}")
    for src in ("alpha", "beta", "delta"):
        _add(src, 1, 3, story="real")
    sids = _link_stories()
    top = c.get("/api/overview").json()["top_stories"]
    assert [s["story_id"] for s in top] == [sids["real"]] and top[0]["sources_count"] == 3


def test_representative_and_same_story_order_follow_centrality(story_client, monkeypatch):
    """Vertreter und Reihenfolge nach Mittigkeit (stories.centrality): Ein deutscher Artikel, der nur
    einen Nebenaspekt erzählt, verliert gegen einen mittigen englischen; unter den mittigen gewinnt der
    deutsche. Englische Artikel, die mit dem offenen nicht erkennbar dasselbe erzählen, fehlen."""
    from app import stories
    c = story_client
    side_de = _add("alpha", 1, 1, story="s", image=True)
    central_en = _add("beta", 1, 2, story="s", image=True)
    central_de = _add("delta", 1, 3, story="s")
    other_en = _add("eps", 1, 2, story="s")
    _link_stories()
    scores = {side_de: 0.2, central_en: 0.9, central_de: 0.8, other_en: 0.85}
    monkeypatch.setattr(stories, "central_scores", lambda ids: {i: scores[i] for i in ids if i in scores})
    top = c.get("/api/overview").json()["top_stories"]
    assert top[0]["article"]["id"] == central_de                  # mittig (>= 0.85 * 0.9) und deutsch
    monkeypatch.setattr(stories, "shown_with", lambda a, b: b != other_en)
    same = [it["id"] for it in c.get(f"/api/articles/{side_de}/related").json()["same_story"]]
    assert same == [central_de, central_en]


def test_related_without_story_shows_the_same_event_as_same_story(story_client, monkeypatch):
    """Ohne Geschichte: Artikel anderer Zeitungen, die dasselbe Ereignis erzählen (stories.same_event_ids),
    stehen unter „dieselbe Geschichte“ (gezählt wie gelistet), nicht unter den Themen."""
    from app import stories
    c = story_client
    me = _add("alpha", 1, 2)
    other = _add("beta", 1, 1)
    hidden = _add("zeta", 1, 1)                       # nicht in ALLE: fehlt auch hier
    monkeypatch.setattr(stories, "same_event_ids", lambda row, exclude: [other, hidden])
    monkeypatch.setattr(stories, "topic_ids", lambda db, row, exclude, **kw: [i for i in (other,) if i not in exclude])
    body = c.get(f"/api/articles/{me}/related").json()
    assert body["story_id"] is None and [it["id"] for it in body["same_story"]] == [other]
    assert body["sources_count"] == 2 and body["topic"] == []


def test_related_without_story_keeps_the_candidates_stories_out_of_topics(story_client, monkeypatch):
    """Gehört ein Artikel desselben Ereignisses zu einer Geschichte, ist keines ihrer Mitglieder ein Thema."""
    from app import stories
    c = story_client
    me = _add("alpha", 1, 2)
    other = _add("beta", 1, 1, story="s")
    member = _add("delta", 1, 1, story="s")
    unrelated = _add("eps", 1, 1)
    _link_stories()
    monkeypatch.setattr(stories, "same_event_ids", lambda row, exclude: [other])
    monkeypatch.setattr(stories, "topic_ids", lambda db, row, exclude, **kw: [i for i in (member, unrelated)
                                                                        if i not in exclude])
    body = c.get(f"/api/articles/{me}/related").json()
    assert [it["id"] for it in body["same_story"]] == [other]
    assert [it["id"] for it in body["topic"]] == [unrelated]


def test_related_passes_the_story_id_to_topics(story_client, monkeypatch):
    """Auch vor dem Fenster (außerhalb des Modells) kennt topic_ids die Geschichte des Artikels."""
    from app import stories
    c = story_client
    me = _add("alpha", 1, 24 * 5, story="old")
    _add("beta", 1, 24 * 5, story="old")
    sid = _link_stories()["old"]
    seen = {}

    def spy(db, row, exclude, **kw):
        seen["story_id"] = row.get("story_id")
        return []

    monkeypatch.setattr(stories, "topic_ids", spy)
    assert c.get(f"/api/articles/{me}/related").status_code == 200
    assert seen["story_id"] == sid


def test_overview_and_frontpage_just_after_midnight_use_the_last_24_hours(story_client, monkeypatch):
    """Um 00:05 KIOSK_TZ hat „heute“ noch keine Geschichte: Meistberichtet und Aufmacher kommen aus den
    letzten 24 Stunden (window „24h“), Zähler und Stunden bleiben bei heute."""
    c = story_client
    for src in ("alpha", "beta", "delta"):
        _add(src, 1, 2, story="night")
    sids = _link_stories()
    midnight = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(microsecond=0).isoformat()   # seit 00:05
    monkeypatch.setattr(state.db, "today_start", lambda now=None: midnight)
    ov = c.get("/api/overview").json()
    assert ov["window"] == "24h" and [s["story_id"] for s in ov["top_stories"]] == [sids["night"]]
    assert ov["counts"]["new_today"] == 0 and sum(ov["hourly"]) == 0
    fp = c.get("/api/frontpage").json()
    assert fp["window"] == "24h" and fp["lead"]["story_id"] == sids["night"]


def test_top_story_names_and_active_sources_count_newspapers(story_client):
    """The Local Deutschland und Österreich sind eine Zeitung: ein Kurzname, eine aktive Zeitung."""
    c = story_client
    for src in ("alpha", "thelocalde", "thelocalat", "beta"):
        _add(src, 1, 1, story="s")
    _link_stories()
    ov = c.get("/api/overview").json()
    top = ov["top_stories"][0]
    assert top["sources_count"] == 3 and len(top["sources"]) == 3
    assert ov["counts"]["active_sources"] == 3 and ov["counts"]["new_today"] == 4


def test_overview_day_query_timeout_gives_nulls(story_client, monkeypatch):
    from app.db import QueryTimeout
    c = story_client
    _add("alpha", 1, 1)

    def slow(*a, **kw):
        raise QueryTimeout("zu langsam")

    monkeypatch.setattr(state.db, "published_since", slow)
    body = c.get("/api/overview").json()
    assert body["counts"]["new_today"] is None and body["counts"]["active_sources"] is None
    assert body["hourly"] == []


def test_related_runs_under_one_budget(story_client, monkeypatch):
    """Alle Abfragen von /related teilen ein Zeitbudget; ist es verbraucht, bleibt die Seite leer statt 503."""
    import time as time_mod

    from app import main
    c = story_client
    me = _add("alpha", 1, 2, story="s")
    _add("beta", 1, 1, story="s")
    _link_stories()
    monkeypatch.setattr(main, "STORY_PAGE_BUDGET", 0.2)
    real = state.db.story_articles

    def slow(*a, **kw):
        out = real(*a, **kw)
        time_mod.sleep(0.25)
        return out

    monkeypatch.setattr(state.db, "story_articles", slow)
    rel = c.get(f"/api/articles/{me}/related")
    assert rel.status_code == 200 and rel.json()["same_story"] == [] and rel.json()["topic"] == []


def test_single_paper_story_counts_as_no_story(story_client):
    """Eine übrig gebliebene story_id, die nur noch eine Zeitung trägt, ist keine Geschichte."""
    c = story_client
    me = _add("alpha", 1, 2, story="s")
    _add("alpha", 2, 1, story="s")                 # dieselbe Zeitung
    _link_stories()
    body = c.get(f"/api/articles/{me}/related").json()
    assert body["story_id"] is None and body["same_story"] == [] and body["sources_count"] == 1


def test_related_old_article_is_404_for_others(story_client):
    """Wie GET /api/articles/{id}: hinter der Aufbewahrungsgrenze nur für den, der es gemerkt hat."""
    c = story_client
    old = _add("alpha", 1, 24 * 200)
    assert c.get(f"/api/articles/{old}/related").status_code == 404
    # Gemerkt, bevor die Aufbewahrungsfrist ablief (set_flag erlaubt das danach nicht mehr)
    state.db.q("INSERT INTO user_articles (user_id, article_id, starred, updated_at) VALUES (?, ?, 1, ?)",
               (UID, old, ago(days=150)))
    assert c.get(f"/api/articles/{old}/related").status_code == 200


def test_related_flags_do_not_leak_between_users(story_client):
    from app.auth import hash_password
    c = story_client
    me = _add("alpha", 1, 2, story="s")
    other = _add("beta", 1, 1, story="s")
    _link_stories()
    anna = state.db.create_user("anna", hash_password("anna-passwort-123"))
    state.db.set_flag(anna, other, "read", True)
    state.db.set_flag(anna, other, "starred", True)
    item = c.get(f"/api/articles/{me}/related").json()["same_story"][0]
    assert item["id"] == other and item["read"] == 0 and item["starred"] == 0


def test_related_topic_from_real_clustering(story_client):
    """Mit einem echten Lauf: dieselbe Geschichte über zwei Sprachen, verwandtes Thema daneben."""
    import asyncio

    from app import stories
    c = story_client
    quake = [
        ("alpha", "Erdbeben in der Türkei: Mindestens 40 Tote in Malatya",
         "Ein Erdbeben der Stärke 7,1 hat die Provinz Malatya erschüttert. Rettungskräfte suchen Verschüttete."),
        ("beta", "Earthquake in Turkey's Malatya kills at least 40",
         "A magnitude 7.1 earthquake struck Malatya province, rescuers search the rubble for survivors."),
        ("eps", "Malatya earthquake: death toll rises to 40 as rescuers search rubble",
         "Rescuers in Turkey's Malatya province search for survivors after the 7.1 magnitude quake."),
    ]
    ids = [_add(src, i, 3 - i, title=t, summary=s) for i, (src, t, s) in enumerate(quake)]
    later = _add("delta", 9, 1, title="Nach dem Erdbeben in Malatya: Türkei bittet um internationale Hilfe",
                 summary="Die Türkei bittet nach dem schweren Erdbeben in der Provinz Malatya um Zelte und Helfer "
                         "aus dem Ausland, die Regierung ruft den Notstand aus.")
    _add("beta", 8, 1, title="Apple unveils new iPhone with satellite messaging", summary="In Cupertino.")
    asyncio.run(stories.refresh(state.db, state.catalog))
    body = c.get(f"/api/articles/{ids[0]}/related").json()
    # Die deutsche Folgemeldung (Hilfsersuchen) darf zur Geschichte gehören oder ein verwandtes Thema sein.
    same = {it["id"] for it in body["same_story"]}
    assert body["story_id"] == ids[0] and same - {later} == set(ids[1:])
    assert body["sources_count"] == 3 + (later in same)
    topic = {it["id"] for it in body["topic"]}
    assert not topic & {*ids}
    assert all(it["source_id"] in ("alpha", "beta", "delta", "eps") for it in body["topic"])


def test_overview_counts_top_stories_and_hourly(story_client):
    c = story_client
    # Geschichte a: 4 Quellen, b: 3 Quellen, c: nur 2 (fehlt in der Übersicht)
    a_de = _add("alpha", 1, 2, story="a", image=True)
    _add("beta", 1, 1, story="a", image=True)
    _add("delta", 1, 3, story="a")
    _add("eps", 1, 1, story="a")
    _add("alpha", 2, 1, story="b")
    b_pick = _add("delta", 2, 2, story="b", image=True)
    _add("eps", 2, 3, story="b", image=True)
    _add("alpha", 3, 1, story="c")
    _add("beta", 3, 1, story="c")
    _add("beta", 4, 1, paywalled=True)            # zählt nirgends
    _add("zeta", 4, 1)                            # nicht in ALLE
    _add("alpha", 5, 24 * 3)                      # nicht von heute
    sids = _link_stories()
    state.db.set_flag(UID, a_de, "read", True)
    state.db.set_flag(UID, b_pick, "later", True)
    body = c.get("/api/overview").json()
    assert set(body) == {"date", "now_hour", "counts", "top_stories", "window", "hourly"}
    assert body["window"] == "today"
    assert len(body["date"]) == 10 and 0 <= body["now_hour"] <= 23
    assert body["counts"] == {"new_today": 9, "unread_today": c.get("/api/counts").json()["unread"],
                              "later": 1, "active_sources": 4}
    assert body["counts"]["unread_today"] == 8
    assert len(body["hourly"]) == 24 and sum(body["hourly"]) == 9
    top = body["top_stories"]
    assert [s["story_id"] for s in top] == [sids["a"], sids["b"]]
    assert [s["sources_count"] for s in top] == [4, 3]
    # Vertreter: deutschsprachig mit Bild; Kurznamen in der Reihenfolge der Auswahl
    assert top[0]["article"]["id"] == a_de and top[0]["sources"][0] == "ALPHA"
    assert top[1]["article"]["id"] == b_pick and top[1]["sources"][:1] == ["DEL"]
    assert len(top[0]["sources"]) == 4 and set(top[0]["sources"]) == {"ALPHA", "BETA", "DEL", "EPSILON"}
    assert top[0]["article"]["read"] == 1 and top[0]["article"]["summary"]


def test_frontpage_lead_and_profile_sections(story_client):
    c = story_client
    lead_img = _add("beta", 1, 2, story="big", image=True)
    _add("alpha", 1, 1, story="big")
    _add("delta", 1, 1, story="big")
    two = _add("alpha", 2, 3, story="two")
    _add("delta", 2, 2, story="two", image=True)
    solo_new = _add("delta", 3, 1)
    solo_old = _add("alpha", 4, 5)
    _add("alpha", 5, 30)                          # älter als 24 h
    sids = _link_stories()
    body = c.get("/api/frontpage").json()
    assert set(body) == {"date", "now_hour", "window", "lead", "sections"}
    assert body["window"] == "today"
    assert body["date"] == c.get("/api/overview").json()["date"] and 0 <= body["now_hour"] <= 23
    lead = body["lead"]
    assert lead["story_id"] == sids["big"] and lead["sources_count"] == 3
    assert lead["article"]["id"] == lead_img      # der Aufmacher bevorzugt ein Bild
    de = next(s for s in body["sections"] if s["name"] == "Deutschland")
    ids = [it["article"]["id"] for it in de["items"]]
    # ohne den Aufmacher; Geschichte "two" zuerst (2 Quellen, das Bild gewinnt), dann die neuesten
    assert len(ids) == 3 and state.db.story_id_of(ids[0]) == sids["two"] and ids[0] != two
    assert ids[1:] == [solo_new, solo_old]
    assert [it["sources_count"] for it in de["items"]] == [2, 1, 1]
    # Profile ohne Treffer (Wirtschaft, Militär: nur Begriffe) fallen weg; jede Geschichte nur einmal.
    names = [s["name"] for s in body["sections"]]
    assert "Wirtschaft" not in names and "Militär" not in names
    shown = [it["article"]["id"] for s in body["sections"] for it in s["items"]]
    assert len(shown) == len(set(shown)) and lead_img not in shown
    # Nur Profile, die oben als Filterchip stehen
    world = _profile(c, "world")
    c.patch(f"/api/profiles/{_profile(c, 'de')['id']}", json={"in_chips": False})
    assert [s["profile_id"] for s in c.get("/api/frontpage").json()["sections"]] == [world["id"]]


def test_frontpage_and_overview_when_empty(story_client):
    c = story_client
    fp = c.get("/api/frontpage").json()
    assert fp["lead"] is None and fp["sections"] == [] and len(fp["date"]) == 10
    body = c.get("/api/overview").json()
    assert body["top_stories"] == [] and body["hourly"] == [0] * 24
    assert body["counts"] == {"new_today": 0, "unread_today": 0, "later": 0, "active_sources": 0}


def test_story_routes_need_login(app_env):
    with TestClient(app, base_url=ORIGIN) as c:
        for path in ("/api/overview", "/api/frontpage", "/api/articles/1/related"):
            assert c.get(path).status_code == 401, path


def test_settings_desktop_empty_and_focus_mode(authed):
    assert authed.patch("/api/settings", json={"desktop_empty": "frontpage"}).json()["desktop_empty"] == "frontpage"
    assert authed.patch("/api/settings", json={"focus_mode": True}).json()["focus_mode"] is True
    for bad in ({"desktop_empty": "sidebar"}, {"desktop_empty": None}, {"desktop_empty": 1},
                {"focus_mode": "yes"}, {"focus_mode": 1}, {"focus_mode": None}):
        assert authed.patch("/api/settings", json=bad).status_code == 422, bad
    got = authed.get("/api/settings").json()
    assert got["desktop_empty"] == "frontpage" and got["focus_mode"] is True
    # Kaputter gespeicherter Wert: Standard gilt
    state.db.q("INSERT OR REPLACE INTO settings (user_id, key, value) VALUES (?, 'desktop_empty', '\"x\"')", (UID,))
    assert authed.get("/api/settings").json()["desktop_empty"] == "overview"
