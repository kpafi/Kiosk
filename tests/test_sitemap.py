"""Google-News-Sitemaps als Quelle: Erkennung, sicheres Parsen, Auswahl im Index, Abruf und Filter."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import app.fetcher as fetcher_mod
import app.sitemap as sitemap_mod
from app.fetcher import Fetcher, parse_feed
from app.sitemap import SitemapError, parse_sitemap, pick_child, sitemap_kind, w3c_timestamp
from app.sources import Catalog, Source

PUBLIC = "http://93.184.216.34"
INDEX_URL = f"{PUBLIC}/sitemap-index/4-news-sitemap.xml"
CHILD_URL = f"{PUBLIC}/sitemap/4-2026-3-0001.xml"
SRC = Source(id="alpha", name="Alpha", country="DE", lang="de", feed=INDEX_URL, homepage=PUBLIC)
SM = "http://www.sitemaps.org/schemas/sitemap/0.9"
NEWS = "http://www.google.com/schemas/sitemap-news/0.9"
IMG = "http://www.google.com/schemas/sitemap-image/1.1"


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(fetcher_mod, "PER_DOMAIN_DELAY", 0)


def index(*children: tuple[str, str | None]) -> bytes:
    """Sitemap-Index wie bei GEA: <sitemap><loc/><lastmod/></sitemap> je Kind."""
    body = "".join(f"<sitemap><loc>{loc}</loc>" + (f"<lastmod>{mod}</lastmod>" if mod else "") + "</sitemap>"
                   for loc, mod in children)
    return f'<?xml version="1.0" encoding="utf-8" ?><sitemapindex xmlns="{SM}">{body}</sitemapindex>'.encode()


def url(loc: str, *, title: str | None = "Titel", published: str | None = "2026-09-26T19:05:00+02:00",
        lastmod: str | None = None, image: str | None = None, p: str = "n") -> str:
    """Ein <url>-Eintrag nach dem Muster der GEA-Sitemap (Präfix n: statt news:)."""
    news = (f"<{p}:news><{p}:publication><{p}:name>Reutlinger General-Anzeiger</{p}:name> "
            f"<{p}:language>de</{p}:language> </{p}:publication>"
            + (f"<{p}:publication_date>{published}</{p}:publication_date> " if published else "")
            + (f"<{p}:title>{title}</{p}:title>" if title is not None else "") + f"</{p}:news>")
    return (f"<url><loc>{loc}</loc> " + (f"<lastmod>{lastmod}</lastmod> " if lastmod else "") + news
            + (f"<image:image><image:loc>{image}</image:loc></image:image>" if image else "") + "</url>\n")


def urlset(*urls: str, p: str = "n") -> bytes:
    return (f'<?xml version="1.0" encoding="utf-8" ?><urlset xmlns="{SM}" xmlns:{p}="{NEWS}" '
            f'xmlns:image="{IMG}">' + "".join(urls) + "</urlset>").encode()


def art(n: int, section: str = "reutlingen") -> str:
    return f"https://www.gea.de/{section}_artikel,-meldung-{n}-_arid,{7169600 + n}.html"


# ---- Erkennung ------------------------------------------------------------------

def test_sitemap_kind():
    assert sitemap_kind(urlset(url(art(1)))) == "urlset"
    assert sitemap_kind(index((CHILD_URL, None))) == "sitemapindex"
    assert sitemap_kind(b"\xef\xbb\xbf" + urlset(url(art(1)))) == "urlset"                  # BOM
    assert sitemap_kind(f'<?xml version="1.0" encoding="utf-16"?><urlset xmlns="{SM}"/>'.encode("utf-16")) == "urlset"
    assert sitemap_kind(b"<?xml version='1.0'?><!-- Kommentar --><urlset xmlns='" + SM.encode() + b"'>") == "urlset"
    for other in (b"<?xml version='1.0'?><rss version='2.0'><channel/></rss>",
                  b"<feed xmlns='http://www.w3.org/2005/Atom'/>", b"<html><body>x</body></html>",
                  b"<urlset/>",                                                   # ohne Namensraum
                  b"<urlset xmlns='https://example.org/other'/>",
                  b'<!DOCTYPE urlset [<!ENTITY a "x">]><urlset xmlns="' + SM.encode() + b'"/>',
                  b"", b"kein xml", b"\x00\xff" * 100):
        assert sitemap_kind(other) is None, other[:60]


@pytest.mark.parametrize("encoding", ["foo", "x-mac-roman", "rot13", "base64", "utf-8-sig\x00"])
def test_sitemap_kind_never_raises_for_unknown_encodings(encoding):
    raw = f'<?xml version="1.0" encoding="{encoding}"?><rss version="2.0"><channel/></rss>'.encode()
    assert sitemap_kind(raw) is None
    raw = f'<?xml version="1.0" encoding="{encoding}"?><urlset xmlns="{SM}"/>'.encode()
    assert sitemap_kind(raw) is None
    with pytest.raises(SitemapError):
        parse_sitemap(SRC, raw, 300)


@pytest.mark.parametrize("encoding", ["foo", "x-mac-roman", "rot13"])
async def test_feed_with_unknown_encoding_still_reaches_feedparser(db, settings, encoding):
    """Das Erkennen der Sitemap darf einen Feed, den feedparser liest, nie scheitern lassen."""
    date = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    feed = (f'<?xml version="1.0" encoding="{encoding}"?><rss version="2.0"><channel><title>T</title>'
            f"<item><title>Eins</title><guid>g1</guid><link>{art(1)}</link><pubDate>{date}</pubDate></item>"
            "</channel></rss>").encode()
    src = Source(id="alpha", name="Alpha", country="DE", lang="de", feed=f"{PUBLIC}/feed", homepage=PUBLIC)
    f = make_fetcher(db, settings, lambda r: httpx.Response(200, content=feed), src)
    try:
        assert await f._fetch_feed(src) == 1
        assert db.source_states()["alpha"]["last_error"] is None
    finally:
        await f.close()


def test_deep_nesting_stays_linear():
    deep = 200_000
    raw = (f'<urlset xmlns="{SM}"><url><loc>{art(1)}</loc>' + "<a>" * deep + "</a>" * deep + "</url></urlset>").encode()
    t0 = time.monotonic()
    rows = parse_sitemap(SRC, raw, 300)
    assert time.monotonic() - t0 < 3
    assert [r["url"] for r in rows] == [art(1)]


def test_sitemap_kind_reads_only_the_start(monkeypatch):
    monkeypatch.setattr(sitemap_mod, "SNIFF_BYTES", 300)   # der Start-Tag von urlset() endet vorher
    assert sitemap_kind(b"<!--" + b"x" * 300 + b"-->" + urlset()) is None
    assert sitemap_kind(urlset(*(url(art(i)) for i in range(50)))) == "urlset"


# ---- URL-Liste ------------------------------------------------------------------

def test_parse_sitemap_gea_structure():
    raw = urlset(
        url(art(1), title="  Kartoffelmarkt\n in Reutlingen ", published="2026-09-01T19:05:00+02:00",
            lastmod="2026-09-01T19:33:09+02:00", image="https://www.gea.de/cms_media/bild.jpg"),
        url(art(2), title="Rathaussanierung &amp; Umzug", published="2026-09-01T20:28:00+02:00"),
    )
    before = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    rows = parse_sitemap(SRC, raw, 300)
    assert [r["url"] for r in rows] == [art(2), art(1)]              # neueste zuerst
    r2, r1 = rows
    assert r1 == {
        "source_id": "alpha", "guid": art(1), "url": art(1), "title": "Kartoffelmarkt in Reutlingen",
        "summary": "", "author": None, "image_url": "https://www.gea.de/cms_media/bild.jpg",
        "published_at": "2026-09-01T17:05:00+00:00", "fetched_at": r1["fetched_at"],
    }
    assert r1["fetched_at"] >= before
    assert r2["title"] == "Rathaussanierung & Umzug" and r2["image_url"] is None
    assert r2["published_at"] == "2026-09-01T18:28:00+00:00"


def test_prefix_does_not_matter():
    """Namensräume zählen, nicht Präfixe: n: (GEA) und news: (üblich) ergeben dasselbe."""
    items = [(art(1), "Eins", "2026-09-26T10:00:00Z"), (art(2), "Zwei", "2026-09-25T10:00:00+01:00")]
    rows_n = parse_sitemap(SRC, urlset(*(url(u, title=t, published=d) for u, t, d in items)), 300)
    rows_news = parse_sitemap(SRC, urlset(*(url(u, title=t, published=d, p="news") for u, t, d in items), p="news"), 300)
    strip = lambda rows: [{k: v for k, v in r.items() if k != "fetched_at"} for r in rows]  # noqa: E731
    assert strip(rows_n) == strip(rows_news) and len(rows_n) == 2
    # Falscher Namensraum hinter dem gewohnten Präfix: kein News-Element, also weder Titel noch Datum.
    wrong = urlset(url(art(3), title="X"), p="n").replace(NEWS.encode(), b"https://example.org/fake-news")
    assert parse_sitemap(SRC, wrong, 300)[0]["title"] == "(ohne Titel)"


def test_missing_title_and_dates():
    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    before = datetime.now(timezone.utc).replace(microsecond=0)
    rows = parse_sitemap(SRC, urlset(
        url(art(1), title=None),                                                         # ohne Titel
        url(art(2), title="   ", published=None, lastmod="2026-09-20T08:00:00+02:00"),  # Datum aus lastmod
        url(art(3), published=None),                                                     # ohne jedes Datum
        url(art(4), published=future),                                                   # Zukunft
        url(art(5), published="kein Datum", lastmod="2026-09-19"),                       # nur Tag
        url(art(6), published=None, lastmod="unlesbar"),
    ), 300)
    after = datetime.now(timezone.utc)
    by_url = {r["url"]: r for r in rows}
    assert by_url[art(1)]["title"] == "(ohne Titel)" and by_url[art(2)]["title"] == "(ohne Titel)"
    assert by_url[art(2)]["published_at"] == "2026-09-20T06:00:00+00:00"
    assert by_url[art(5)]["published_at"] == "2026-09-19T00:00:00+00:00"
    for n in (3, 4, 6):   # Zukunft auf jetzt begrenzt, fehlendes Datum = jetzt
        assert before <= datetime.fromisoformat(by_url[art(n)]["published_at"]) <= after
    # Ohne Datum ans Ende (in Reihenfolge der Sitemap), die Zukunft zählt vorn.
    assert [r["url"] for r in rows] == [art(4), art(1), art(2), art(5), art(3), art(6)]


@pytest.mark.parametrize("raw,expected", [
    ("2026-09-26T19:05:00+02:00", "2026-09-26T17:05:00+00:00"),
    ("2026-09-26T19:05+02:00", "2026-09-26T17:05:00+00:00"),
    ("2026-09-26T17:05:00.75Z", "2026-09-26T17:05:00+00:00"),
    ("2026-09-26T17:05:00z", "2026-09-26T17:05:00+00:00"),
    ("2026-09-26T17:05:00", "2026-09-26T17:05:00+00:00"),       # ohne Zeitzone: UTC
    ("2026-09-26T01:00:00-05:00", "2026-09-26T06:00:00+00:00"),
    ("2026-09-26", "2026-09-26T00:00:00+00:00"),
    ("2026-09", "2026-09-01T00:00:00+00:00"),
    ("2026", "2026-01-01T00:00:00+00:00"),
    (" 2026-09-26T19:05:00+02:00 ", "2026-09-26T17:05:00+00:00"),
    ("", None), (None, None), ("gestern", None), ("2026-13-01", None), ("9999-12-31T23:59:59-14:00", None),
    ("2026-09-26T19:05:00+02:00" + " " * 60, "2026-09-26T17:05:00+00:00"),
    ("1" * 100, None),
])
def test_w3c_timestamp(raw, expected):
    ts = w3c_timestamp(raw)
    assert (None if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()) == expected


def test_unsafe_locs_and_images_are_skipped():
    rows = parse_sitemap(SRC, urlset(
        url("javascript:alert(1)"), url("ftp://www.gea.de/a"), url("/relativ_artikel,-x-_arid,1.html"),
        url("  "), url("file:///etc/passwd"),
        url(art(1), image="javascript:alert(1)"), url(art(2), image="//cdn.example/bild.jpg"),
        "<url><n:news><n:title>ohne loc</n:title></n:news></url>",
    ), 300)
    assert [(r["url"], r["image_url"]) for r in rows] == [(art(1), None), (art(2), None)]


def test_only_direct_children_count():
    """<loc> zählt nur direkt unter <url>, Titel und Datum nur in n:news; Verschachteltes wird überlesen."""
    raw = (f'<urlset xmlns="{SM}" xmlns:n="{NEWS}"><url><x><loc>{art(9)}</loc></x><loc>{art(1)}</loc>'
           f"<loc>{art(2)}</loc><n:title>kein news</n:title></url>"
           f"<other><url><loc>{art(3)}</loc></url></other></urlset>").encode()
    rows = parse_sitemap(SRC, raw, 300)
    assert [(r["url"], r["title"]) for r in rows] == [(art(1), "(ohne Titel)")]


@pytest.mark.parametrize("raw", [
    # Entity-Expansion ("billion laughs") und externe Entities: schon das DOCTYPE wird abgelehnt.
    ('<?xml version="1.0"?><!DOCTYPE urlset [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;">]>'
     f'<urlset xmlns="{SM}"><url><loc>https://x.example/&b;</loc></url></urlset>').encode(),
    ('<?xml version="1.0"?><!DOCTYPE urlset [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
     f'<urlset xmlns="{SM}"><url><loc>https://x.example/&xxe;</loc></url></urlset>').encode(),
    ('<?xml version="1.0"?><!DOCTYPE urlset SYSTEM "http://evil.example/x.dtd">'
     f'<urlset xmlns="{SM}"><url><loc>https://x.example/a</loc></url></urlset>').encode(),
    (f'<urlset xmlns="{SM}"><url><loc>https://x.example/&undefiniert;</loc></url></urlset>').encode(),
    (f'<urlset xmlns="{SM}"><url><loc>https://x.example/a</loc></url>').encode(),        # abgeschnitten
    b"<rss version='2.0'><channel/></rss>",                                                 # keine URL-Liste
    index((CHILD_URL, None)),
])
def test_hostile_or_broken_sitemaps_are_rejected(raw):
    with pytest.raises(SitemapError):
        parse_sitemap(SRC, raw, 300)


def test_doctype_in_utf16_is_rejected_too():
    raw = (f'<?xml version="1.0" encoding="utf-16"?><!DOCTYPE urlset [<!ENTITY a "x">]>'
           f'<urlset xmlns="{SM}"><url><loc>https://x.example/&a;</loc></url></urlset>').encode("utf-16")
    with pytest.raises(SitemapError, match="DOCTYPE"):
        parse_sitemap(SRC, raw, 300)


def test_entries_and_values_are_capped(monkeypatch):
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    urls = [url(art(i), published=(base + timedelta(hours=i)).isoformat()) for i in range(10)]
    rows = parse_sitemap(SRC, urlset(*urls), 3)
    assert [r["url"] for r in rows] == [art(9), art(8), art(7)]       # die neuesten, nicht die ersten
    monkeypatch.setattr(sitemap_mod, "MAX_ITEMS", 4)
    assert [r["url"] for r in parse_sitemap(SRC, urlset(*urls), 300)] == [art(3), art(2), art(1), art(0)]
    long_title = parse_sitemap(SRC, urlset(url(art(1), title="Wort " * 5000)), 300)[0]["title"]
    assert len(long_title) <= 300
    assert parse_sitemap(SRC, urlset(url("https://x.example/" + "a" * 5000)), 300)[0]["url"] == \
        ("https://x.example/" + "a" * 5000)[:1000]


# ---- Sitemap-Index ----------------------------------------------------------------

HOSTS = frozenset({"93.184.216.34"})


def test_pick_child_takes_newest_lastmod():
    raw = index((f"{PUBLIC}/sitemap/4-2026-2-0001.xml", "2026-06-30T23:59:00+02:00"),
                (CHILD_URL, "2026-09-26T19:03:41+02:00"),
                (f"{PUBLIC}/sitemap/ohne-datum.xml", None))
    assert pick_child(raw, HOSTS) == CHILD_URL
    # Zeitzonen zählen: 01:00+02:00 ist früher als 00:30+00:00.
    raw = index((f"{PUBLIC}/a.xml", "2026-09-26T01:00:00+02:00"), (f"{PUBLIC}/b.xml", "2026-09-26T00:30:00Z"))
    assert pick_child(raw, HOSTS) == f"{PUBLIC}/b.xml"


def test_pick_child_tie_break_is_highest_url():
    """Ohne oder mit gleichem lastmod gewinnt die lexikographisch größte URL (neuestes Quartal)."""
    q3, q4 = f"{PUBLIC}/sitemap/4-2026-3-0001.xml", f"{PUBLIC}/sitemap/4-2026-4-0001.xml"
    assert pick_child(index((q4, None), (q3, "kaputt")), HOSTS) == q4
    assert pick_child(index((q3, None), (q4, None)), HOSTS) == q4
    same = "2026-10-01T00:00:10+02:00"
    assert pick_child(index((q4, same), (q3, same)), HOSTS) == q4
    assert pick_child(index((q3, same), (q4, same)), HOSTS) == q4
    # Ein lesbares Datum schlägt eine größere URL ohne Datum; sonst zählt das jüngere Datum.
    assert pick_child(index((q4, None), (q3, same)), HOSTS) == q3
    assert pick_child(index((q4, "2026-09-30T23:00:00+02:00"), (q3, "2026-10-01T00:00:00+02:00")), HOSTS) == q3
    assert pick_child(index(), HOSTS) is None


def test_pick_child_only_same_host_and_default_ports():
    raw = index(("https://evil.example/sitemap.xml", "2026-09-27T00:00:00Z"),              # fremder Host
                (f"{PUBLIC}:8080/sitemap.xml", "2026-09-27T00:00:00Z"),                    # anderer Port
                ("http://[::1/sitemap.xml", "2026-09-27T00:00:00Z"),                       # kaputte URL
                ("ftp://93.184.216.34/sitemap.xml", "2026-09-27T00:00:00Z"),
                ("/sitemap/relativ.xml", "2026-09-27T00:00:00Z"),
                (f"{PUBLIC}:80/alt.xml", "2026-01-01T00:00:00Z"))
    assert pick_child(raw, HOSTS) == f"{PUBLIC}:80/alt.xml"
    assert pick_child(index(("https://evil.example/s.xml", None)), HOSTS) is None
    # Groß-/Kleinschreibung und abschließender Punkt am Host spielen keine Rolle.
    assert pick_child(index(("https://WWW.GEA.DE./s.xml", None)), frozenset({"www.gea.de"})) == "https://WWW.GEA.DE./s.xml"


def test_pick_child_rejects_doctype():
    raw = b'<!DOCTYPE x [<!ENTITY a "b">]>' + index((CHILD_URL, None))
    with pytest.raises(SitemapError):
        pick_child(raw, HOSTS)


# ---- Abruf über den Fetcher --------------------------------------------------------------

def make_fetcher(db, settings, handler, src: Source = SRC) -> Fetcher:
    f = Fetcher(db, Catalog([src]), settings)
    f._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    return f


def recent(hours: float = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()


class Server:
    """Index und Kind-Sitemap unter PUBLIC; zeichnet alle Anfragen auf."""

    def __init__(self, index_body: bytes | None = None, child_body: bytes | None = None, child_status: int = 200):
        self.index_body = index_body if index_body is not None else index((CHILD_URL, recent(0)))
        self.child_body = child_body if child_body is not None else urlset(
            url(art(1), published=recent(1)), url(art(2, "land"), published=recent(2)),
            url(art(3, "neckar-alb/neckar-erms"), published=recent(3)))
        self.child_status = child_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        headers = {"etag": '"v1"', "last-modified": "Sat, 26 Sep 2026 17:00:00 GMT", "content-type": "text/xml"}
        if str(request.url) == INDEX_URL:
            return httpx.Response(200, headers=headers, content=self.index_body)
        if str(request.url) == CHILD_URL:
            return httpx.Response(self.child_status, headers=headers, content=self.child_body)
        return httpx.Response(404)

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


async def test_fetch_sitemap_index_follows_newest_child(db, settings):
    server = Server(index((f"{PUBLIC}/sitemap/alt.xml", recent(24 * 90)), (CHILD_URL, recent(0))))
    f = make_fetcher(db, settings, server)
    try:
        assert await f._fetch_feed(SRC) == 3
        assert server.urls() == [INDEX_URL, CHILD_URL]                     # genau ein Kind je Lauf
        rows = {r["url"]: r for r in db.q("SELECT url, title, summary FROM articles")}
        assert set(rows) == {art(1), art(2, "land"), art(3, "neckar-alb/neckar-erms")}
        assert rows[art(1)]["title"] == "Titel" and rows[art(1)]["summary"] == ""
        st = db.source_states()["alpha"]
        # Keine Validatoren für Sitemaps: Ein 304 auf den Index darf keine geänderte URL-Liste verdecken.
        assert st["etag"] is None and st["last_modified"] is None and st["last_error"] is None
        assert st["last_new"] == 3

        assert await f._fetch_feed(SRC) == 0
        assert server.urls()[2:] == [INDEX_URL, CHILD_URL]
        for r in server.requests:
            assert "if-none-match" not in r.headers and "if-modified-since" not in r.headers
    finally:
        await f.close()


async def test_old_validators_do_not_survive_a_sitemap_fetch(db, settings):
    """War die Quelle vorher ein Feed mit ETag, löscht der erste Sitemap-Abruf die Validatoren."""
    db.record_fetch("alpha", ok=True, validators=('"alt"', "Mon, 01 Sep 2026 10:00:00 GMT"))
    server = Server()
    f = make_fetcher(db, settings, server)
    try:
        await f._fetch_feed(SRC)
        st = db.source_states()["alpha"]
        assert st["etag"] is None and st["last_modified"] is None
        await f._fetch_feed(SRC)
        assert "if-none-match" not in server.requests[-2].headers
    finally:
        await f.close()


async def test_direct_urlset_as_feed(db, settings):
    src = Source(id="alpha", name="Alpha", country="DE", lang="de", feed=CHILD_URL, homepage=PUBLIC)
    server = Server()
    f = make_fetcher(db, settings, server, src)
    try:
        assert await f._fetch_feed(src) == 3
        assert server.urls() == [CHILD_URL]
        assert db.source_states()["alpha"]["etag"] is None
    finally:
        await f.close()


async def test_include_paths_filter_sitemap_entries(db, settings):
    src = Source(id="alpha", name="Alpha", country="DE", lang="de", feed=INDEX_URL, homepage=PUBLIC,
                 include_paths=("^/reutlingen[^,]*_artikel,", "^/neckar-alb/[^,]*_artikel,"))
    f = make_fetcher(db, settings, Server(), src)
    try:
        assert await f._fetch_feed(src) == 2
        assert {r["url"] for r in db.q("SELECT url FROM articles")} == {art(1), art(3, "neckar-alb/neckar-erms")}
    finally:
        await f.close()


@pytest.mark.parametrize("index_body,error", [
    (index(("https://evil.example/sitemap.xml", None)), "Sitemap-Index nennt keine zulässige Sitemap"),
    (index((f"{PUBLIC}:8443/sitemap.xml", None)), "Sitemap-Index nennt keine zulässige Sitemap"),
    (index(), "Sitemap-Index nennt keine zulässige Sitemap"),
    (b'<!DOCTYPE s [<!ENTITY a "b">]>' + index((CHILD_URL, None)), "Feed enthält keine Einträge"),  # an feedparser
])
async def test_index_without_usable_child(db, settings, index_body, error):
    server = Server(index_body)
    f = make_fetcher(db, settings, server)
    try:
        await f._fetch_all_feeds()
        assert db.source_states()["alpha"]["last_error"] == error
        assert server.urls() == [INDEX_URL]                                  # kein weiterer Abruf
        assert db.q("SELECT COUNT(*) AS n FROM articles")[0]["n"] == 0
    finally:
        await f.close()


@pytest.mark.parametrize("child_body,child_status,error", [
    (index((CHILD_URL, None)), 200, "Sitemap-Index verweist nicht auf eine URL-Liste"),   # Index im Index
    (b"<rss version='2.0'><channel/></rss>", 200, "Sitemap-Index verweist nicht auf eine URL-Liste"),
    (b"", 404, "Sitemap: HTTP 404"),
    (b'<?xml version="1.0"?><!DOCTYPE urlset [<!ENTITY a "b">]><urlset xmlns="' + SM.encode() + b'"/>', 200,
     "Sitemap-Index verweist nicht auf eine URL-Liste"),
    (urlset(url(art(1)))[:-5], 200, "SitemapError: Sitemap ungültig: "),                      # abgeschnitten
])
async def test_bad_child(db, settings, child_body, child_status, error):
    server = Server(child_body=child_body, child_status=child_status)
    f = make_fetcher(db, settings, server)
    try:
        await f._fetch_all_feeds()
        assert db.source_states()["alpha"]["last_error"].startswith(error)
        assert server.urls() == [INDEX_URL, CHILD_URL]                        # höchstens ein Kind
    finally:
        await f.close()


async def test_child_goes_through_the_guarded_fetch(db, settings, monkeypatch):
    """Das Kind läuft über _get: Größenlimit und netguard (auch nach einer Umleitung) gelten."""
    monkeypatch.setattr(fetcher_mod, "FEED_MAX_BYTES", 5000)
    server = Server(child_body=urlset(*(url(art(i), published=recent(1)) for i in range(100))))
    f = make_fetcher(db, settings, server)
    try:
        await f._fetch_all_feeds()
        assert db.source_states()["alpha"]["last_error"].startswith("Antwort zu groß")
    finally:
        await f.close()

    def redirecting(request):
        if str(request.url) == CHILD_URL:
            return httpx.Response(302, headers={"location": "http://127.0.0.1/sitemap.xml"})
        return server(request)

    f = make_fetcher(db, settings, redirecting)
    try:
        await f._fetch_all_feeds()
        assert db.source_states()["alpha"]["last_error"].startswith("Adresse nicht erlaubt: 127.0.0.1")
    finally:
        await f.close()


def _hanging_sitemap(source, raw, max_entries):
    """Läuft im Worker-Prozess: Sitemap-Parsing, das nicht fertig wird."""
    time.sleep(60)


async def test_sitemap_parsing_runs_in_worker_with_timeout(db, settings, monkeypatch):
    monkeypatch.setattr(fetcher_mod, "parse_sitemap", _hanging_sitemap)
    monkeypatch.setattr(fetcher_mod, "FEED_PARSE_TIMEOUT", 1)
    f = make_fetcher(db, settings, Server())
    try:
        assert await asyncio.wait_for(f._fetch_all_feeds(), timeout=20) == 0
        assert db.source_states()["alpha"]["last_error"] == "Sitemap-Parsing: Zeitüberschreitung"
        assert not f._pids   # hängender Worker-Prozess beendet
    finally:
        await f.close()


async def test_sitemap_new_articles_are_capped_per_run(db, settings, monkeypatch):
    monkeypatch.setattr(fetcher_mod, "FEED_MAX_NEW", 3)
    server = Server(child_body=urlset(*(url(art(i), published=recent(i + 1)) for i in range(5))))
    f = make_fetcher(db, settings, server)
    try:
        assert await f._fetch_feed(SRC) == 3
        assert await f._fetch_feed(SRC) == 2
    finally:
        await f.close()


async def test_empty_or_fully_filtered_sitemap_is_not_an_error(db, settings):
    """Neue, noch leere Quartals-Sitemap oder nur ungewollte Ressorts: Lauf ohne Fehler, 0 neue."""
    src = Source(id="alpha", name="Alpha", country="DE", lang="de", feed=INDEX_URL, homepage=PUBLIC,
                 include_paths=("^/reutlingen",))
    for body in (urlset(), urlset(url(art(1, "welt"), published=recent(1)))):
        f = make_fetcher(db, settings, Server(child_body=body), src)
        try:
            assert await f._fetch_feed(src) == 0
            st = db.source_states()["alpha"]
            assert st["last_error"] is None and st["last_ok_at"] is not None and st["last_new"] == 0
        finally:
            await f.close()


async def test_sitemap_without_usable_entries_is_an_error(db, settings):
    """Einträge da, aber keiner brauchbar (nicht wegen include_paths): Fehler wie beim Feed."""
    body = urlset(url("javascript:alert(1)"), url("/relativ_artikel,-x-_arid,1.html"))
    f = make_fetcher(db, settings, Server(child_body=body))
    try:
        assert await f._fetch_feed(SRC) == 0
        assert db.source_states()["alpha"]["last_error"] == "Feed enthält keine Einträge"
    finally:
        await f.close()


def test_parsed_rows_count_entries_and_filtered():
    import pickle
    src = Source(id="alpha", name="Alpha", country="DE", lang="de", feed=INDEX_URL, homepage=PUBLIC,
                 include_paths=("^/reutlingen",))
    rows = pickle.loads(pickle.dumps(parse_sitemap(src, urlset(url(art(1)), url(art(2, "welt")), url("ftp://x/y")), 300)))
    assert len(rows) == 1 and rows.total == 3 and rows.filtered_out == 1
    empty = parse_sitemap(src, urlset(), 300)
    assert empty == [] and empty.total == 0 and empty.filtered_out == 0


async def test_child_change_is_logged(db, settings, caplog):
    import logging
    server = Server()
    f = make_fetcher(db, settings, server)
    try:
        with caplog.at_level(logging.INFO, logger="kiosk.fetcher"):
            await f._fetch_feed(SRC)
            await f._fetch_feed(SRC)
            q4 = f"{PUBLIC}/sitemap/4-2026-4-0001.xml"
            server.index_body = index((CHILD_URL, recent(1)), (q4, recent(0)))
            await f._fetch_feed(SRC)
        logged = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Sitemap alpha")]
        assert logged == [f"Sitemap alpha: {CHILD_URL}", f"Sitemap alpha: {q4}"]
    finally:
        await f.close()


async def test_guid_pattern_prevents_duplicates_after_slug_change(db, settings):
    src = Source(id="alpha", name="Alpha", country="DE", lang="de", feed=INDEX_URL, homepage=PUBLIC,
                 guid_pattern=r"_arid,(\d+)")
    old = "https://www.gea.de/reutlingen_artikel,-alter-titel-_arid,7169640.html"
    new = "https://www.gea.de/reutlingen_artikel,-neuer-titel-_arid,7169640.html"
    server = Server(child_body=urlset(url(old, published=recent(1))))
    f = make_fetcher(db, settings, server, src)
    try:
        assert await f._fetch_feed(src) == 1
        server.child_body = urlset(url(new, title="Neuer Titel", published=recent(1)),
                                   url("https://www.gea.de/reutlingen_artikel,-ohne-nummer.html", published=recent(2)))
        assert await f._fetch_feed(src) == 1                          # nur der Eintrag ohne Nummer ist neu
        rows = {r["guid"]: r["url"] for r in db.q("SELECT guid, url FROM articles")}
        assert rows == {"7169640": old,
                        "https://www.gea.de/reutlingen_artikel,-ohne-nummer.html":
                            "https://www.gea.de/reutlingen_artikel,-ohne-nummer.html"}
    finally:
        await f.close()


def test_guid_pattern_in_feeds():
    src = Source(id="alpha", name="Alpha", country="DE", lang="de", feed=INDEX_URL, homepage=PUBLIC,
                 guid_pattern=r"_arid,(\d+)")
    rows = parse_feed(src, _rss([art(1), "https://www.gea.de/ohne-nummer.html"]))
    assert sorted(r["guid"] for r in rows) == ["7169601", "g1"]         # sonst die GUID des Feeds


# ---- include_paths im Feed ------------------------------------------------------------------

def _rss(links: list[str]) -> bytes:
    items = "".join(f"<item><title>{i}</title><guid>g{i}</guid><link>{link}</link>"
                    f"<pubDate>Mon, 01 Sep 2026 {20 - i:02d}:00:00 GMT</pubDate></item>"   # neueste vorn
                    for i, link in enumerate(links))
    return f"<?xml version='1.0'?><rss version='2.0'><channel><title>T</title>{items}</channel></rss>".encode()


def test_include_paths_apply_to_feeds_before_the_cap(monkeypatch):
    monkeypatch.setattr(fetcher_mod, "FEED_MAX_ENTRIES", 2)
    src = Source(id="alpha", name="Alpha", country="DE", lang="de", feed=INDEX_URL, homepage=PUBLIC,
                 include_paths=("^/reutlingen",))
    # Die ungewollten Einträge sind neuer und stehen vorn; ohne Filter vor der Grenze bliebe nichts.
    links = [art(i, "welt") for i in range(5, 10)] + [art(1), art(2), art(3)] + ["", "javascript:x"]
    rows = parse_feed(src, _rss(links))
    assert [r["url"] for r in rows] == [art(1), art(2)]
    assert len(parse_feed(SRC, _rss(links))) == 2                                      # ohne Filter: alle zählen


async def test_fully_filtered_feed_is_not_an_error(db, settings):
    src = Source(id="alpha", name="Alpha", country="DE", lang="de", feed=f"{PUBLIC}/feed", homepage=PUBLIC,
                 include_paths=("^/reutlingen",))
    f = make_fetcher(db, settings, lambda r: httpx.Response(200, content=_rss([art(1, "welt")])), src)
    try:
        assert await f._fetch_feed(src) == 0
        assert db.source_states()["alpha"]["last_error"] is None
        f._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"kaputt")))
        assert await f._fetch_feed(src) == 0                    # ohne jeden Eintrag bleibt es ein Fehler
        assert db.source_states()["alpha"]["last_error"] == "Feed enthält keine Einträge"
    finally:
        await f.close()


def test_feed_rows_keep_filter_count_across_processes():
    import pickle
    src = Source(id="alpha", name="Alpha", country="DE", lang="de", feed=INDEX_URL, homepage=PUBLIC,
                 include_paths=("^/reutlingen",))
    rows = pickle.loads(pickle.dumps(parse_feed(src, _rss([art(1), art(2, "welt"), art(3, "land")]))))
    assert len(rows) == 1 and rows.filtered_out == 2
