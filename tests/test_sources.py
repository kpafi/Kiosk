"""sources.yaml: Typprüfung beim Laden."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.sources import SHORT_MAX, Catalog, Source

ROOT = Path(__file__).resolve().parent.parent
# Nicht in ALLE, aber Lesestoff der KI-Ausgabe (in_all: false, in_edition: true)
WELTLAGE = {"unnews", "reliefweb", "thehindu", "africanews", "guardiandev", "carbonbrief", "dabanga"}


def _load(tmp_path, country_yaml: str) -> Catalog:
    path = tmp_path / "sources.yaml"
    path.write_text(
        "sources:\n  - {id: x, name: X, " + country_yaml + " feed: \"https://x.example/feed\"}\n",
        encoding="utf-8",
    )
    return Catalog.load(path)


@pytest.mark.parametrize("country_yaml", ["country: NO,", "country: ~,", "country: 49,", "country: '',", ""])
def test_country_must_be_text(tmp_path, country_yaml):
    with pytest.raises(ValueError, match="Quelle x: country muss ein Text sein"):
        _load(tmp_path, country_yaml)


def test_quoted_country_is_accepted(tmp_path):
    assert _load(tmp_path, 'country: "no",').get("x").country == "NO"


def test_shipped_catalog_loads():
    catalog = Catalog.load(ROOT / "sources.yaml")
    assert catalog.all() and all(isinstance(s.country, str) and s.country for s in catalog.all())
    assert catalog.get("thelocalno").country == "NO"


def test_short_defaults_to_upper_name_and_is_validated(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(
        "sources:\n"
        "  - {id: a, name: Sehr langer Zeitungsname, country: DE, feed: \"https://a.example/f\"}\n"
        "  - {id: b, name: B, short: bee, country: DE, feed: \"https://b.example/f\"}\n",
        encoding="utf-8",
    )
    catalog = Catalog.load(path)
    assert catalog.get("a").short == "SEHR LANGER"  # 12 Zeichen, Leerzeichen am Ende weg
    assert catalog.get("b").short == "BEE"
    path.write_text("sources:\n  - {id: c, name: C, short: VIEL-ZU-LANGER-NAME, country: DE, feed: \"x\"}\n",
                    encoding="utf-8")
    with pytest.raises(ValueError, match="Quelle c: short"):
        Catalog.load(path)


def test_shipped_catalog_has_distinct_short_names():
    catalog = Catalog.load(ROOT / "sources.yaml")
    shorts = [s.short for s in catalog.all()]
    # CNN und PBS am 28.09.2026 entfernt; dazu die KI-Ausgabe (kiosk) seit 03.10.2026 und die sieben
    # Weltlage-Quellen für die KI-Ausgabe
    assert len(shorts) == 101 and len(set(shorts)) == 101
    assert all(0 < len(x) <= 12 and x == x.upper() for x in shorts)
    assert catalog.get("spiegel").short == "SPIEGEL"


def test_include_paths_are_validated(tmp_path):
    path = tmp_path / "sources.yaml"
    for bad in ('"^/a"', "[]", "[1]", '[""]', '["(offen"]', "{a: b}", "[" + ", ".join(['"x"'] * 21) + "]"):
        path.write_text(f'sources:\n  - {{id: x, name: X, country: DE, feed: "https://x.example/f", include_paths: {bad}}}\n',
                        encoding="utf-8")
        with pytest.raises(ValueError, match="Quelle x: include_paths"):
            Catalog.load(path)
    path.write_text('sources:\n  - {id: x, name: X, country: DE, feed: "https://x.example/f", include_paths: ["^/a/", "b$"]}\n',
                    encoding="utf-8")
    src = Catalog.load(path).get("x")
    assert src.include_paths == ("^/a/", "b$")
    assert src.wants_url("https://x.example/a/1") and src.wants_url("https://x.example/c/b?q=1")
    assert not src.wants_url("https://x.example/c/a/1") and not src.wants_url("https://x.example/b/?q=b")
    assert not src.wants_url("http://[::1/a/") and not src.wants_url("")
    path.write_text('sources:\n  - {id: x, name: X, country: DE, feed: "https://x.example/f", include_paths: [";p=1$"]}\n',
                    encoding="utf-8")
    assert Catalog.load(path).get("x").wants_url("https://x.example/a/b;p=1?q=2#f")   # ;Parameter zählen mit
    assert _load(tmp_path, "country: DE,").get("x").wants_url("javascript:x")   # ohne Filter: alles


def test_new_sources_heise_security_and_gea():
    catalog = Catalog.load(ROOT / "sources.yaml")
    ids = [s.id for s in catalog.all()]
    assert ids[ids.index("heise") + 1] == "heisesec"
    sec = catalog.get("heisesec")
    assert (sec.name, sec.short, sec.country, sec.lang, sec.category, sec.default_enabled) == \
        ("heise Security", "HEISE SEC", "DE", "de", "tech", True)
    assert sec.feed == "https://www.heise.de/security/rss/news-atom.xml"
    assert sec.homepage == "https://www.heise.de/security/"
    gea = catalog.get("gea")
    assert (gea.name, gea.short, gea.country, gea.lang, gea.category, gea.default_enabled) == \
        ("Reutlinger General-Anzeiger", "GEA", "DE", "de", "general", True)
    assert gea.feed == "https://www.gea.de/sitemap-index/4-gea_de_-_news-sitemap.xml"
    assert gea.homepage == "https://www.gea.de" and "Paywall" in gea.note and "Sitemap" in gea.note
    # Im Abschnitt Deutschland: vor der ersten Quelle eines anderen Landes.
    first_foreign = next(i for i, s in enumerate(catalog.all()) if s.country != "DE")
    assert ids.index("gea") < first_foreign


@pytest.mark.parametrize("path,wanted", [
    ("/reutlingen_artikel,-knollen-aus-der-region-beim-reutlinger-kartoffelmarkt-_arid,7169640.html", True),
    ("/neckar-alb/ueber-die-alb_artikel,-x-_arid,1.html", True),
    ("/neckar-alb/kreis-tuebingen_artikel,-x-_arid,1.html", True),
    ("/neckar-alb/kultur-in-der-region_artikel,-x-_arid,1.html", True),
    ("/blaulicht_artikel,-mann-randaliert-in-metzingen-_arid,1.html", True),
    ("/sport/fussball-in-der-region_artikel,-x-_arid,1.html", True),
    ("/land_artikel,-vorfahrt-genommen-_arid,7169672.html", False),
    ("/welt_artikel,-x-_arid,1.html", False),
    ("/welt/weltspiegel_artikel,-x-_arid,1.html", False),
    ("/welt/wirtschaft_artikel,-x-_arid,1.html", False),
    ("/leser/leserbriefe_artikel,-x-_arid,1.html", False),
    ("/weitere-nachrichten_artikel,-x-_arid,1.html", False),
    ("/sport/ueberregionaler-sport_artikel,-x-_arid,1.html", False),
    ("/sport/vfl-pfullingen_artikel,-x-_arid,1.html", True),
    ("/sport/tigers-tuebingen_artikel,-x-_arid,1.html", True),
    ("/sport/tus-metzingen_artikel,-x-_arid,1.html", True),
    ("/land_artikel,-hilfe-in-der-region_artikel,-_arid,1.html", False),     # nur der Ressortteil zählt
    ("/welt_artikel,-reutlingen-und-blaulicht-_arid,1.html", False),
])
def test_gea_include_paths(path, wanted):
    gea = Catalog.load(ROOT / "sources.yaml").get("gea")
    assert gea.wants_url("https://www.gea.de" + path) is wanted


@pytest.mark.parametrize("path, wanted", [
    ("/politik/ausland/serbiens-praesident-tritt-zurueck_86d6ebbb.html", True),
    ("/panorama/welt/mann-tankt-mit-fremder-karte_1.html", True),
    ("/finanzen/finanzamt-krypto_1.html", True),
    ("/kultur/terminator-ikone-wird-70_1.html", True),          # Kultur ohne Promi-Ressort bleibt
    ("/kultur/buecher/neuer-roman_1.html", True),
    ("/kultur/stars/sylvie-meis_1.html", False),
    ("/kultur/kino-tv/fernsehgarten_1.html", False),
    ("/kultur/royals/prinz-william_1.html", False),
    ("/die-debatte/leser-diskutieren_1.html", False),
    ("/tagesthema/abends/19-uhr_1.html", False),
    ("/sport/fussball/die-debatte-um-klopp_1.html", True),      # nur das Ressort am Anfang zählt
    # Ratgeber und Lifestyle
    ("/gesundheit/some_1.html", False),
    ("/gesundheit/ratgeber/schlaf/tipps_1.html", False),
    ("/familie/schule/elternabend_1.html", False),
    ("/panorama/oktoberfest/wiesn-bilanz_1.html", False),
    ("/reisen/fernreisen/thailand_1.html", False),
    ("/finanzen/altersvorsorge/rente-mit-63_1.html", False),
    ("/finanzen/karriere/gehalt_1.html", False),
    ("/immobilien/kaufen/eigentumswohnung_1.html", False),
    ("/auto/fahrberichte/vw-id7_1.html", False),
    # Nachbarn davon bleiben
    ("/panorama/welt/unwetter_1.html", True),
    ("/finanzen/news/dax-schliesst_1.html", True),
    ("/finanzen/boerse/aktie_1.html", True),
    ("/reisen/flug/streik-am-flughafen_1.html", True),
    ("/immobilien/some-article_1.html", True),
    ("/immobilien/mieten/mietpreisbremse_1.html", True),
    ("/auto/news/rueckruf_1.html", True),
    ("/politik/deutschland/gesundheit-reform_1.html", True),  # Gesundheitspolitik im Ressort Politik
])
def test_focus_include_paths(path, wanted):
    focus = Catalog.load(ROOT / "sources.yaml").get("focus")
    assert focus.wants_url("https://www.focus.de" + path) is wanted


def test_focus_is_not_in_all_by_default():
    """FOCUS steht nur für Konten mit all_extra_sources in ALLE (wie heise Security gar nicht)."""
    catalog = Catalog.load(ROOT / "sources.yaml")
    assert {s.id for s in catalog.all() if not s.in_all} == {"heisesec", "focus"} | WELTLAGE
    assert catalog.get("focus").default_enabled is True and "Nicht in ALLE" in catalog.get("focus").note


def test_new_sources_are_enabled_in_an_existing_database(tmp_path):
    """Neue Quellen bekommen beim Start (ensure_sources) ihren default_enabled-Wert, bestehende
    Einstellungen bleiben, wie sie sind."""
    from app.db import Database
    catalog = Catalog.load(ROOT / "sources.yaml")
    old = [(s.id, s.default_enabled) for s in catalog.all() if s.id not in ("heisesec", "gea")]
    db = Database(tmp_path / "kiosk.db")
    try:
        db.ensure_sources(old)
        db.set_source_enabled("heise", False)
        db.set_source_enabled("dlf", True)
        before = {sid: r["enabled"] for sid, r in db.source_states().items()}
        db.ensure_sources([(s.id, s.default_enabled) for s in catalog.all()])   # wie app.main beim Start
        states = {sid: r["enabled"] for sid, r in db.source_states().items()}
        assert states["heisesec"] == 1 and states["gea"] == 1
        assert {sid: v for sid, v in states.items() if sid in before} == before
        assert states["heise"] == 0 and states["dlf"] == 1
        assert {"heisesec", "gea"} <= set(db.enabled_source_ids())
    finally:
        db.close()


def test_guid_pattern_is_validated(tmp_path):
    path = tmp_path / "sources.yaml"
    for bad in ('""', "[x]", "'(offen'", "'keine-gruppe'", "'(a)(b)'", "5"):
        path.write_text(f'sources:\n  - {{id: x, name: X, country: DE, feed: "https://x.example/f", guid_pattern: {bad}}}\n',
                        encoding="utf-8")
        with pytest.raises(ValueError, match="Quelle x: guid_pattern"):
            Catalog.load(path)
    path.write_text("sources:\n  - {id: x, name: X, country: DE, feed: \"https://x.example/f\", guid_pattern: 'id=(\\d+)'}\n",
                    encoding="utf-8")
    src = Catalog.load(path).get("x")
    assert src.guid_for("https://x.example/a?id=42", "d") == "42" and src.guid_for("https://x.example/a", "d") == "d"
    assert _load(tmp_path, "country: DE,").get("x").guid_for("https://x.example/a?id=42", "d") == "d"


def test_gea_guid_is_the_article_number():
    gea = Catalog.load(ROOT / "sources.yaml").get("gea")
    for slug in ("-alter-titel-", "-neuer-titel-"):
        assert gea.guid_for(f"https://www.gea.de/reutlingen_artikel,{slug}_arid,7169640.html", "x") == "7169640"


def test_in_all_is_validated_and_set_for_heise_security(tmp_path):
    path = tmp_path / "sources.yaml"
    for bad in ('"false"', "1", "0", "ja", "~x"):
        path.write_text(f'sources:\n  - {{id: x, name: X, country: DE, feed: "https://x.example/f", in_all: {bad}}}\n',
                        encoding="utf-8")
        with pytest.raises(ValueError, match="Quelle x: in_all"):
            Catalog.load(path)
    path.write_text('sources:\n  - {id: x, name: X, country: DE, feed: "https://x.example/f", in_all: false}\n',
                    encoding="utf-8")
    assert Catalog.load(path).get("x").in_all is False
    assert _load(tmp_path, "country: DE,").get("x").in_all is True
    catalog = Catalog.load(ROOT / "sources.yaml")
    assert [s.id for s in catalog.all() if not s.in_all and s.id not in WELTLAGE] == ["focus", "heisesec"]
    sec = catalog.get("heisesec")
    assert sec.default_enabled is True and "Nicht in ALLE" in sec.note


def test_catalog_changes_of_2026_09_28():
    catalog = Catalog.load(ROOT / "sources.yaml")
    assert "cnn" not in catalog and "pbs" not in catalog
    jt = catalog.get("japantimes")
    assert jt.default_enabled is False and jt.note == "Nur Überschrift und Anreißer, die Seite sperrt Server-Zugriffe"
    assert "als Paywall markiert" in catalog.get("scmp").note
    heise, bbc = catalog.get("heise"), catalog.get("bbc")
    assert not heise.wants_url("https://www.heise.de/bestenlisten/testsieger/top-10-der-beste-saugroboter/2dxtvlp")
    assert heise.wants_url("https://www.heise.de/news/Nvidia-stellt-Sicherheitsnetz-vor-11468689.html?wt_mc=rss")
    assert not bbc.wants_url("https://www.bbc.co.uk/sounds/play/live:bbc_radio_five_live?at_medium=RSS")
    assert bbc.wants_url("https://www.bbc.co.uk/news/articles/c85ydnwqpzyzo?at_medium=RSS&at_campaign=rss")
    hb = catalog.get("handelsblatt")
    assert not hb.wants_url("https://www.handelsblatt.com/epaper-pdf/pdf-handelsblatt-vom-29.-september-2026/100258003.html")
    assert hb.wants_url("https://www.handelsblatt.com/technik/ki/ki-mistral-steht-kurz-vor-dem-start/100257905.html")


@pytest.mark.parametrize("url, wanted", [
    # Videos (aus dem Feed vom 30.09.2026): nur eine kurze Beschreibung, „Nur Anfang verfügbar“
    ("https://www.welt.de/politik/deutschland/video6abcdab553ba19e8dfe57287/linnemanns-gesetzentwurf.html", False),
    ("https://www.welt.de/videos/video6abcc8c80f65d43326f6998c/moegliches-linksbuendnis.html", False),
    ("https://www.welt.de/vermischtes/video6abcbc3b7d42e93b4b11ecbc/nahost-vorfall-im-cockpit.html", False),
    ("https://www.welt.de/politik/ausland/video253112345/aeltere-video-id.html", False),
    ("https://www.welt.de/vermischtes/article6abcae7f0f65d43326f69752/block-prozess-wegen-entfuehrung.html", True),
    ("https://www.welt.de/wirtschaft/plus6abcc0e31181c77c293b7679/gesetzentwurf-exklusiv.html", True),
    ("https://www.welt.de/politik/article6abcc0bb1181c77c293b7675/videoueberwachung-am-bahnhof.html", True),
    ("https://www.welt.de/videos/", True),   # keine Video-ID
])
def test_welt_videos_are_excluded(url, wanted):
    assert Catalog.load(ROOT / "sources.yaml").get("welt").wants_url(url) is wanted


@pytest.mark.parametrize("path, wanted", [
    # aus dem Feed vom 30.09.2026: Videos, Sendungen und /multimedia/ ohne Artikeltext
    ("/video/video-1657984.html", False),
    ("/tagesthemen/video-1657796.html", False),
    ("/tagesschau_in_100_sekunden/video-1658006.html", False),
    ("/tagesthemen/tt-12898.html", False),
    ("/tagesschau_20_uhr/ts-81274.html", False),
    ("/tagesschau/ts-81232.html", False),
    ("/tagesschau_in_einfacher_sprache/tse-1452.html", False),
    ("/tagesschau_mit_gebaerdensprache/tsg-12280.html", False),
    ("/multimedia/livestreams/livestream-ts24-104.html", False),
    ("/multimedia/bilder/blickpunkte-10476.html", False),
    ("/multimedia/podcast/15-minuten/15minuten-handys-bau-flugzeuge-einkaufen-100.html", False),
    ("/eilmeldung/pflegereform-kabinett-100.html", True),
    ("/inland/reformen-bundesregierung-100.html", True),
    ("/wirtschaft/technologie/ki-ausbrueche-haftung-faq-100.html", True),
    ("/investigativ/br-recherche/oktoberfest-muenchen-teufelsrad-videos-100.html", True),
    ("/inland/tagesschau-video-archiv-100.html", True),
    ("/podcast-11km-wetterphaenomen-el-nino-100.html", False),   # leitet zur ARD Audiothek weiter
    ("/wetter/regenradar-deutschland", False),
    ("/wetter/deutschland/wettervorhersage-deutschland-100.html", False),
    ("/wissen/klima/podcast-folge-zum-wetter-100.html", True),   # nur Podcasts auf oberster Ebene
])
def test_tagesschau_videos_and_multimedia_are_excluded(path, wanted):
    ts = Catalog.load(ROOT / "sources.yaml").get("tagesschau")
    assert ts.wants_url("https://www.tagesschau.de" + path) is wanted
    assert ts.wants_url("https://www.sportschau.de/fussball/premier-league/city,statement-100.html")


@pytest.mark.parametrize("url, wanted", [
    ("https://www.bbc.co.uk/news/videos/c8n5d0e0n6wdo?at_medium=RSS&at_campaign=rss", False),
    ("https://www.bbc.co.uk/sounds/play/p0pd0z9v", False),
    ("https://www.bbc.co.uk/news/articles/cmlyewr0lnj3o?at_medium=RSS&at_campaign=rss", True),
    ("https://www.bbc.co.uk/sport/football/articles/c9lyk46gekkeo?at_medium=RSS&at_campaign=rss", True),
])
def test_bbc_videos_are_excluded(url, wanted):
    assert Catalog.load(ROOT / "sources.yaml").get("bbc").wants_url(url) is wanted


def test_source_removed_from_catalog_does_not_break_the_app(app_env):
    """Artikel, Einstellung, Favorit und Stern einer Quelle, die aus sources.yaml verschwunden ist (CNN, PBS),
    bleiben in der Datenbank; die App zeigt sie nicht mehr und stolpert nicht darüber."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.state import state
    from conftest import ago, make_row

    with TestClient(app, base_url="https://testserver") as c:
        assert c.post("/api/login", json={"username": "admin", "password": "geheim-und-lang"}).status_code == 204
        uid = state.db.q("SELECT id FROM users WHERE username = ?", ("admin",))[0]["id"]
        state.db.ensure_sources([("cnn", True)])
        state.db.upsert_articles([make_row("cnn", 1, ago(hours=1), body_len=900), make_row("alpha", 1, ago(hours=1))])
        cnn_id = state.db.q("SELECT id FROM articles WHERE source_id='cnn'")[0]["id"]
        state.db.put_favorite(uid, "cnn", True)
        state.db.set_flag(uid, cnn_id, "starred", True)
        for path in ("/api/sources", "/api/articles", "/api/articles?starred=true", "/api/articles?source=cnn",
                     "/api/overview", "/api/frontpage", "/api/status", "/api/favorites/sources", "/api/bar",
                     "/api/counts", "/api/profiles"):
            r = c.get(path)
            assert r.status_code == 200, (path, r.status_code, r.text[:200])
        # Ein gespeicherter Artikel der entfernten Quelle bleibt über seine ID lesbar (z. B. aus „Gemerkt“),
        # mit der Quellen-ID als Namen
        r = c.get(f"/api/articles/{cnn_id}")
        assert r.status_code == 200 and r.json()["source_name"] == "cnn", (r.status_code, r.text[:200])
        assert "cnn" not in {s["id"] for s in c.get("/api/sources").json()}
        assert all(a["source_id"] != "cnn" for a in c.get("/api/articles").json()["items"])


def test_generated_is_validated_and_allows_empty_feed(tmp_path):
    """generated: nur true/false; ohne generated ist feed Pflicht (nicht leer), mit generated darf er leer
    sein oder fehlen. generated_ids() nennt die generierten Quellen."""
    path = tmp_path / "sources.yaml"
    for bad in ('"ja"', "1", "[]"):
        path.write_text(f'sources:\n  - {{id: x, name: X, country: DE, feed: "", generated: {bad}}}\n',
                        encoding="utf-8")
        with pytest.raises(ValueError, match="Quelle x: generated muss true oder false sein"):
            Catalog.load(path)
    for feed in ('feed: "",', 'feed: "  ",', ""):
        path.write_text(f"sources:\n  - {{id: x, name: X, country: DE, {feed} homepage: \"\"}}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Quelle x: feed"):
            Catalog.load(path)
        path.write_text(f"sources:\n  - {{id: x, name: X, country: KI, {feed} generated: true}}\n", encoding="utf-8")
        src = Catalog.load(path).get("x")
        assert src.generated is True and src.feed == "" and src.homepage == ""
    path.write_text('sources:\n  - {id: a, name: A, country: DE, feed: "https://a.example/f", generated: false}\n'
                    '  - {id: b, name: B, country: KI, feed: "", generated: true}\n'
                    '  - {id: c, name: C, country: DE, feed: "https://c.example/f"}\n', encoding="utf-8")
    catalog = Catalog.load(path)
    assert catalog.generated_ids() == frozenset({"b"})
    assert not catalog.get("a").generated and not catalog.get("c").generated


def test_shipped_catalog_has_the_kiosk_edition_last():
    catalog = Catalog.load(ROOT / "sources.yaml")
    kiosk = catalog.get("kiosk")
    assert catalog.all()[-1] is kiosk and catalog.generated_ids() == frozenset({"kiosk"})
    assert (kiosk.name, kiosk.short, kiosk.country, kiosk.lang, kiosk.feed, kiosk.homepage) == \
        ("Kiosk", "KIOSK", "KI", "de", "", "")
    assert kiosk.in_all and kiosk.default_enabled and "OPENROUTER_API_KEY" in kiosk.note


def test_in_edition_defaults_to_in_all_and_is_validated(tmp_path):
    """in_edition: nur true/false; ohne Angabe wie in_all; generierte Quellen nie."""
    path = tmp_path / "sources.yaml"
    for bad in ('"true"', "1", "0", "ja", "[]"):
        path.write_text(f'sources:\n  - {{id: x, name: X, country: DE, feed: "https://x.example/f", in_edition: {bad}}}\n',
                        encoding="utf-8")
        with pytest.raises(ValueError, match="Quelle x: in_edition muss true oder false sein"):
            Catalog.load(path)
    rows = {
        "a": "",                                    # Standard: in_all true -> true
        "b": "in_all: false,",                      # Standard: wie in_all -> false
        "c": "in_all: false, in_edition: true,",
        "d": "in_edition: false,",
        "e": "in_all: true, in_edition: true,",
    }
    path.write_text("sources:\n" + "".join(
        f'  - {{id: {sid}, name: {sid.upper()}, country: DE, {extra} feed: "https://{sid}.example/f"}}\n'
        for sid, extra in rows.items()) + '  - {id: k, name: K, country: KI, feed: "", generated: true, in_edition: true}\n',
        encoding="utf-8")
    catalog = Catalog.load(path)
    assert {s.id: (s.in_all, s.in_edition) for s in catalog.all()} == {
        "a": (True, True), "b": (False, False), "c": (False, True), "d": (True, False), "e": (True, True),
        "k": (True, False)}
    # Direkt gebaute Quellen (Tests) folgen derselben Regel
    assert Source(id="z", name="Z", country="DE", lang="de", feed="f", homepage="", in_all=False).in_edition is False
    assert Source(id="z", name="Z", country="DE", lang="de", feed="f", homepage="").in_edition is True


def test_shipped_catalog_in_edition():
    """Bestand unverändert (FOCUS und heise Security bleiben aus der KI-Ausgabe, kiosk sowieso);
    die Weltlage-Quellen stehen nicht in ALLE, aber in der KI-Ausgabe."""
    catalog = Catalog.load(ROOT / "sources.yaml")
    assert {s.id for s in catalog.all() if not s.in_edition} == {"focus", "heisesec", "kiosk"}
    assert {s.id for s in catalog.all() if s.in_edition and not s.in_all} == WELTLAGE


@pytest.mark.parametrize("sid,short,country", [
    ("unnews", "UN NEWS", "UN"), ("reliefweb", "RELIEFWEB", "UN"), ("thehindu", "THE HINDU", "IN"),
    ("africanews", "AFRICANEWS", "AFR"), ("guardiandev", "GUARDIAN DEV", "GB"),
    ("carbonbrief", "CARBON BRIEF", "GB"), ("dabanga", "DABANGA", "SD"),
])
def test_weltlage_sources(sid, short, country):
    src = Catalog.load(ROOT / "sources.yaml").get(sid)
    assert (src.short, src.country, src.lang) == (short, country, "en")
    assert len(src.short) <= SHORT_MAX
    assert src.in_all is False and src.in_edition is True and src.default_enabled is True
    assert not src.generated and src.category == "general"
    assert src.feed.startswith("https://") and src.homepage.startswith("https://")
    assert "nicht in alle" in src.note.lower() and "KI-Ausgabe" in src.note


@pytest.mark.parametrize("url,wanted", [
    ("https://www.thehindu.com/news/international/iran-executes-man/article71536001.ece", True),
    ("https://www.thehindu.com/business/markets/x/article1.ece", True),
    ("https://www.thehindu.com/videos/trump-says-iran-link-possible/article71536084.ece", False),
    ("https://www.thehindu.com/podcast/us-china-detente-podcast/article71536787.ece", False),
])
def test_the_hindu_videos_and_podcasts_are_excluded(url, wanted):
    assert Catalog.load(ROOT / "sources.yaml").get("thehindu").wants_url(url) is wanted


@pytest.mark.parametrize("url,wanted", [
    ("https://www.theguardian.com/global-development/2026/oct/02/row-erupts-over-cairo-mural", True),
    ("https://www.theguardian.com/world/2026/oct/02/some-story", True),
    ("https://www.theguardian.com/world/video/2026/aug/23/ebola-day-100-explainer", False),
    # Newsletter- und Hinweisseiten, die der Feed seit Jahren mitführt
    ("https://www.theguardian.com/info/2017/jun/08/global-dispatch-sign-up-for-our-newsletter", False),
    ("https://www.theguardian.com/global/2022/sep/20/sign-up-for-the-her-stage-newsletter", False),
    ("https://www.theguardian.com/global-development/audio/2026/oct/01/some-podcast", False),
])
def test_guardian_development_videos_are_excluded(url, wanted):
    assert Catalog.load(ROOT / "sources.yaml").get("guardiandev").wants_url(url) is wanted


def test_guardian_development_is_the_guardian_in_stories():
    """Dieselbe Meldung kann in beiden Guardian-Feeds stehen; in Geschichten zählen sie als eine Zeitung."""
    from app.stories import publisher
    assert publisher("guardiandev") == publisher("guardian") == "guardian"
    assert publisher("unnews") == "unnews" and publisher("reliefweb") == "reliefweb"
