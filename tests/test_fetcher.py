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


async def on_demand(f: Fetcher, article_id: int, url: str) -> None:
    """Extraktion auf Anforderung wie über die API (start_extraction), wartet auf ihr Ende."""
    assert f.start_extraction(article_id, url)
    task = f._inflight.get(article_id)
    if task is not None:
        await asyncio.shield(task)


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
        assert "idx_articles_read_paywalled_source" not in idx and "idx_articles_list" in idx and "idx_articles_published" not in idx
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
    try:
        assert await f._fetch_feed(SRC) == 1  # der 200 Tage alte Eintrag wird gar nicht angelegt
        assert [r["guid"] for r in db.q("SELECT guid FROM articles")] == ["g1"]
        st = db.source_states()["alpha"]
        assert st["etag"] == '"v1"' and st["last_error"] is None

        assert await f._fetch_feed(SRC) == 0
        assert seen[1]["if-none-match"] == '"v1"'
        assert seen[1]["if-modified-since"] == "Mon, 01 Sep 2026 10:00:00 GMT"
        st = db.source_states()["alpha"]
        assert st["last_error"] is None and st["last_new"] == 0 and st["etag"] == '"v1"'
    finally:
        await f.close()  # parse_feed lief im Prozess-Pool


# ---- Extraktion -------------------------------------------------------------

def _article(db, url=f"{PUBLIC}/artikel") -> int:
    db.upsert_articles([make_row("alpha", 1, ago(hours=1), url=url)])
    return db.one("SELECT id FROM articles")["id"]


async def test_failed_retry_keeps_existing_text(db, settings):
    aid = _article(db)
    db.store_extraction(aid, status="partial", body_html="<p>Anfang</p>", body_text="Anfang",
                        body_len=6, paywalled=True)
    await on_demand(make_fetcher(db, settings, lambda r: httpx.Response(403)), aid, f"{PUBLIC}/artikel")
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

    async def fake_extract(page, url, skip=None):
        return extract_mod.Extraction("full", "<p>Text</p>", "Text", 4, False, page_image, None)

    monkeypatch.setattr(f, "_run_extract", fake_extract)
    await on_demand(f, aid, f"{PUBLIC}/artikel")
    assert db.get_article(aid)["image_url"] == expected


async def test_page_image_does_not_replace_image_of_already_extracted_article(db, settings, monkeypatch):
    small = "https://i.example/bild.jpg?width=140"
    db.upsert_articles([make_row("alpha", 1, ago(hours=1), url=f"{PUBLIC}/artikel", image_url=small)])
    aid = db.one("SELECT id FROM articles")["id"]
    db.store_extraction(aid, status="full", body_html="<p>alt</p>", body_text="alt", body_len=3, paywalled=False)
    f = make_fetcher(db, settings, lambda r: httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>x</p>"))

    async def fake_extract(page, url, skip=None):
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
        on_demand(f, aid, f"{PUBLIC}/artikel"),
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
    assert row["text_error"].startswith("Name nicht auflösbar")


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

    await on_demand(make_fetcher(db, settings, handler), aid, url)
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
    await asyncio.wait_for(on_demand(f, ids[target], target), timeout=2)
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
    await on_demand(f, aid, f"{PUBLIC}/artikel")
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
    prio = asyncio.create_task(on_demand(f, aid, f"{PUBLIC}/artikel"))
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
    await on_demand(f, aid, f"{PUBLIC}/artikel")
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

def _pidfile(token: str):
    import tempfile
    from pathlib import Path
    return Path(tempfile.gettempdir()) / f"kiosk-test-{token}.pid"


def _sleepy_extract(page, url):
    """Läuft im Worker-Prozess: hängt deutlich länger als das Test-Zeitlimit. Schreibt seine PID
    in die Datei zum letzten URL-Segment (die Umgebung der Worker stammt vom Forkserver, der
    schon vor diesem Test gestartet sein kann)."""
    import os
    _pidfile(url.rsplit("/", 1)[-1]).write_text(str(os.getpid()))
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


async def test_hung_extraction_times_out_and_only_its_process_is_killed(db, settings, monkeypatch):
    import uuid
    token = uuid.uuid4().hex
    pidfile = _pidfile(token)
    monkeypatch.setattr(fetcher_mod, "EXTRACT_TIMEOUT", 1.5)
    db.upsert_articles([make_row("alpha", 1, ago(hours=1), url=f"{PUBLIC}/a/{token}")])
    first = db.one("SELECT id FROM articles")["id"]
    f = make_fetcher(db, settings, lambda r: httpx.Response(
        200, headers={"content-type": "text/html"}, content=ARTICLE_HTML.encode()))
    try:
        monkeypatch.setattr(fetcher_mod, "extract", _sleepy_extract)
        # Der Sammellauf kommt nach dem Zeitlimit weiter, statt 60 s zu hängen.
        assert await asyncio.wait_for(f._extract_pending(), timeout=15) == 1
        row = db.get_article(first)
        assert row["text_status"] == "pending" and row["attempts"] == 1
        assert row["text_error"] == "Extraktion: Zeitüberschreitung"
        pid = int(pidfile.read_text())
        pidfile.unlink()
        for _ in range(50):
            if _gone_or_zombie(pid):
                break
            await asyncio.sleep(0.1)
        assert _gone_or_zombie(pid), "hängender Worker wurde nicht beendet"
        assert not f._pids

        # Die nächste Extraktion bekommt ihren eigenen Prozess und funktioniert.
        monkeypatch.setattr(fetcher_mod, "extract", extract_mod.extract)
        db.upsert_articles([make_row("alpha", 2, ago(minutes=5), url=f"{PUBLIC}/a/2")])
        second = db.one("SELECT id FROM articles WHERE guid='alpha-2'")["id"]
        await asyncio.wait_for(f.extract_article(second, f"{PUBLIC}/a/2"), timeout=30)
        assert db.get_article(second)["text_status"] in ("full", "partial")
    finally:
        await f.close()


def _crashing_extract(page, url):
    """Läuft im Worker-Prozess: stirbt ohne Ergebnis (wie nach SIGKILL durch den OOM-Killer)."""
    import os
    import signal
    os.kill(os.getpid(), signal.SIGKILL)


def _hungry_extract(page, url):
    """Läuft im Worker-Prozess: will weit mehr Speicher als JOB_MEMORY_LIMIT."""
    return bytearray(2 * 1024 ** 3)


async def test_crashed_worker_counts_one_attempt(db, settings, monkeypatch):
    aid = _article(db)
    f = make_fetcher(db, settings, lambda r: httpx.Response(
        200, headers={"content-type": "text/html"}, content=ARTICLE_HTML.encode()))
    monkeypatch.setattr(fetcher_mod, "extract", _crashing_extract)
    try:
        await asyncio.wait_for(f.extract_article(aid, f"{PUBLIC}/artikel"), timeout=30)
    finally:
        await f.close()
    row = db.get_article(aid)
    # Der Absturz gehört eindeutig zu diesem Auftrag (eigener Prozess): ein Versuch, später erneut.
    assert row["text_status"] == "pending" and row["attempts"] == 1
    assert "Worker-Prozess beendet" in row["text_error"]


async def test_memory_limit_fails_only_that_job(db, settings, monkeypatch):
    aid = _article(db)
    f = make_fetcher(db, settings, lambda r: httpx.Response(
        200, headers={"content-type": "text/html"}, content=ARTICLE_HTML.encode()))
    monkeypatch.setattr(fetcher_mod, "extract", _hungry_extract)
    try:
        await asyncio.wait_for(f.extract_article(aid, f"{PUBLIC}/artikel"), timeout=30)
        row = db.get_article(aid)
        assert row["text_status"] == "failed" and "zu viel Speicher" in row["text_error"]
        # Der nächste Auftrag läuft normal.
        monkeypatch.setattr(fetcher_mod, "extract", extract_mod.extract)
        result = await f._run_extract(ARTICLE_HTML.encode(), f"{PUBLIC}/x")
        assert result.status in ("full", "partial")
    finally:
        await f.close()


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
        await asyncio.gather(*(f._run_extract(page, f"{PUBLIC}/d/0") for _ in range(2)))  # Forkserver warm
        results = await asyncio.wait_for(
            asyncio.gather(*(f._run_extract(page, f"{PUBLIC}/d/0.8") for _ in range(6))), timeout=30)
        assert len(results) == 6 and all(r.status in ("full", "partial") for r in results)
    finally:
        await f.close()


async def test_hung_job_does_not_touch_its_neighbours(db, settings, monkeypatch):
    """Ein hängender Auftrag: nur er zählt einen Versuch; der gleichzeitig laufende Nachbar (hier ein
    Abruf auf Anforderung) und die wartenden laufen ungestört zu Ende (je Auftrag ein Prozess)."""
    monkeypatch.setattr(fetcher_mod, "EXTRACT_TIMEOUT", 3)   # Luft: jeder Worker importiert das Testmodul neu
    monkeypatch.setattr(fetcher_mod, "extract", _timed_extract)
    urls = {"hung": f"{PUBLIC}/d/60", "nb": f"{PUBLIC}/d/1.6", "w1": f"{PUBLIC}/d/0.1", "w2": f"{PUBLIC}/d/0.2"}
    db.upsert_articles([make_row("alpha", i, ago(hours=i + 1), url=u) for i, u in enumerate(urls.values())])
    ids = {k: db.one("SELECT id FROM articles WHERE url=?", (u,))["id"] for k, u in urls.items()}
    f = make_fetcher(db, settings, lambda r: httpx.Response(
        200, headers={"content-type": "text/html"}, content=ARTICLE_HTML.encode()))
    try:
        await asyncio.gather(*(f._run_extract(b"<p>x</p>", f"{PUBLIC}/d/0") for _ in range(2)))  # warm
        hung = asyncio.create_task(f.extract_article(ids["hung"], urls["hung"]))
        await asyncio.sleep(0.8)
        nb = asyncio.create_task(on_demand(f, ids["nb"], urls["nb"]))
        await asyncio.sleep(0.1)
        waiting = [asyncio.create_task(f.extract_article(ids[k], urls[k])) for k in ("w1", "w2")]
        await asyncio.wait_for(asyncio.gather(hung, nb, *waiting), timeout=30)

        row = db.get_article(ids["hung"])
        assert row["text_status"] == "pending" and row["attempts"] == 1
        assert row["text_error"] == "Extraktion: Zeitüberschreitung"
        for k in ("nb", "w1", "w2"):
            row = db.get_article(ids[k])
            assert row["text_status"] in ("full", "partial") and row["attempts"] == 0, (k, row["text_error"])
    finally:
        await f.close()


async def test_close_kills_running_job(db, settings, monkeypatch):
    """Herunterfahren mitten in einer Extraktion: Der Worker-Prozess endet mit, der Aufrufer bekommt
    CancelledError (kein hängender Prozess, kein gespeicherter Fehlschlag)."""
    import uuid
    token = uuid.uuid4().hex
    monkeypatch.setattr(fetcher_mod, "extract", _sleepy_extract)
    aid = _article(db)
    f = make_fetcher(db, settings, lambda r: httpx.Response(
        200, headers={"content-type": "text/html"}, content=ARTICLE_HTML.encode()))
    task = asyncio.create_task(f._run_extract(ARTICLE_HTML.encode(), f"{PUBLIC}/a/{token}"))
    pidfile = _pidfile(token)
    for _ in range(200):
        if pidfile.exists() and pidfile.read_text():
            break
        await asyncio.sleep(0.05)
    pid = int(pidfile.read_text())
    pidfile.unlink()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await f.close()
    for _ in range(50):
        if _gone_or_zombie(pid):
            break
        await asyncio.sleep(0.1)
    assert _gone_or_zombie(pid)
    assert db.get_article(aid)["attempts"] == 0


async def test_source_breaker_skips_rest_of_source_after_two_timeouts(db, settings, monkeypatch):
    """Nach SOURCE_TROUBLE_LIMIT Zeitüberschreitungen einer Quelle in einem Lauf: Die übrigen Artikel
    dieser Quelle bleiben ohne Versuch fällig, andere Quellen laufen weiter; der nächste Lauf
    beginnt von vorn."""
    monkeypatch.setattr(fetcher_mod, "EXTRACT_TIMEOUT", 1)
    monkeypatch.setattr(fetcher_mod, "extract", _timed_extract)
    rows = [make_row("alpha", i, ago(minutes=i + 1), url=f"{PUBLIC}/alpha{i}/60") for i in range(6)]
    rows.append(make_row("beta", 1, ago(minutes=30), url=f"{PUBLIC}/beta/0"))
    db.upsert_articles(rows)
    f = make_fetcher(db, settings, lambda r: httpx.Response(
        200, headers={"content-type": "text/html"}, content=ARTICLE_HTML.encode()))
    try:
        await asyncio.wait_for(f._extract_pending(), timeout=30)
        alpha = db.q("SELECT attempts, text_status, text_error FROM articles WHERE source_id='alpha'")
        # Zwei Zeitüberschreitungen lösen aus; ein Auftrag, der schon lief, als die zweite kam, darf
        # noch zu Ende laufen (höchstens SOURCE_TROUBLE_LIMIT + gleichzeitige Extraktionen - 1).
        tried = sum(r["attempts"] for r in alpha)
        assert 2 <= tried <= 3 and sorted(r["attempts"] for r in alpha)[-tried:] == [1] * tried
        assert all(r["text_status"] == "pending" for r in alpha)
        assert all(r["text_error"] is None for r in alpha if r["attempts"] == 0)
        assert db.one("SELECT text_status FROM articles WHERE source_id='beta'")["text_status"] in ("full", "partial")
        assert f._tripped("alpha") and not f._tripped("beta")
        # Neuer Lauf: Schalter zurückgesetzt, die vier übersprungenen kommen dran (die zwei mit
        # Versuch erst nach ihrer Wartezeit).
        monkeypatch.setattr(fetcher_mod, "extract", extract_mod.extract)
        await asyncio.wait_for(f._extract_pending(), timeout=30)
        alpha = db.q("SELECT attempts, text_status FROM articles WHERE source_id='alpha'")
        done = [r["text_status"] for r in alpha if r["attempts"] == 0]
        assert len(done) == 6 - tried and all(st in ("full", "partial") for st in done)
    finally:
        await f.close()


# Feindliche Seiten aus dem Pentest (gekürzt auf das Nötige): werden vor trafilatura abgelehnt.
HOSTILE_PAGES = {
    "short_p": b"<html><body><article>" + b"<p>Kurzer Satz.</p>" * 250_000 + b"</article></body></html>",
    "italics": b"<html><body><div>" + b"<i>a</i>" * 650_000 + b"</div></body></html>",
    "deep_div": b"<html><body>" + b"<div>" * 100_000 + b"<p>x</p>" + b"</div>" * 100_000 + b"</body></html>",
    "attrs": b"<html><body><div " + b" ".join(b'a%d="1"' % i for i in range(100_000)) + b"><p>x</p></div></body></html>",
    "bool_attrs": b"<html><body><div " + b" ".join(b"a%d" % i for i in range(30_000)) + b"><p>x</p></div></body></html>",
}


@pytest.mark.parametrize("name", sorted(HOSTILE_PAGES))
def test_markup_guard_rejects_hostile_pages_quickly(name):
    raw = HOSTILE_PAGES[name]
    start = time.perf_counter()
    problem = fetcher_mod.markup_problem(raw, max_tags=fetcher_mod.MAX_PAGE_TAGS,
                                         max_attrs=fetcher_mod.MAX_TAG_ATTRS, max_lt=fetcher_mod.MAX_PAGE_LT)
    assert problem and time.perf_counter() - start < 1


def test_markup_guard_accepts_big_real_world_shapes():
    # Live-Blog-artig: 4 000 Tags, Skripte voller "<", ein Attribut mit JSON voller Leerzeichen und
    # "=" (Guardian props="…"), CDATA-Text, Kommentare, "a < b" im Text.
    props = "{&quot;a&quot;: " + ", ".join(f"&quot;k{i}&quot;: &quot;v = {i} x y z&quot;" for i in range(3000)) + "}"
    page = ("<!DOCTYPE html><html><head><style>a<b{}</style><script>" + "if (a < b) x();" * 20_000
            + "</script></head><body><!-- " + "<x>" * 5000 + " -->"
            + f'<gu-island name="x" props="{props}" data-x=\'{{"a": "b c d e"}}\'></gu-island>'
            + "<article>" + "<p>Absatz mit <a href='/x?a=1&b=2'>Link</a> und 1 < 2.</p>" * 1300
            + "</article></body></html>").encode()
    assert fetcher_mod.markup_problem(page, max_tags=fetcher_mod.MAX_PAGE_TAGS,
                                      max_attrs=fetcher_mod.MAX_TAG_ATTRS, max_lt=fetcher_mod.MAX_PAGE_LT) is None
    feed = ('<?xml version="1.0"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>'
            + "".join(f'<item xmlns:dc="http://purl.org/dc/elements/1.1/"><title>t{i}</title><link>https://a/{i}</link>'
                      f"<description><![CDATA[{'Ein Wort ' * 400}<p>x</p>]]></description></item>" for i in range(300))
            + "</channel></rss>").encode()
    assert _feed_problem(feed) is None


async def test_hostile_page_fails_fast_without_retries(db, settings):
    aid = _article(db)
    f = make_fetcher(db, settings, lambda r: httpx.Response(
        200, headers={"content-type": "text/html"}, content=HOSTILE_PAGES["short_p"]))
    try:
        start = time.perf_counter()
        await asyncio.wait_for(f.extract_article(aid, f"{PUBLIC}/artikel"), timeout=30)
        assert time.perf_counter() - start < 10
    finally:
        await f.close()
    row = db.get_article(aid)
    assert row["text_status"] == "failed" and row["text_error"].startswith("Seite abgelehnt: zu viel Markup")


@pytest.mark.parametrize("body", [
    b"".join(b'<n%d:x xmlns:n%d="urn:%d"/>' % (i, i, i) for i in range(150_000)),     # feedbomb ns
    b"<x " + b" ".join(b'a%d="1"' % i for i in range(300_000)) + b"/>",                 # feedbomb attrs
], ids=["ns", "attrs"])
async def test_hostile_feed_is_rejected_before_feedparser(db, settings, body):
    raw = (b'<rss version="2.0"><channel><item><title>x</title><link>https://a/1</link>' + body
           + b"</item></channel></rss>")[:5 * 1024 * 1024]
    f = make_fetcher(db, settings, lambda r: httpx.Response(200, content=raw))
    try:
        start = time.perf_counter()
        assert await asyncio.wait_for(f._fetch_all_feeds(), timeout=30) == 0
        assert time.perf_counter() - start < 10
        assert db.source_states()["alpha"]["last_error"].startswith("Feed abgelehnt: zu viele")
    finally:
        await f.close()


# ---- _extract_pending: vollständig und robust ------------------------------------

async def test_extract_pending_pages_past_articles_that_stay_pending(db, settings, monkeypatch):
    # 150 fällige Artikel, die Hälfte mit identischem Zeitstempel (Keyset braucht die ID als Tiebreaker).
    same = ago(hours=1)
    db.upsert_articles([make_row("alpha", i, same if i % 2 else ago(minutes=i)) for i in range(150)])
    f = make_fetcher(db, settings, lambda r: httpx.Response(503))
    calls: list[int] = []

    async def stays_pending(article_id, url, source=None):
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

    async def flaky(article_id, url, source=None):
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


# ---- Retry-After, Validatoren, feindliche Feeds -------------------------------------------

@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "infinity"])
def test_retry_after_rejects_non_finite(raw):
    assert fetcher_mod.parse_retry_after(raw) is None


async def test_429_with_nan_retry_after_counts_attempt(db, settings):
    aid = _article(db)
    f = make_fetcher(db, settings, lambda r: httpx.Response(429, headers={"retry-after": "nan"}))
    await f.extract_article(aid, f"{PUBLIC}/artikel")
    row = db.get_article(aid)
    assert row["text_status"] == "pending" and row["attempts"] == 1 and row["next_attempt_at"] > ago(seconds=0)


async def test_unexpected_error_in_bookkeeping_still_counts_attempt(db, settings, monkeypatch):
    aid = _article(db)
    f = make_fetcher(db, settings, lambda r: httpx.Response(404))

    def broken(*a, **kw):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(db, "store_extraction", broken)
    await f.extract_article(aid, f"{PUBLIC}/artikel")
    row = db.get_article(aid)
    assert row["attempts"] == 1 and row["next_attempt_at"] > ago(seconds=0)
    assert row["text_error"].startswith("Interner Fehler: RuntimeError")
    assert not db.is_due_pending(aid)   # nicht in jedem Lauf erneut


def test_http_token():
    assert fetcher_mod.http_token('W/"abc-123"') == 'W/"abc-123"'
    for bad in ('"häßlich"', "a\nb", "x" * 501, "", None, "’tag’"):
        assert fetcher_mod.http_token(bad) is None


async def test_non_ascii_validators_are_not_stored_or_sent(db, settings):
    seen = []

    def handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, headers=[(b"etag", '"ä-1"'.encode()), (b"last-modified", "Mon, 01 Sep 2026 10:00:00 GMT".encode())],
                              content=rss(item(1, date=None)))

    f = make_fetcher(db, settings, handler)
    try:
        await f._fetch_feed(SRC)
        st = db.source_states()["alpha"]
        assert st["etag"] is None and st["last_modified"] == "Mon, 01 Sep 2026 10:00:00 GMT"
        # Ein älterer, schon gespeicherter kaputter Wert wird nicht gesendet (httpx würde scheitern).
        db.q("UPDATE source_state SET etag=?, last_modified=? WHERE id='alpha'", ('"ö"', "Mo, 1 Sep 2026 ä"))
        assert await f._fetch_feed(SRC) == 0
        assert "if-none-match" not in seen[-1] and "if-modified-since" not in seen[-1]
        assert db.source_states()["alpha"]["last_error"] is None
    finally:
        await f.close()


async def test_failed_feed_fetch_clears_validators(db, settings):
    db.record_fetch("alpha", ok=True, validators=('"v1"', "Mon, 01 Sep 2026 10:00:00 GMT"))
    f = make_fetcher(db, settings, lambda r: httpx.Response(500))
    await f._fetch_all_feeds()
    st = db.source_states()["alpha"]
    assert st["last_error"] == "HTTP 500" and st["etag"] is None and st["last_modified"] is None


def test_parse_feed_caps_entries(monkeypatch):
    monkeypatch.setattr(fetcher_mod, "FEED_MAX_ENTRIES", 5)
    rows = parse_feed(SRC, rss(*(item(i) for i in range(40))))
    assert [r["guid"] for r in rows] == [f"g{i}" for i in range(5)]


async def test_fetch_feed_caps_new_articles_per_run(db, settings, monkeypatch):
    monkeypatch.setattr(fetcher_mod, "FEED_MAX_NEW", 3)
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    f = make_fetcher(db, settings, lambda r: httpx.Response(200, content=rss(*(item(i, date=recent) for i in range(8)))))
    try:
        assert await f._fetch_feed(SRC) == 3
        assert await f._fetch_feed(SRC) == 3
        assert await f._fetch_feed(SRC) == 2
    finally:
        await f.close()


def test_feed_limits():
    assert fetcher_mod.FEED_MAX_BYTES <= 5 * 1024 * 1024 and fetcher_mod.ARTICLE_MAX_BYTES <= 5 * 1024 * 1024
    assert fetcher_mod.FEED_MAX_ENTRIES <= 300


def _hanging_parse(source, raw):
    """Läuft im Worker-Prozess: feedparser, der nicht fertig wird."""
    time.sleep(60)


async def test_feed_parsing_runs_in_worker_with_timeout(db, settings, monkeypatch):
    monkeypatch.setattr(fetcher_mod, "parse_feed", _hanging_parse)
    monkeypatch.setattr(fetcher_mod, "FEED_PARSE_TIMEOUT", 1)
    f = make_fetcher(db, settings, lambda r: httpx.Response(200, content=rss(item(1))))
    try:
        assert await asyncio.wait_for(f._fetch_all_feeds(), timeout=20) == 0
        assert db.source_states()["alpha"]["last_error"] == "Feed-Parsing: Zeitüberschreitung"
        assert not f._pids   # hängender Worker-Prozess beendet
    finally:
        await f.close()


# ---- Abruf auf Anforderung: gedeckelt ------------------------------------------------------

async def test_on_demand_extractions_are_bounded(db, settings, monkeypatch):
    monkeypatch.setattr(fetcher_mod, "PRIORITY_QUEUE_MAX", 3)
    db.upsert_articles([make_row("alpha", i, ago(hours=i + 1), url=f"{PUBLIC}/a/{i}") for i in range(5)])
    ids = [r["id"] for r in db.q("SELECT id FROM articles ORDER BY id")]
    gate = asyncio.Event()

    async def handler(request):
        await gate.wait()
        return httpx.Response(503)

    f = make_fetcher(db, settings, handler)
    assert [f.start_extraction(aid, f"{PUBLIC}/a/{i}") for i, aid in enumerate(ids)] == [True] * 3 + [False] * 2
    assert f.start_extraction(ids[0], f"{PUBLIC}/a/0") is True   # läuft schon: kein zweiter Start
    gate.set()
    await asyncio.gather(*list(f._inflight.values()))
    assert f._ondemand_count == 0 and f._inflight == {}
    assert f.start_extraction(ids[3], f"{PUBLIC}/a/3") is True
    await asyncio.gather(*list(f._inflight.values()))


async def test_retry_waits_after_recent_on_demand_attempt(db, settings, monkeypatch):
    aid = _article(db)
    calls = []
    f = make_fetcher(db, settings, lambda r: calls.append(1) or httpx.Response(404))
    await on_demand(f, aid, f"{PUBLIC}/artikel")
    assert db.get_article(aid)["text_status"] == "failed"
    assert f.start_extraction(aid, f"{PUBLIC}/artikel", retry=True) is False
    assert calls == [1]
    monkeypatch.setattr(fetcher_mod, "RETRY_MIN_INTERVAL", 0)
    assert f.start_extraction(aid, f"{PUBLIC}/artikel", retry=True) is True
    await asyncio.gather(*list(f._inflight.values()))
    assert calls == [1, 1]


async def test_title_echo_is_removed_off_the_event_loop(db, settings, monkeypatch):
    aid = _article(db)
    threads = []
    real = fetcher_mod.drop_title_echo

    def spy(result, title):
        threads.append(threading.current_thread() is threading.main_thread())
        return real(result, title)

    async def fake_extract(page, url, skip=None):
        return extract_mod.Extraction("full", "<p>Titel 1</p><p>Text</p>", "Titel 1 Text", 12, False, None, None)

    monkeypatch.setattr(fetcher_mod, "drop_title_echo", spy)
    f = make_fetcher(db, settings, lambda r: httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>x</p>"))
    monkeypatch.setattr(f, "_run_extract", fake_extract)
    await f._extract(aid, f"{PUBLIC}/artikel")
    assert threads == [False]
    assert db.get_article(aid)["body_html"] == "<p>Text</p>"


def test_parse_feed_keeps_newest_entries_of_oldest_first_feed(monkeypatch):
    monkeypatch.setattr(fetcher_mod, "FEED_MAX_ENTRIES", 3)
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    dated = [item(i, date=(base + timedelta(hours=i)).strftime("%a, %d %b %Y %H:%M:%S GMT")) for i in range(6)]
    rows = parse_feed(SRC, rss(item(90, date=None), *dated))      # älteste zuerst, einer ohne Datum
    assert [r["guid"] for r in rows] == ["g5", "g4", "g3"]
    monkeypatch.setattr(fetcher_mod, "FEED_MAX_ENTRIES", 10)
    rows = parse_feed(SRC, rss(item(90, date=None), item(91, date=None), *dated))
    assert [r["guid"] for r in rows][-2:] == ["g90", "g91"]         # ohne Datum: hinten, Reihenfolge bleibt


async def test_capped_fetch_does_not_store_validators(db, settings, monkeypatch):
    monkeypatch.setattr(fetcher_mod, "FEED_MAX_NEW", 3)
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    seen = []

    def handler(request):
        seen.append(request.headers.get("if-none-match"))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, headers={"etag": '"v1"'}, content=rss(*(item(i, date=recent) for i in range(5))))

    f = make_fetcher(db, settings, handler)
    try:
        assert await f._fetch_feed(SRC) == 3
        assert db.source_states()["alpha"]["etag"] is None       # Grenze erreicht: ohne Bedingung weiter
        assert await f._fetch_feed(SRC) == 2
        assert db.source_states()["alpha"]["etag"] == '"v1"'
        assert await f._fetch_feed(SRC) == 0
        assert seen == [None, None, '"v1"']
    finally:
        await f.close()


async def test_background_reads_have_no_time_limit_and_timeouts_do_not_count(db, settings, monkeypatch):
    from app.db import QueryTimeout, request_time_limit
    aid = _article(db)
    f = make_fetcher(db, settings, lambda r: httpx.Response(404))
    limits = []
    real_store = db.store_extraction

    def spy(*a, **kw):
        limits.append(request_time_limit.get())
        return real_store(*a, **kw)

    monkeypatch.setattr(db, "store_extraction", spy)
    token = request_time_limit.set(2.0)   # wie in einer API-Anfrage
    try:
        assert f.start_extraction(aid, f"{PUBLIC}/artikel")
        task = f._inflight[aid]
    finally:
        request_time_limit.reset(token)
    await task
    # Die aus der Anfrage gestartete Extraktion liest ohne Frist.
    assert limits == [None] and db.get_article(aid)["text_status"] == "failed"

    # Käme doch ein QueryTimeout, zählt er nicht als Versuch.
    db.upsert_articles([make_row("alpha", 2, ago(hours=1), url=f"{PUBLIC}/b")])
    aid2 = db.one("SELECT id FROM articles WHERE guid='alpha-2'")["id"]

    async def timeout(*a, **kw):
        raise QueryTimeout("x")

    monkeypatch.setattr(f, "_extract_once", timeout)
    await f._extract(aid2, f"{PUBLIC}/b")
    row = db.get_article(aid2)
    assert row["attempts"] == 0 and row["text_status"] == "pending" and row["text_error"] is None


async def test_purge_runs_even_if_a_phase_fails(db, settings, monkeypatch):
    db.upsert_articles([make_row("alpha", 99, ago(days=200))])
    f = make_fetcher(db, settings, lambda r: httpx.Response(503))

    async def broken():
        raise RuntimeError("kaputt")

    monkeypatch.setattr(f, "_fetch_all_feeds", broken)
    with pytest.raises(RuntimeError):
        await f.run_once()
    assert db.one("SELECT COUNT(*) AS n FROM articles")["n"] == 0


def test_forkserver_preloads_job_modules(db, settings, monkeypatch):
    import multiprocessing
    calls = []
    ctx = multiprocessing.get_context("forkserver")
    monkeypatch.setattr(type(ctx), "set_forkserver_preload", lambda self, mods: calls.append(mods))
    f = make_fetcher(db, settings, lambda r: httpx.Response(200))
    f._mp_context()
    assert calls == [["app.fetcher"]]
    assert fetcher_mod.parse_feed.__module__ == "app.fetcher" and fetcher_mod.extract.__module__ == "app.extract"


async def test_rebinding_is_stopped_before_the_request_is_sent(db, settings, monkeypatch):
    """DNS-Rebinding: check_url sah eine öffentliche Adresse, beim Verbindungsaufbau liefert DNS eine
    lokale. GuardedBackend löst selbst auf, prüft und verbindet nur mit geprüften Adressen: Beim
    lokalen Dienst kommt keine Anfrage an."""
    import socket
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(200)
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    async def ok(url):
        return None

    monkeypatch.setattr(fetcher_mod, "check_url", ok)
    f = Fetcher(db, Catalog([]), settings)
    await f.start()
    loop = asyncio.get_running_loop()
    real = loop.getaddrinfo

    async def rebinding(host, *args, **kw):
        if host == "rebind.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        return await real(host, *args, **kw)

    monkeypatch.setattr(loop, "getaddrinfo", rebinding)
    try:
        with pytest.raises(BlockedURL, match="nicht erlaubter Adresse"):
            await f._fetch_once(f"http://rebind.example:{port}/geheim", headers=None, max_bytes=1000)
        assert hits == []
    finally:
        await f.close()
        server.shutdown()
        server.server_close()


async def test_guarded_backend_connects_to_checked_public_address(monkeypatch):
    """Erlaubte Namen: Verbindung zur geprüften Adresse, Hostname bleibt für SNI/Host unverändert
    (das Backend bekommt nur host/port; start_tls erhält den Namen von httpcore getrennt)."""
    import socket
    from app.netguard import GuardedBackend
    calls = []

    class Inner:
        async def connect_tcp(self, host, port, **kw):
            calls.append((host, port))
            return "stream"

        async def sleep(self, s):
            pass

    loop = asyncio.get_running_loop()

    async def resolve(host, port, **kw):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1", port, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(loop, "getaddrinfo", resolve)
    backend = GuardedBackend(Inner())
    assert await backend.connect_tcp("news.example", 443) == "stream"
    assert calls == [("2606:4700::1", 443)]
    with pytest.raises(BlockedURL):
        await backend.connect_tcp("localhost", 80)
    with pytest.raises(BlockedURL):
        await backend.connect_unix_socket("/var/run/docker.sock")


def _page_problem(raw: bytes):
    return fetcher_mod.markup_problem(raw, max_tags=fetcher_mod.MAX_PAGE_TAGS, max_attrs=fetcher_mod.MAX_TAG_ATTRS,
                                      max_lt=fetcher_mod.MAX_PAGE_LT)


def _feed_problem(raw: bytes):
    return fetcher_mod.markup_problem(raw, max_tags=fetcher_mod.MAX_FEED_TAGS, max_attrs=fetcher_mod.MAX_TAG_ATTRS,
                                      max_lt=fetcher_mod.MAX_FEED_LT, max_prefixes=fetcher_mod.MAX_FEED_PREFIXES)


ATTR_BOMB = b"<div " + b" ".join(b'a%d="1"' % i for i in range(100_000)) + b"><p>x</p></div>"


@pytest.mark.parametrize("prefix", [
    b'<div title="a><!--">',                 # ">" und "<!--" in einem Attributwert beenden nichts
    b"<!-- nie beendet ",                    # unbeendeter Kommentar: der Rest zählt weiter
    b"<script>var a = 1;",                   # unbeendetes Skript
    b"<style>p{}",                           # unbeendeter Style
    b"<!-- x --!>",                          # Kommentar-Ende, das auch HTML-Parser kennen
    b"<div data-x='unbeendet",               # unbeendeter Wert: danach zählt alles mit
], ids=["gt-in-wert", "kommentar", "skript", "style", "kommentar-bang", "wert"])
def test_markup_guard_cannot_be_blinded(prefix):
    raw = b"<html><body>" + prefix + ATTR_BOMB + b"</body></html>"
    assert "Attribute" in (_page_problem(raw) or "")


def test_markup_guard_counts_cdata_content_in_feeds():
    raw = (b'<rss version="2.0"><channel><item><title>x</title><description><![CDATA[<!--]]></description>'
           + b"<x/>" * 300_000 + b"</item></channel></rss>")
    assert _feed_problem(raw)
    # Auch knapp unter der Gesamtgrenze: Tags in CDATA zählen als Tags.
    raw = b"<rss><channel><item><description><![CDATA[" + b"<p>" * 110_000 + b"]]></description></item></channel></rss>"
    assert "Tags" in (_feed_problem(raw) or "")


def test_markup_guard_accepts_single_quoted_json_attributes():
    obj = "{" + ",".join(f'"k{i}":"v {i}"' for i in range(140)) + "}"
    ids = "[" + ",".join(f'"a{i}"' for i in range(300)) + "]"
    page = (f"<html><body><div data-x='{obj}' data-ids='{ids}' class=\"a b\">Text</div>"
            f"<p title='a \"b\" c' data-y = 'x y z'>ok</p></body></html>").encode()
    assert _page_problem(page) is None


@pytest.mark.parametrize("raw", [
    b"<!--" * 90_000,                                    # lauter unbeendete Kommentare
    b'<a b="' * 90_000,                                   # lauter unbeendete Werte
    b"<div " + b'a="' * 1_500_000,                        # ein Tag voller unbeendeter Werte
    b"<script>" * 90_000,                                 # lauter unbeendete Skripte
    b"<p " + b"x" * 5_000_000,                            # ein Tag ohne Ende
], ids=["kommentare", "werte", "tag-voller-werte", "skripte", "tag-ohne-ende"])
def test_markup_guard_stays_linear(raw):
    start = time.perf_counter()
    _page_problem(raw)
    _feed_problem(raw)
    assert time.perf_counter() - start < 5


async def test_breaker_is_checked_again_with_the_job_slot(db, settings, monkeypatch):
    """Auch mit 6 gleichzeitigen Extraktionen (KIOSK_EXTRACT_CONCURRENCY) laufen nach dem Auslösen
    keine weiteren Aufträge der Quelle: Wer schon auf einen Worker-Platz wartet, fragt mit dem Platz
    noch einmal nach. Höchstens LIMIT + (Worker-Plätze - 1) Zeitüberschreitungen."""
    from dataclasses import replace
    monkeypatch.setattr(fetcher_mod, "EXTRACT_TIMEOUT", 1)
    monkeypatch.setattr(fetcher_mod, "extract", _timed_extract)
    db.upsert_articles([make_row("alpha", i, ago(minutes=i + 1), url=f"{PUBLIC}/alpha{i}/60") for i in range(8)])
    f = make_fetcher(db, replace(settings, extract_concurrency=6), lambda r: httpx.Response(
        200, headers={"content-type": "text/html"}, content=ARTICLE_HTML.encode()))
    try:
        await asyncio.wait_for(f._extract_pending(), timeout=30)
    finally:
        await f.close()
    tried = db.one("SELECT SUM(attempts) AS n FROM articles")["n"]
    assert 2 <= tried <= fetcher_mod.SOURCE_TROUBLE_LIMIT + fetcher_mod.EXTRACT_WORKERS - 1


async def test_on_demand_extraction_ignores_the_breaker(db, settings):
    """Öffnet jemand einen Artikel einer ausgelösten Quelle, gibt es trotzdem einen echten Versuch
    (sonst wartete die Oberfläche 30 s auf nichts)."""
    aid = _article(db)
    f = make_fetcher(db, settings, lambda r: httpx.Response(
        200, headers={"content-type": "text/html"}, content=ARTICLE_HTML.encode()))
    try:
        f._cycle_started = time.monotonic() - 1
        for _ in range(fetcher_mod.SOURCE_TROUBLE_LIMIT):
            f._note_trouble("alpha")
        assert f._tripped("alpha")
        await f.extract_article(aid, f"{PUBLIC}/artikel", "alpha")      # Hintergrund: übersprungen
        assert db.get_article(aid)["text_status"] == "pending"
        assert f.start_extraction(aid, f"{PUBLIC}/artikel", source="alpha")
        await asyncio.wait_for(asyncio.shield(f._inflight[aid]), timeout=30)
        assert db.get_article(aid)["text_status"] in ("full", "partial")
    finally:
        await f.close()


def _exiting_extract(page, url):
    """Läuft im Worker-Prozess: beendet sich per SystemExit (keine Exception-Unterklasse)."""
    raise SystemExit(3)


async def test_worker_base_exception_is_not_reraised_in_the_loop(db, settings, monkeypatch):
    aid = _article(db)
    f = make_fetcher(db, settings, lambda r: httpx.Response(
        200, headers={"content-type": "text/html"}, content=ARTICLE_HTML.encode()))
    monkeypatch.setattr(fetcher_mod, "extract", _exiting_extract)
    try:
        with pytest.raises(fetcher_mod.JobInfraError, match="SystemExit"):
            await f._run_extract(ARTICLE_HTML.encode(), f"{PUBLIC}/x")
        await asyncio.wait_for(f.extract_article(aid, f"{PUBLIC}/artikel"), timeout=30)
        row = db.get_article(aid)
        # Fehler der Worker-Mechanik: vorübergehend (Versuch zählt), nicht endgültig "failed".
        assert "SystemExit" in row["text_error"] and row["text_status"] == "pending" and row["attempts"] == 1
    finally:
        await f.close()


async def test_jobs_use_their_own_threads_not_the_default_executor(db, settings, monkeypatch):
    """Start und Einsammeln der Worker-Prozesse blockieren nicht den Standard-Executor (den brauchen
    getaddrinfo, scrypt und die Gruppierung)."""
    used = []
    loop = asyncio.get_running_loop()
    real = loop.run_in_executor

    def spy(executor, fn, *args):
        used.append(executor)
        return real(executor, fn, *args)

    monkeypatch.setattr(loop, "run_in_executor", spy)
    f = make_fetcher(db, settings, lambda r: httpx.Response(200))
    try:
        result = await f._run_extract(ARTICLE_HTML.encode(), f"{PUBLIC}/x")
        assert result.status in ("full", "partial")
        assert used and all(e is f._threads for e in used)
    finally:
        await f.close()
    assert f._threads is None


BOMB_20K = b"<div " + b" ".join(b'a%d="1"' % i for i in range(20_000)) + b"><p>x</p></div>"


@pytest.mark.parametrize("before, after", [
    (b"<!-->", b"-->"),                                   # leerer Kommentar ist sofort zu Ende
    (b"<!--->", b"-->"),
    (b"<title><script></title>", b"</script>"),          # Textelemente: "<script" darin beginnt nichts
    (b"<textarea><!--</textarea>", b"-->"),
    (b"<xmp><style></xmp>", b"</style>"),
    (b"<iframe><script></iframe>", b"</script>"),
    (b"<noembed><script></noembed>", b"</script>"),
    (b"<noframes><script></noframes>", b"</script>"),
    (b'<p a=b="> ', b' ">'),                             # ungequoteter Wert b=" öffnet nichts
    (b'<p ="> ', b' ">'),                                # Attributname "=\"" statt Wert
    (b"<p a= b'> ", b" '>"),
], ids=["leer", "leer-strich", "title", "textarea", "xmp", "iframe", "noembed", "noframes",
        "phantom-wert", "name-mit-gleich", "phantom-einfach"])
def test_markup_guard_follows_the_html5_tokenizer(before, after):
    raw = b"<html><body>" + before + BOMB_20K + after + b"</body></html>"
    assert "Attribute" in (_page_problem(raw) or ""), before


def test_markup_guard_counts_attributes_on_end_tags():
    raw = b"<html><body><p>x</p " + b" ".join(b"a%d" % i for i in range(20_000)) + b"></body></html>"
    assert "Attribute" in (_page_problem(raw) or "")


def test_markup_guard_real_text_elements_and_values_pass():
    page = (b"<html><head><title>A <b> B</title><script>if (a<b) x('</p>');</script></head><body>"
            b"<textarea>x < y</textarea><p a=b c='d e' f = \"g h\" i=j=k>ok</p>"
            + b"<div class=x data-a='1'>t</div>" * 2000 + b"</body></html>")
    assert _page_problem(page) is None


def test_markup_guard_many_small_attributes_stays_fast():
    tag = b"<p " + b"a " * 250 + b">"
    raw = tag * (5 * 1024 * 1024 // len(tag))
    start = time.perf_counter()
    _page_problem(raw)
    assert time.perf_counter() - start < 10


async def test_worker_start_failure_is_requeued_without_attempt(db, settings, monkeypatch):
    """fork scheitert (z. B. EAGAIN an pids_limit): nicht die Schuld des Artikels."""
    import multiprocessing.process
    aid = _article(db)
    f = make_fetcher(db, settings, lambda r: httpx.Response(
        200, headers={"content-type": "text/html"}, content=ARTICLE_HTML.encode()))

    def no_fork(self):
        raise BlockingIOError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(multiprocessing.process.BaseProcess, "start", no_fork)
    try:
        assert await f.extract_article(aid, f"{PUBLIC}/artikel") is True
    finally:
        await f.close()
    row = db.get_article(aid)
    assert row["text_status"] == "pending" and row["attempts"] == 0 and row["next_attempt_at"] is None
    assert "ließ sich nicht starten" in row["text_error"]


async def test_skipped_articles_do_not_count_as_extracted(db, settings, monkeypatch):
    """Vom Leistungsschalter übersprungene Artikel zählen nicht fürs Budget je Lauf und nicht als
    extrahiert (last_extracted)."""
    db.upsert_articles([make_row("alpha", i, ago(minutes=i + 1), url=f"{PUBLIC}/alpha{i}") for i in range(5)])
    f = make_fetcher(db, settings, lambda r: httpx.Response(
        200, headers={"content-type": "text/html"}, content=ARTICLE_HTML.encode()))
    class Sticky(dict):
        def clear(self):          # _extract_pending setzt den Schalter zurück; hier bleibt er ausgelöst
            pass

    later = time.monotonic() + 3600
    f._trouble = Sticky(alpha=[later] * fetcher_mod.SOURCE_TROUBLE_LIMIT)
    try:
        assert await f._extract_pending() == 0
        assert f._extracted_ids == []
        assert all(r["attempts"] == 0 and r["text_status"] == "pending" for r in db.q("SELECT * FROM articles"))
    finally:
        await f.close()
