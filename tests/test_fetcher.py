"""Fetcher: Feed-Parsing, bedingte Abrufe, erneute Extraktion, DNS-Fehler und Rebinding-Schutz."""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

import app.extract as extract_mod
import app.fetcher as fetcher_mod
from app.fetcher import Fetcher, parse_feed
from app.netguard import BlockedURL, server_addr_blocked
from app.sources import Catalog, Source
from conftest import ago, make_row

PUBLIC = "http://93.184.216.34"
SRC = Source(id="alpha", name="Alpha", country="DE", lang="de", feed=f"{PUBLIC}/feed", homepage=PUBLIC)


def make_fetcher(db, settings, handler) -> Fetcher:
    f = Fetcher(db, Catalog([SRC]), settings)
    f._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    return f


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(fetcher_mod, "PER_DOMAIN_DELAY", 0)


def rss(*items: str) -> bytes:
    return ("<?xml version='1.0'?><rss version='2.0'><channel><title>T</title>"
            + "".join(items) + "</channel></rss>").encode()


def item(n: int, *, link: str | None = "default", date: str | None = "Mon, 01 Sep 2026 10:00:00 GMT") -> str:
    link = f"https://alpha.example/a/{n}" if link == "default" else link
    return ("<item>" + f"<title>Artikel {n}</title>" + f"<guid>g{n}</guid>"
            + (f"<link>{link}</link>" if link else "") + (f"<pubDate>{date}</pubDate>" if date else "")
            + "</item>")


# ---- parse_feed -------------------------------------------------------------

def test_parse_feed_does_not_open_local_paths(tmp_path):
    feed = tmp_path / "lokal.xml"
    feed.write_bytes(rss(item(1)))
    assert len(parse_feed(SRC, feed.read_bytes())) == 1
    # Ein Body, der wie ein Dateipfad aussieht, darf nicht als Datei geöffnet werden.
    assert parse_feed(SRC, str(feed).encode()) == []


def test_parse_feed_dates_and_links():
    future = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    before = datetime.now(timezone.utc).replace(microsecond=0)
    rows = parse_feed(SRC, rss(item(1), item(2, link=None), item(3, link="javascript:x"),
                               item(4, date=future), item(5, date=None)))
    after = datetime.now(timezone.utc)
    by_guid = {r["guid"]: r for r in rows}
    assert set(by_guid) == {"g1", "g4", "g5"}  # ohne (gültigen) Link übersprungen
    assert by_guid["g1"]["published_at"] == "2026-09-01T10:00:00+00:00"
    for g in ("g4", "g5"):  # Zukunft auf jetzt begrenzt, fehlendes Datum = jetzt
        assert before <= datetime.fromisoformat(by_guid[g]["published_at"]) <= after


def test_parse_feed_drops_placeholder_summary_and_author():
    def entry(n, extra):
        return (f"<item><title>Artikel {n}</title><guid>g{n}</guid>"
                f"<link>https://alpha.example/a/{n}</link>{extra}</item>")
    rows = parse_feed(SRC, rss(
        entry(1, "<description>None</description><author>None</author>"),
        entry(2, "<description> null </description><author>undefined</author>"),
        entry(3, "<description>Artikel 3</description>"),              # nur der Titel
        entry(4, "<description>Echter Teaser</description><author>Anna</author>"),
    ))
    by_guid = {r["guid"]: r for r in rows}
    assert [by_guid[f"g{i}"]["summary"] for i in (1, 2, 3)] == ["", "", ""]
    assert by_guid["g1"]["author"] is None and by_guid["g2"]["author"] is None
    assert by_guid["g4"]["summary"] == "Echter Teaser" and by_guid["g4"]["author"] == "Anna"


def test_migration_v5_clears_placeholders(tmp_path):
    from app.db import Database
    d = Database(tmp_path / "k.db")
    d.upsert_articles([make_row("alpha", 1, ago(hours=1), summary="None", author="null"),
                       make_row("alpha", 2, ago(hours=1), summary="Undefined "),
                       make_row("alpha", 3, ago(hours=1), summary="Nonetheless", author="Anna")])
    d.set_meta("schema_version", "4")
    d.close()
    for _ in range(2):  # mehrfach ausführbar
        d = Database(tmp_path / "k.db")
        rows = {r["guid"]: (r["summary"], r["author"]) for r in d.q("SELECT guid, summary, author FROM articles")}
        assert rows == {"alpha-1": ("", None), "alpha-2": ("", None), "alpha-3": ("Nonetheless", "Anna")}
        idx = {r["name"] for r in d.q("PRAGMA index_list(articles)")}
        assert "idx_articles_read_paywalled_source" not in idx and "idx_articles_published" in idx
        d.close()


# ---- _fetch_feed ------------------------------------------------------------

async def test_fetch_feed_conditional_and_retention(db, settings):
    old = (datetime.now(timezone.utc) - timedelta(days=200)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    recent = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    seen = []

    def handler(request):
        seen.append(dict(request.headers))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, headers={"etag": '"v1"', "last-modified": "Mon, 01 Sep 2026 10:00:00 GMT"},
                              content=rss(item(1, date=recent), item(2, date=old)))

    f = make_fetcher(db, settings, handler)
    assert await f._fetch_feed(SRC) == 1  # der 200 Tage alte Eintrag wird gar nicht angelegt
    assert [r["guid"] for r in db.q("SELECT guid FROM articles")] == ["g1"]
    st = db.source_states()["alpha"]
    assert st["etag"] == '"v1"' and st["last_error"] is None

    assert await f._fetch_feed(SRC) == 0
    assert seen[1]["if-none-match"] == '"v1"'
    assert seen[1]["if-modified-since"] == "Mon, 01 Sep 2026 10:00:00 GMT"
    st = db.source_states()["alpha"]
    assert st["last_error"] is None and st["last_new"] == 0 and st["etag"] == '"v1"'


# ---- Extraktion -------------------------------------------------------------

def _article(db, url=f"{PUBLIC}/artikel") -> int:
    db.upsert_articles([make_row("alpha", 1, ago(hours=1), url=url)])
    return db.one("SELECT id FROM articles")["id"]


async def test_failed_retry_keeps_existing_text(db, settings):
    aid = _article(db)
    db.store_extraction(aid, status="partial", body_html="<p>Anfang</p>", body_text="Anfang",
                        body_len=6, paywalled=True)
    await make_fetcher(db, settings, lambda r: httpx.Response(403)).extract_article(aid, f"{PUBLIC}/artikel",
                                                                                     priority=True)
    row = db.get_article(aid)
    assert row["text_status"] == "partial" and row["body_text"] == "Anfang" and row["body_len"] == 6
    assert row["body_html"] == "<p>Anfang</p>" and row["text_error"] == "HTTP 403"


@pytest.mark.parametrize("feed_image,page_image,expected", [
    ("https://i.example/bild.jpg?width=140", "https://i.example/bild.jpg?width=1200", "https://i.example/bild.jpg?width=1200"),
    ("https://i.example/bild.jpg?width=640", "https://i.example/gross.jpg", "https://i.example/bild.jpg?width=640"),
    ("https://i.example/bild.jpg?width=140", "https://i.example/logo.jpg?width=100", "https://i.example/bild.jpg?width=140"),
    ("https://i.example/bild.jpg?width=140", None, "https://i.example/bild.jpg?width=140"),
    (None, "https://i.example/gross.jpg", "https://i.example/gross.jpg"),
])
async def test_small_feed_image_is_replaced_by_page_image(db, settings, monkeypatch, feed_image, page_image, expected):
    db.upsert_articles([make_row("alpha", 1, ago(hours=1), url=f"{PUBLIC}/artikel", image_url=feed_image)])
    aid = db.one("SELECT id FROM articles")["id"]
    f = make_fetcher(db, settings, lambda r: httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>x</p>"))

    async def fake_extract(page, url):
        return extract_mod.Extraction("full", "<p>Text</p>", "Text", 4, False, page_image, None)

    monkeypatch.setattr(f, "_run_extract", fake_extract)
    await f.extract_article(aid, f"{PUBLIC}/artikel", priority=True)
    assert db.get_article(aid)["image_url"] == expected


async def test_page_image_does_not_replace_image_of_already_extracted_article(db, settings, monkeypatch):
    small = "https://i.example/bild.jpg?width=140"
    db.upsert_articles([make_row("alpha", 1, ago(hours=1), url=f"{PUBLIC}/artikel", image_url=small)])
    aid = db.one("SELECT id FROM articles")["id"]
    db.store_extraction(aid, status="full", body_html="<p>alt</p>", body_text="alt", body_len=3, paywalled=False)
    f = make_fetcher(db, settings, lambda r: httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>x</p>"))

    async def fake_extract(page, url):
        return extract_mod.Extraction("full", "<p>Text</p>", "Text", 4, False, "https://i.example/gross.jpg", None)

    monkeypatch.setattr(f, "_run_extract", fake_extract)
    await f._extract(aid, f"{PUBLIC}/artikel")
    row = db.get_article(aid)
    assert row["body_text"] == "Text" and row["image_url"] == small


async def test_same_article_is_never_extracted_twice_concurrently(db, settings):
    aid = _article(db)
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        await asyncio.sleep(0.05)
        return httpx.Response(503)

    f = make_fetcher(db, settings, handler)
    await asyncio.gather(
        f.extract_article(aid, f"{PUBLIC}/artikel"),
        f.extract_article(aid, f"{PUBLIC}/artikel", priority=True),
    )
    assert calls == ["/artikel"]
    assert db.get_article(aid)["attempts"] == 1
    assert f._inflight == {}


async def test_background_skips_article_done_meanwhile(db, settings):
    aid = _article(db)
    db.store_extraction(aid, status="full", body_html="<p>x</p>", body_text="x", body_len=1, paywalled=False)
    calls = []
    f = make_fetcher(db, settings, lambda r: calls.append(1) or httpx.Response(503))
    await f.extract_article(aid, f"{PUBLIC}/artikel")
    assert calls == [] and db.get_article(aid)["text_status"] == "full"


async def test_dns_error_is_transient(db, settings, monkeypatch):
    async def fail(self, host, port, *args, **kwargs):
        raise socket.gaierror(socket.EAI_AGAIN, "vorübergehend")

    monkeypatch.setattr(type(asyncio.get_running_loop()), "getaddrinfo", fail)
    aid = _article(db, url="https://news.example/artikel")
    f = make_fetcher(db, settings, lambda r: httpx.Response(200))
    await f.extract_article(aid, "https://news.example/artikel")
    row = db.get_article(aid)
    assert row["text_status"] == "pending" and row["attempts"] == 1
    assert "ResolveError" in row["text_error"]


@pytest.mark.parametrize("url,handler_exc", [
    ("http://[::1/artikel", None),                          # ValueError aus urlparse
    ("https://news.example:99999/artikel", None),           # Port außerhalb des Bereichs
    (f"{PUBLIC}/artikel", httpx.InvalidURL("kaputt")),
    (f"{PUBLIC}/artikel", httpx.UnsupportedProtocol("ftp")),
])
async def test_invalid_urls_fail_permanently(db, settings, url, handler_exc):
    aid = _article(db, url=url)

    def handler(request):
        if handler_exc is not None:
            raise handler_exc
        return httpx.Response(200)

    await make_fetcher(db, settings, handler).extract_article(aid, url, priority=True)
    row = db.get_article(aid)
    assert row["text_status"] == "failed" and row["attempts"] == 0, row["text_error"]
    assert row["text_error"].startswith("Ungültige URL")


async def test_overall_deadline_covers_second_attempt(db, settings, monkeypatch):
    monkeypatch.setattr(fetcher_mod, "OVERALL_TIMEOUT", 0.3)

    async def handler(request):
        await asyncio.sleep(0.2)
        return httpx.Response(403)  # löst den zweiten Versuch mit anderem User-Agent aus

    f = make_fetcher(db, settings, handler)
    with pytest.raises(TimeoutError):
        await f._get(f"{PUBLIC}/x", max_bytes=1000)


def test_domain_state_is_pruned(db, settings):
    f = make_fetcher(db, settings, lambda r: httpx.Response(200))
    now = time.monotonic()
    f._domain_last.update({"alt.example": now - fetcher_mod.DOMAIN_STATE_TTL - 1, "neu.example": now})
    f._domain_locks["alt.example"]
    f._domain_locks["neu.example"]
    f._prune_domains()
    assert set(f._domain_last) == {"neu.example"} and set(f._domain_locks) == {"neu.example"}


# ---- DNS-Rebinding ----------------------------------------------------------

@pytest.mark.parametrize("addr,blocked", [
    (("93.184.216.34", 443), False),
    (("2606:4700:4700::1111", 443, 0, 0), False),
    (("127.0.0.1", 80), True),
    (("10.1.2.3", 443), True),
    (("::ffff:192.168.0.1", 443, 0, 0), True),
    (("fe80::1%eth0", 443, 0, 2), True),
    (None, False),          # unbekannt: keine Prüfung möglich
    ("kaputt", True),
    ((), True),
    (("keine-ip", 80), True),
])
def test_server_addr_classification(addr, blocked):
    assert server_addr_blocked(addr) is blocked


@pytest.fixture
def local_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b"<html>geheim</html>"
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


async def test_connection_to_private_peer_is_blocked(db, settings, monkeypatch, local_server):
    # Simuliert Rebinding: Die Namensprüfung sagt "öffentlich", die Verbindung landet aber lokal.
    async def ok(url):
        return None

    monkeypatch.setattr(fetcher_mod, "check_url", ok)
    f = Fetcher(db, Catalog([]), settings)
    await f.start()
    try:
        with pytest.raises(BlockedURL, match="nicht erlaubter Adresse"):
            await f._fetch_once(f"{local_server}/", headers=None, max_bytes=1000)
    finally:
        await f.close()


# ---- Abruf auf Anforderung vs. Hintergrund-Warteschlange ----------------------

async def test_priority_request_does_not_wait_behind_background_queue(db, settings):
    from dataclasses import replace

    urls = [f"{PUBLIC}/a/{i}" for i in range(4)]
    db.upsert_articles([make_row("alpha", i, ago(hours=i), url=u) for i, u in enumerate(urls)])
    ids = {r["url"]: r["id"] for r in db.q("SELECT id, url FROM articles")}
    target = urls[-1]  # ältester Artikel: steht im Hintergrundlauf ganz hinten
    gate = asyncio.Event()
    calls: list[str] = []

    async def handler(request):
        calls.append(str(request.url))
        if str(request.url) != target:
            await gate.wait()  # Hintergrund hängt am ersten Artikel fest
        return httpx.Response(503)

    f = make_fetcher(db, replace(settings, extract_concurrency=1), handler)
    bg = asyncio.create_task(f._extract_pending())
    await asyncio.sleep(0.05)
    assert calls == [urls[0]] and ids[target] not in f._inflight
    # Ohne Fix hinge der Abruf an der wartenden Hintergrund-Aufgabe (bis zum Timeout).
    await asyncio.wait_for(f.extract_article(ids[target], target, priority=True), timeout=2)
    assert db.get_article(ids[target])["attempts"] == 1
    gate.set()
    await bg
    # Der Hintergrund holt den Artikel danach nicht noch einmal: nicht mehr fällig.
    assert calls.count(target) == 1
    assert db.get_article(ids[target])["attempts"] == 1
    assert f._inflight == {}


async def test_stale_background_task_respects_due_time(db, settings):
    aid = _article(db)
    calls = []
    f = make_fetcher(db, settings, lambda r: calls.append(1) or httpx.Response(503))
    await f.extract_article(aid, f"{PUBLIC}/artikel", priority=True)
    row = db.get_article(aid)
    assert row["attempts"] == 1 and row["next_attempt_at"] > ago(seconds=0)
    await f.extract_article(aid, f"{PUBLIC}/artikel")  # aus einer älteren Liste, nicht mehr fällig
    assert calls == [1] and db.get_article(aid)["attempts"] == 1


async def test_background_skips_while_priority_runs(db, settings):
    aid = _article(db)
    gate = asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(1)
        await gate.wait()
        return httpx.Response(503)

    f = make_fetcher(db, settings, handler)
    prio = asyncio.create_task(f.extract_article(aid, f"{PUBLIC}/artikel", priority=True))
    await asyncio.sleep(0.02)
    await f.extract_article(aid, f"{PUBLIC}/artikel")  # kehrt sofort zurück, kein zweiter Abruf
    gate.set()
    await prio
    assert calls == [1] and db.get_article(aid)["attempts"] == 1 and f._inflight == {}


# ---- Kompression --------------------------------------------------------------

class RawStream(httpx.AsyncByteStream):
    """Liefert rohe Bytes in Stücken wie eine echte Verbindung (httpx liest sie nicht vorab)."""

    def __init__(self, data: bytes, chunk: int = 16384):
        self.data, self.chunk = data, chunk

    async def __aiter__(self):
        for i in range(0, len(self.data), self.chunk):
            yield self.data[i:i + self.chunk]


def _gzip(data: bytes) -> bytes:
    import gzip
    return gzip.compress(data, 9)


async def test_requests_identity_and_caps_gzip_bomb(db, settings, monkeypatch):
    bomb = _gzip(b"\0" * (50 * 1024 * 1024))
    assert len(bomb) < 100_000
    seen = []
    decoded_sizes = []
    real = fetcher_mod.zlib.decompressobj

    class Spy:
        def __init__(self, *a):
            self.d = real(*a)

        def decompress(self, data, max_length=0):
            out = self.d.decompress(data, max_length)
            decoded_sizes.append(len(out))
            return out

        def flush(self):
            return self.d.flush()

    monkeypatch.setattr(fetcher_mod.zlib, "decompressobj", Spy)

    def handler(request):
        seen.append(request.headers.get("accept-encoding"))
        # Server ignoriert "identity" und schickt trotzdem gzip.
        return httpx.Response(200, headers={"content-type": "text/html", "content-encoding": "gzip"},
                              stream=RawStream(bomb))

    f = make_fetcher(db, settings, handler)
    with pytest.raises(fetcher_mod.FetchError, match="zu groß"):
        await f._fetch_once(f"{PUBLIC}/", headers=None, max_bytes=1_000_000)
    assert seen == ["identity"]
    assert sum(decoded_sizes) <= 1_000_001  # nie mehr als ein Byte über dem Limit entpackt


async def test_client_default_headers_request_identity(db, settings):
    f = Fetcher(db, Catalog([]), settings)
    await f.start()
    try:
        assert f._client.headers["accept-encoding"] == "identity"
    finally:
        await f.close()


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "identity", ""])
async def test_small_compressed_bodies_are_decoded(db, settings, encoding):
    import zlib
    body = b"<html><p>Hallo Welt</p></html>"
    raw = {"gzip": _gzip(body), "deflate": zlib.compress(body)}.get(encoding, body)
    headers = {"content-type": "text/html", **({"content-encoding": encoding} if encoding else {})}
    f = make_fetcher(db, settings, lambda r: httpx.Response(200, headers=headers, stream=RawStream(raw, 7)))
    r = await f._fetch_once(f"{PUBLIC}/", headers=None, max_bytes=1000)
    assert r.content == body


async def test_unsupported_encoding_fails_permanently(db, settings):
    aid = _article(db)
    f = make_fetcher(db, settings, lambda r: httpx.Response(
        200, headers={"content-type": "text/html", "content-encoding": "br"}, stream=RawStream(b"\x0b\x02\x80")))
    await f.extract_article(aid, f"{PUBLIC}/artikel", priority=True)
    row = db.get_article(aid)
    assert row["text_status"] == "failed" and "Content-Encoding" in row["text_error"]


def _raw_deflate(data: bytes) -> bytes:
    import zlib
    c = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    return c.compress(data) + c.flush()


@pytest.mark.parametrize("encoding,kind,chunk", [
    ("GZIP", "gzip", 7),
    ("gzip, identity", "gzip", 7),
    ("identity, x-gzip", "gzip", 7),
    ("UTF-8", "plain", 7),              # falsch gesetzter Header: als unkomprimiert behandeln
    ("deflate", "raw", 1),              # rohes deflate ohne zlib-Kopf, auch bei 1-Byte-Stücken
    ("deflate", "raw", 16384),
    ("Deflate", "zlib", 1),
])
async def test_content_encoding_variants(db, settings, encoding, kind, chunk):
    import zlib
    body = b"<html><p>" + b"Hallo Welt " * 50 + b"</p></html>"
    raw = {"gzip": _gzip(body), "plain": body, "raw": _raw_deflate(body), "zlib": zlib.compress(body)}[kind]
    headers = {"content-type": "text/html", "content-encoding": encoding}
    f = make_fetcher(db, settings, lambda r: httpx.Response(200, headers=headers, stream=RawStream(raw, chunk)))
    r = await f._fetch_once(f"{PUBLIC}/", headers=None, max_bytes=100_000)
    assert r.content == body


@pytest.mark.parametrize("encoding", ["br", "zstd", "compress", "gzip, gzip", "deflate, gzip", "gzip, br"])
async def test_rejected_content_encodings(db, settings, encoding):
    headers = {"content-type": "text/html", "content-encoding": encoding}
    f = make_fetcher(db, settings, lambda r: httpx.Response(200, headers=headers, stream=RawStream(b"xx")))
    with pytest.raises(fetcher_mod.UnsupportedEncoding):
        await f._fetch_once(f"{PUBLIC}/", headers=None, max_bytes=1000)


@pytest.mark.parametrize("status,encoding", [(200, "gzip"), (200, "deflate"), (204, "gzip"), (304, "gzip")])
async def test_empty_compressed_body_is_not_truncated(db, settings, status, encoding):
    headers = {"content-type": "text/html", "content-encoding": encoding}
    f = make_fetcher(db, settings, lambda r: httpx.Response(status, headers=headers, stream=RawStream(b"")))
    r = await f._fetch_once(f"{PUBLIC}/", headers=None, max_bytes=1000)
    assert r.status_code == status and r.content == b""


@pytest.mark.parametrize("encoding,cut", [("gzip", 0.5), ("gzip", -8), ("deflate", 0.5)])
async def test_truncated_compressed_body_is_transient_error(db, settings, encoding, cut):
    body = b"<html><p>" + bytes(range(256)) * 40 + b"</p></html>"
    full = _gzip(body) if encoding == "gzip" else _raw_deflate(body)
    raw = full[:int(len(full) * cut)] if cut > 0 else full[:cut]
    headers = {"content-type": "text/html", "content-encoding": encoding}
    f = make_fetcher(db, settings, lambda r: httpx.Response(200, headers=headers, stream=RawStream(raw, 100)))
    with pytest.raises(fetcher_mod.FetchError, match="unvollständig"):
        await f._fetch_once(f"{PUBLIC}/", headers=None, max_bytes=100_000)


# ---- Extraktion im Prozess-Pool mit hartem Zeitlimit ----------------------------

def _sleepy_extract(page, url):
    """Läuft im Worker-Prozess: hängt deutlich länger als das Test-Zeitlimit."""
    import os
    from pathlib import Path
    Path(os.environ["KIOSK_TEST_PIDFILE"]).write_text(str(os.getpid()))
    time.sleep(60)


def _gone_or_zombie(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(")")[-1].split()[0] == "Z"
    except FileNotFoundError:
        return True


ARTICLE_HTML = ("<html><head><title>T</title></head><body><article><h1>Titel</h1>"
                + "<p>Ein ganz normaler Absatz mit genug Text für die Extraktion. </p>" * 30
                + "</article></body></html>")


async def test_hung_extraction_times_out_and_pool_is_recreated(db, settings, monkeypatch, tmp_path):
    pidfile = tmp_path / "pid"
    monkeypatch.setenv("KIOSK_TEST_PIDFILE", str(pidfile))
    monkeypatch.setattr(fetcher_mod, "EXTRACT_TIMEOUT", 1.5)
    db.upsert_articles([make_row("alpha", 1, ago(hours=1), url=f"{PUBLIC}/a/1")])
    first = db.one("SELECT id FROM articles")["id"]
    f = make_fetcher(db, settings, lambda r: httpx.Response(
        200, headers={"content-type": "text/html"}, content=ARTICLE_HTML.encode()))
    try:
        monkeypatch.setattr(fetcher_mod, "extract", _sleepy_extract)
        old_pool = f._extraction_pool()
        # Der Sammellauf kommt nach dem Zeitlimit weiter, statt 60 s zu hängen.
        assert await asyncio.wait_for(f._extract_pending(), timeout=15) == 1
        row = db.get_article(first)
        assert row["text_status"] == "pending" and row["attempts"] == 1
        assert row["text_error"] == "Extraktion Zeitüberschreitung"
        assert f._pool is None  # verworfen, wird beim nächsten Auftrag neu angelegt
        pid = int(pidfile.read_text())
        for _ in range(50):
            if _gone_or_zombie(pid):
                break
            await asyncio.sleep(0.1)
        assert _gone_or_zombie(pid), "hängender Worker wurde nicht beendet"

        # Zweite Extraktion mit neuem Pool funktioniert.
        monkeypatch.setattr(fetcher_mod, "extract", extract_mod.extract)
        db.upsert_articles([make_row("alpha", 2, ago(minutes=5), url=f"{PUBLIC}/a/2")])
        second = db.one("SELECT id FROM articles WHERE guid='alpha-2'")["id"]
        await asyncio.wait_for(f.extract_article(second, f"{PUBLIC}/a/2"), timeout=30)
        assert f._pool is not None and f._pool is not old_pool
        assert db.get_article(second)["text_status"] in ("full", "partial")
    finally:
        await f.close()
    assert f._pool is None


async def test_broken_pool_is_transient_and_replaced(db, settings, monkeypatch):
    from concurrent.futures.process import BrokenProcessPool
    aid = _article(db)
    f = make_fetcher(db, settings, lambda r: httpx.Response(
        200, headers={"content-type": "text/html"}, content=ARTICLE_HTML.encode()))

    class Broken:
        def submit(self, *a, **kw):
            raise BrokenProcessPool("Worker gestorben")

        def shutdown(self, **kw):
            pass

    broken = Broken()
    f._pool = broken
    await f.extract_article(aid, f"{PUBLIC}/artikel")
    row = db.get_article(aid)
    assert row["text_status"] == "pending" and row["attempts"] == 1 and "abgebrochen" in row["text_error"]
    assert f._pool is None


def _timed_extract(page, url):
    """Läuft im Worker-Prozess: schläft so viele Sekunden, wie das letzte URL-Segment angibt."""
    from app.extract import extract
    time.sleep(float(url.rsplit("/", 1)[-1]))
    return extract(page, url)


async def test_timeout_measures_run_time_not_queue_time(db, settings, monkeypatch):
    # 2 Worker, 6 Aufträge zu je 0,8 s bei 2 s Limit: Ohne Warteschlangen-Grenze liefen die
    # letzten beiden (1,6 s Wartezeit + 0,8 s) ins Zeitlimit.
    monkeypatch.setattr(fetcher_mod, "EXTRACT_TIMEOUT", 2)
    monkeypatch.setattr(fetcher_mod, "extract", _timed_extract)
    f = make_fetcher(db, settings, lambda r: httpx.Response(200))
    try:
        page = ARTICLE_HTML.encode()
        await asyncio.gather(*(f._run_extract(page, f"{PUBLIC}/d/0") for _ in range(2)))  # Pool warm
        pool = f._pool
        results = await asyncio.wait_for(
            asyncio.gather(*(f._run_extract(page, f"{PUBLIC}/d/0.8") for _ in range(6))), timeout=30)
        assert len(results) == 6 and all(r.status in ("full", "partial") for r in results)
        assert f._pool is pool  # nie verworfen
    finally:
        await f.close()


async def test_hung_job_only_counts_for_itself(db, settings, monkeypatch):
    """Ein hängender Auftrag: nur er zählt einen Versuch; der laufende Nachbar wird ohne Zählung
    neu eingeplant (auch als Abruf auf Anforderung ohne CancelledError), wartende laufen danach."""
    monkeypatch.setattr(fetcher_mod, "EXTRACT_TIMEOUT", 2)
    monkeypatch.setattr(fetcher_mod, "extract", _timed_extract)
    urls = {"hung": f"{PUBLIC}/d/60", "nb": f"{PUBLIC}/d/1.6", "w1": f"{PUBLIC}/d/0.1", "w2": f"{PUBLIC}/d/0.2"}
    db.upsert_articles([make_row("alpha", i, ago(hours=i + 1), url=u) for i, u in enumerate(urls.values())])
    ids = {k: db.one("SELECT id FROM articles WHERE url=?", (u,))["id"] for k, u in urls.items()}
    f = make_fetcher(db, settings, lambda r: httpx.Response(
        200, headers={"content-type": "text/html"}, content=ARTICLE_HTML.encode()))
    try:
        await asyncio.gather(*(f._run_extract(b"<p>x</p>", f"{PUBLIC}/d/0") for _ in range(2)))  # Pool warm
        hung = asyncio.create_task(f.extract_article(ids["hung"], urls["hung"]))
        await asyncio.sleep(0.8)
        nb = asyncio.create_task(f.extract_article(ids["nb"], urls["nb"], priority=True))
        await asyncio.sleep(0.1)
        waiting = [asyncio.create_task(f.extract_article(ids[k], urls[k])) for k in ("w1", "w2")]
        await asyncio.wait_for(asyncio.gather(hung, nb, *waiting), timeout=30)

        row = db.get_article(ids["hung"])
        assert row["text_status"] == "pending" and row["attempts"] == 1
        assert row["text_error"] == "Extraktion Zeitüberschreitung"
        row = db.get_article(ids["nb"])
        assert row["text_status"] == "pending" and row["attempts"] == 0 and row["next_attempt_at"] is None
        assert "Pool neu gestartet" in row["text_error"]
        for k in ("w1", "w2"):
            assert db.get_article(ids[k])["text_status"] in ("full", "partial")
    finally:
        await f.close()


async def test_cancelled_pool_future_is_not_a_cancelled_request(db, settings):
    """Bricht der Pool einen Auftrag ab (shutdown mit cancel_futures), kommt beim Aufrufer kein
    CancelledError an, sondern ein normaler vorübergehender Fehler."""
    import concurrent.futures
    aid = _article(db)
    f = make_fetcher(db, settings, lambda r: httpx.Response(
        200, headers={"content-type": "text/html"}, content=ARTICLE_HTML.encode()))

    class Cancelling:
        def submit(self, *a, **kw):
            fut = concurrent.futures.Future()
            asyncio.get_running_loop().call_later(0.05, fut.cancel)
            return fut

        def shutdown(self, **kw):
            pass

    f._pool = Cancelling()
    await f.extract_article(aid, f"{PUBLIC}/artikel", priority=True)
    row = db.get_article(aid)
    assert row["text_status"] == "pending" and row["attempts"] == 1 and "abgebrochen" in row["text_error"]
    assert f._pool is None

    # Gehört der Pool zu den wegen Zeitüberschreitung verworfenen: ohne Zählung neu eingeplant.
    db.q("UPDATE articles SET next_attempt_at=NULL WHERE id=?", (aid,))
    pool = Cancelling()
    f._pool = pool
    f._discarded.add(pool)
    await f.extract_article(aid, f"{PUBLIC}/artikel", priority=True)
    row = db.get_article(aid)
    assert row["attempts"] == 1 and row["next_attempt_at"] is None


# ---- _extract_pending: vollständig und robust ------------------------------------

async def test_extract_pending_pages_past_articles_that_stay_pending(db, settings, monkeypatch):
    # 150 fällige Artikel, die Hälfte mit identischem Zeitstempel (Keyset braucht die ID als Tiebreaker).
    same = ago(hours=1)
    db.upsert_articles([make_row("alpha", i, same if i % 2 else ago(minutes=i)) for i in range(150)])
    f = make_fetcher(db, settings, lambda r: httpx.Response(503))
    calls: list[int] = []

    async def stays_pending(article_id, url, *, priority=False):
        calls.append(article_id)  # nichts gespeichert: bleibt pending und fällig

    monkeypatch.setattr(f, "extract_article", stays_pending)
    assert await f._extract_pending() == 150
    assert len(calls) == 150 and len(set(calls)) == 150


async def test_unexpected_extraction_error_does_not_abort_run(db, settings, monkeypatch):
    db.upsert_articles([make_row("alpha", i, ago(hours=i + 1)) for i in range(3)])
    db.upsert_articles([make_row("alpha", 99, ago(days=200))])  # wird am Ende gelöscht
    ids = [r["id"] for r in db.pending_articles(10)]
    f = make_fetcher(db, settings, lambda r: httpx.Response(503))
    calls: list[int] = []

    async def flaky(article_id, url, *, priority=False):
        calls.append(article_id)
        if article_id == ids[1]:
            raise RuntimeError("unerwartet")

    async def no_feeds():
        return 0

    monkeypatch.setattr(f, "extract_article", flaky)
    monkeypatch.setattr(f, "_fetch_all_feeds", no_feeds)
    await f.run_once()
    assert sorted(calls) == sorted(ids)
    assert f.state["runs"] == 1 and f.state["last_purged"] == 1 and f.state["running"] is False
