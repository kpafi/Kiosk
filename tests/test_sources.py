"""sources.yaml: Typprüfung beim Laden."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.sources import Catalog

ROOT = Path(__file__).resolve().parent.parent


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
    assert len(shorts) == 95 and len(set(shorts)) == 95
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
    assert {s.id for s in catalog.all() if not s.in_all} == {"heisesec", "focus"}
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
    assert [s.id for s in catalog.all() if not s.in_all] == ["focus", "heisesec"]   # FOCUS: siehe unten
    sec = catalog.get("heisesec")
    assert sec.default_enabled is True and "Nicht in ALLE" in sec.note
