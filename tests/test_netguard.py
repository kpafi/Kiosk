from __future__ import annotations

import socket

import httpx
import pytest

import app.fetcher as fetcher_mod
from app.fetcher import FetchError, Fetcher
from app.netguard import BlockedURL, ResolveError, check_url, ip_is_blocked, server_addr_blocked
from app.sources import Catalog
from conftest import ago, make_row


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "127.8.9.1", "10.0.0.1", "172.16.5.4", "192.168.1.1", "169.254.169.254", "169.254.1.1",
    "0.0.0.0", "100.64.0.1", "224.0.0.1", "240.0.0.1", "255.255.255.255", "198.18.0.1",
    "::1", "::", "fe80::1", "fc00::1", "fd12:3456::1", "ff02::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
    "64:ff9b::7f00:1",
])
def test_blocked_ips(ip):
    assert ip_is_blocked(ip)


@pytest.mark.parametrize("ip", ["93.184.216.34", "1.1.1.1", "8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public_ips(ip):
    assert not ip_is_blocked(ip)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/", "gopher://x", "http://localhost/", "http://LOCALHOST./",
    "http://foo.localhost/", "http://127.0.0.1:8100/healthz", "http://[::1]/", "http://169.254.169.254/latest",
    "http:///nohost", "http://10.1.2.3/",
])
async def test_check_url_rejects(url):
    with pytest.raises(BlockedURL):
        await check_url(url)


def _fake_getaddrinfo(mapping):
    async def fake(self, host, port, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(socket.EAI_NONAME, "unbekannt")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in mapping[host]]
    return fake


async def test_check_url_resolves_hostnames(monkeypatch):
    import asyncio
    loop_cls = type(asyncio.get_running_loop())
    monkeypatch.setattr(loop_cls, "getaddrinfo", _fake_getaddrinfo({
        "good.example": ["93.184.216.34"],
        "mixed.example": ["93.184.216.34", "10.0.0.5"],
        "rebind.example": ["::ffff:192.168.0.1"],
    }))
    await check_url("https://good.example/x")
    for url in ("https://mixed.example/", "http://rebind.example/"):
        with pytest.raises(BlockedURL):
            await check_url(url)
    with pytest.raises(ResolveError):  # DNS-Fehler ist vorübergehend, keine Sperre
        await check_url("http://unknown.example/")


# ---- Fetcher mit MockTransport -----------------------------------------------

PUBLIC = "http://93.184.216.34"


def make_fetcher(db, settings, handler) -> Fetcher:
    f = Fetcher(db, Catalog([]), settings)
    f._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    return f


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(fetcher_mod, "PER_DOMAIN_DELAY", 0)


async def test_redirect_to_private_address_is_blocked(db, settings):
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/weiter"})
        if request.url.path == "/weiter":
            return httpx.Response(301, headers={"location": "http://127.0.0.1:8100/api/status"})
        return httpx.Response(200, text="geheim")

    f = make_fetcher(db, settings, handler)
    with pytest.raises(BlockedURL):
        await f._fetch_once(f"{PUBLIC}/start", headers=None, max_bytes=1000)
    assert requested == [f"{PUBLIC}/start", f"{PUBLIC}/weiter"]  # 127.0.0.1 nie angefragt


async def test_redirect_limit(db, settings):
    def handler(request):
        n = int(request.url.path.strip("/") or 0)
        return httpx.Response(302, headers={"location": f"/{n + 1}"})

    f = make_fetcher(db, settings, handler)
    with pytest.raises(FetchError, match="Umleitungen"):
        await f._fetch_once(f"{PUBLIC}/0", headers=None, max_bytes=1000)
    ok = make_fetcher(db, settings, lambda r: httpx.Response(302, headers={"location": "/ziel"})
                      if r.url.path != "/ziel" else httpx.Response(200, text="da"))
    r = await ok._fetch_once(f"{PUBLIC}/start", headers=None, max_bytes=1000)
    assert r.content == b"da" and r.url.endswith("/ziel")


async def test_size_limit_by_content_length_and_stream(db, settings):
    f = make_fetcher(db, settings, lambda r: httpx.Response(200, headers={"content-length": "5000"}, content=b"x" * 5000))
    with pytest.raises(FetchError, match="zu groß"):
        await f._fetch_once(f"{PUBLIC}/", headers=None, max_bytes=1000)

    class Chunks(httpx.AsyncByteStream):  # ohne Content-Length
        async def __aiter__(self):
            yield b"x" * 600
            yield b"x" * 600

    def chunked(request):
        return httpx.Response(200, stream=Chunks())

    f = make_fetcher(db, settings, chunked)
    with pytest.raises(FetchError, match="zu groß"):
        await f._fetch_once(f"{PUBLIC}/", headers=None, max_bytes=1000)


async def _article(db, url=f"{PUBLIC}/artikel"):
    db.upsert_articles([make_row("alpha", 1, ago(hours=1), url=url)])
    return db.one("SELECT id FROM articles")["id"]


@pytest.mark.parametrize("status,expected_status,expected_attempts", [
    (404, "failed", 0), (410, "failed", 0), (403, "failed", 0),
    (503, "pending", 1), (500, "pending", 1), (408, "pending", 1), (429, "pending", 1),
])
async def test_extraction_failure_classification(db, settings, status, expected_status, expected_attempts):
    aid = await _article(db)
    f = make_fetcher(db, settings, lambda r: httpx.Response(status))
    await f.extract_article(aid, f"{PUBLIC}/artikel")
    row = db.get_article(aid)
    assert row["text_status"] == expected_status
    assert row["attempts"] == expected_attempts
    assert row["text_error"] == f"HTTP {status}"


async def test_429_retry_after_respected_without_second_request(db, settings):
    aid = await _article(db)
    calls = []

    def handler(request):
        calls.append(request.headers["user-agent"])
        return httpx.Response(429, headers={"retry-after": "7200"})

    f = make_fetcher(db, settings, handler)
    await f.extract_article(aid, f"{PUBLIC}/artikel")
    assert len(calls) == 1
    row = db.get_article(aid)
    assert row["text_status"] == "pending" and row["attempts"] == 1
    assert row["next_attempt_at"] > ago(seconds=-7000)


async def test_network_error_is_transient_size_limit_permanent(db, settings, monkeypatch):
    aid = await _article(db)

    def boom(request):
        raise httpx.ConnectError("kaputt", request=request)

    await make_fetcher(db, settings, boom).extract_article(aid, f"{PUBLIC}/artikel")
    assert db.get_article(aid)["text_status"] == "pending"

    monkeypatch.setattr(fetcher_mod, "ARTICLE_MAX_BYTES", 10)
    db.q("UPDATE articles SET next_attempt_at=NULL")
    big = lambda r: httpx.Response(200, headers={"content-type": "text/html"}, content=b"x" * 100)  # noqa: E731
    await make_fetcher(db, settings, big).extract_article(aid, f"{PUBLIC}/artikel")
    row = db.get_article(aid)
    # Zu große Seite: endgültig, keine drei Wiederholungen (die Seite wird nicht kleiner).
    assert row["text_status"] == "failed" and row["next_attempt_at"] is None and "zu groß" in row["text_error"]


async def test_blocked_article_url_fails_permanently(db, settings):
    aid = await _article(db, url="http://127.0.0.1/x")
    await make_fetcher(db, settings, lambda r: httpx.Response(200)).extract_article(aid, "http://127.0.0.1/x")
    row = db.get_article(aid)
    assert row["text_status"] == "failed" and "blockiert" in row["text_error"]


async def test_non_html_fails(db, settings):
    aid = await _article(db)
    f = make_fetcher(db, settings, lambda r: httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF"))
    await f.extract_article(aid, f"{PUBLIC}/artikel")
    assert db.get_article(aid)["text_status"] == "failed"


async def test_total_timeout(db, settings, monkeypatch):
    import asyncio
    monkeypatch.setattr(fetcher_mod, "TOTAL_TIMEOUT", 0.2)

    async def slow_stream():
        yield b"<html>"
        await asyncio.sleep(5)
        yield b"</html>"

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            async for c in slow_stream():
                yield c

    f = make_fetcher(db, settings, lambda r: httpx.Response(200, stream=Slow()))
    with pytest.raises(TimeoutError):
        await f._fetch_once(f"{PUBLIC}/", headers=None, max_bytes=1000)


# ---- Nur Standard-Ports, zusätzliche Sperrliste --------------------------------------

@pytest.mark.parametrize("url", [
    "https://93.184.216.34:8443/", "http://93.184.216.34:8080/feed", "http://93.184.216.34:443/",
    "https://93.184.216.34:80/", "http://93.184.216.34:22/",
])
async def test_check_url_rejects_non_default_ports(url):
    with pytest.raises(BlockedURL, match="Port nicht erlaubt"):
        await check_url(url)


@pytest.mark.parametrize("url", ["https://93.184.216.34:443/x", "http://93.184.216.34:80/x", "https://93.184.216.34/x"])
async def test_check_url_allows_default_ports(url):
    await check_url(url)


async def test_non_default_port_fails_article_permanently(db, settings):
    db.upsert_articles([make_row("alpha", 1, ago(hours=1), url="https://93.184.216.34:8443/a")])
    aid = db.one("SELECT id FROM articles")["id"]
    f = Fetcher(db, Catalog([]), settings)
    f._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    await f.extract_article(aid, "https://93.184.216.34:8443/a")
    row = db.get_article(aid)
    assert row["text_status"] == "failed" and "Port nicht erlaubt" in row["text_error"]


def test_catalog_uses_default_ports_only():
    from pathlib import Path
    from urllib.parse import urlparse
    catalog = Catalog.load(Path(__file__).resolve().parent.parent / "sources.yaml")
    for src in catalog.all():
        parsed = urlparse(src.feed)
        assert parsed.scheme in ("http", "https") and parsed.port in (None, {"http": 80, "https": 443}[parsed.scheme]), src.id


@pytest.fixture
def deny(monkeypatch):
    import app.netguard as netguard_mod
    monkeypatch.setattr(netguard_mod, "_deny_networks", ())
    return netguard_mod.set_deny_networks


async def test_deny_list_blocks_public_addresses(deny, monkeypatch):
    import ipaddress
    deny([ipaddress.ip_network("93.184.216.0/24"), ipaddress.ip_network("2606:4700::/32")])
    assert ip_is_blocked("93.184.216.34") and ip_is_blocked("::ffff:93.184.216.34")
    assert ip_is_blocked("2606:4700:4700::1111") and not ip_is_blocked("1.1.1.1")
    assert server_addr_blocked(("93.184.216.34", 443))
    with pytest.raises(BlockedURL):
        await check_url("https://93.184.216.34/")
    monkeypatch.setattr(type(__import__("asyncio").get_running_loop()), "getaddrinfo",
                        _fake_getaddrinfo({"news.example": ["93.184.216.99"]}))
    with pytest.raises(BlockedURL):
        await check_url("https://news.example/")


def test_fetcher_applies_deny_setting(deny, db, settings):
    import ipaddress
    from dataclasses import replace
    Fetcher(db, Catalog([]), replace(settings, fetch_deny_ips=(ipaddress.ip_network("8.8.8.0/24"),)))
    assert ip_is_blocked("8.8.8.8")
    Fetcher(db, Catalog([]), settings)
    assert not ip_is_blocked("8.8.8.8")


def test_deny_setting_parsing(monkeypatch):
    import ipaddress
    from app.config import fetch_deny_networks
    monkeypatch.setenv("KIOSK_FETCH_DENY_IPS", " 203.0.113.10 , 2a01:4f8::/32,,10.0.0.1/8 ")
    assert fetch_deny_networks() == (ipaddress.ip_network("203.0.113.10/32"), ipaddress.ip_network("2a01:4f8::/32"),
                                     ipaddress.ip_network("10.0.0.0/8"))
    monkeypatch.setenv("KIOSK_FETCH_DENY_IPS", "203.0.113.10,kein-netz")
    with pytest.raises(RuntimeError, match="KIOSK_FETCH_DENY_IPS.*kein-netz"):
        fetch_deny_networks()
    monkeypatch.delenv("KIOSK_FETCH_DENY_IPS")
    assert fetch_deny_networks() == ()
