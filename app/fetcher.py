"""Holt Feeds, legt neue Artikel an und extrahiert im Hintergrund den Volltext."""
from __future__ import annotations

import asyncio
import calendar
import concurrent.futures
import multiprocessing
import io
import logging
import time
import weakref
import zlib
from collections import defaultdict
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import feedparser
import httpx

from .config import Settings
from .db import Database, utcnow
from .extract import drop_title_echo, extract, is_placeholder, is_small_image_url, safe_http_url, strip_html
from .netguard import BlockedURL, ResolveError, check_url, server_addr_blocked
from .sources import Catalog, Source

log = logging.getLogger("kiosk.fetcher")

READER_UA = "Mozilla/5.0 (compatible; KioskReader/0.1; private RSS reader)"
FEED_CONCURRENCY = 8
EXTRACT_BATCH = 60
EXTRACT_MAX_PER_CYCLE = 900
PER_DOMAIN_DELAY = 0.4
IMAGE_TYPES = ("image/",)
FEED_MAX_BYTES = 10 * 1024 * 1024
ARTICLE_MAX_BYTES = 5 * 1024 * 1024
TOTAL_TIMEOUT = 45          # Sekunden pro Abruf inkl. Umleitungen, unabhängig von httpx-Timeouts
OVERALL_TIMEOUT = 60        # Sekunden für _get insgesamt, inkl. zweitem Versuch mit anderem User-Agent
PRIORITY_CONCURRENCY = 2    # gleichzeitige Extraktionen auf Anforderung (API)
DOMAIN_STATE_TTL = 3600     # Sekunden, nach denen ungenutzte Domain-Einträge verworfen werden
MAX_REDIRECTS = 5
REDIRECT_CODES = (301, 302, 303, 307, 308)
# 4xx-Antworten, die trotzdem vorübergehend sein können
TRANSIENT_4XX = (408, 425, 429)
MAX_RETRY_AFTER = 12 * 3600
EXTRACT_TIMEOUT = 90        # Sekunden für eine Textextraktion im Worker-Prozess
EXTRACT_WORKERS = 2         # Worker-Prozesse für die Extraktion (begrenzt auch CPU und Speicher)
# Nach so vielen Aufträgen wird ein Worker durch einen frischen Prozess ersetzt: lxml/trafilatura
# geben Speicher nicht zuverlässig an das System zurück (Fragmentierung, Caches).
EXTRACT_TASKS_PER_CHILD = 25
# Content-Encodings, die wir bewusst nicht selbst entpacken (endgültiger Fehler).
UNSUPPORTED_ENCODINGS = {"br", "zstd", "compress", "x-compress"}


class FetchError(Exception):
    """Abruf gescheitert (Größenlimit, zu viele Umleitungen ...). Gilt als vorübergehend."""


class UnsupportedEncoding(Exception):
    """Antwort mit einer Content-Encoding, die wir nicht selbst (begrenzt) entpacken. Endgültig."""


class _DeflateDecoder:
    """deflate laut RFC mit zlib-Kopf; manche Server schicken rohes deflate ohne Kopf. Scheitert
    der erste Block mit zlib-Kopf, wird auf rohes deflate umgeschaltet. Der erste Block umfasst
    mindestens 2 Bytes (der zlib-Kopf), damit die Entscheidung nicht an Stückgrenzen hängt."""

    def __init__(self) -> None:
        self._d: Any = None
        self._head = b""

    def decompress(self, data: bytes, max_length: int = 0) -> bytes:
        if self._d is None:
            self._head += data
            if len(self._head) < 2:
                return b""
            data, self._head = self._head, b""
            self._d = zlib.decompressobj()
            try:
                return self._d.decompress(data, max_length)
            except zlib.error:
                self._d = zlib.decompressobj(-zlib.MAX_WBITS)
        return self._d.decompress(data, max_length)

    def flush(self) -> bytes:
        return self._d.flush() if self._d is not None else b""

    @property
    def eof(self) -> bool:
        return self._d is not None and self._d.eof


def _decoder(header: str):
    """Entpacker für den Content-Encoding-Header; None für unkomprimiert.

    Nur gzip und deflate, weil wir die Ausgabe selbst begrenzen müssen (httpx würde vor jeder
    Größenprüfung vollständig entpacken). Der Header ist eine Liste: "identity" und unbekannte
    Werte (z. B. ein falsch gesetztes "UTF-8") gelten als unkomprimiert; br/zstd/compress und
    mehrfach gestapelte Kodierungen lehnen wir ab.
    """
    codings: list[str] = []
    for token in header.split(","):
        enc = token.strip().lower()
        if enc in UNSUPPORTED_ENCODINGS:
            raise UnsupportedEncoding(f"Nicht unterstützte Content-Encoding: {header[:40]}")
        if enc in ("gzip", "x-gzip", "deflate"):
            codings.append(enc)
    if len(codings) > 1:
        raise UnsupportedEncoding(f"Nicht unterstützte Content-Encoding (gestapelt): {header[:40]}")
    if not codings:
        return None
    if codings[0] == "deflate":
        return _DeflateDecoder()
    return zlib.decompressobj(16 + zlib.MAX_WBITS)


class ExtractTimeout(Exception):
    """Die Extraktion im Worker-Prozess hat EXTRACT_TIMEOUT überschritten."""


class PoolDiscarded(Exception):
    """Der Auftrag ist gescheitert, weil wir den Pool selbst verworfen haben (hängender Nachbar).
    Kein Fehler des Artikels: wird ohne Zählung eines Versuchs erneut eingeplant."""


def _discard_pool(pool: concurrent.futures.ProcessPoolExecutor) -> None:
    """Beendet einen Prozess-Pool sofort, auch mit hängendem Worker.

    shutdown(wait=False, cancel_futures=True) verwirft wartende Aufträge, bricht einen laufenden
    Worker aber nicht ab. Eine öffentliche API dafür gibt es erst ab Python 3.14 (kill_workers);
    wir nutzen die private Liste _processes (vor shutdown lesen, das setzt sie auf None) und
    schicken SIGKILL an die eigenen Kindprozesse (gleiche uid, braucht keine Capabilities).
    Noch laufende Aufträge des Pools enden dann mit BrokenProcessPool, noch nicht gestartete mit
    CancelledError (siehe Fetcher._run_extract).
    """
    procs = list((getattr(pool, "_processes", None) or {}).values())
    pool.shutdown(wait=False, cancel_futures=True)
    for proc in procs:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


@dataclass
class Fetched:
    status_code: int
    headers: httpx.Headers
    content: bytes
    url: str


def parse_retry_after(raw: str | None) -> float | None:
    """Retry-After als Sekunden (Zahl oder HTTP-Datum), begrenzt auf MAX_RETRY_AFTER."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)


def _entry_datetime(entry: Any) -> str:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc).replace(microsecond=0).isoformat()
            except (OverflowError, ValueError):
                continue
    return utcnow()


def _entry_image(entry: Any) -> str | None:
    for m in entry.get("media_content", []) or []:
        url = m.get("url")
        if url and (m.get("medium") == "image" or str(m.get("type", "")).startswith(IMAGE_TYPES) or not m.get("type")):
            return url
    for m in entry.get("media_thumbnail", []) or []:
        if m.get("url"):
            return m["url"]
    for e in entry.get("enclosures", []) or []:
        if str(e.get("type", "")).startswith(IMAGE_TYPES) and e.get("href"):
            return e["href"]
    for l in entry.get("links", []) or []:
        if l.get("rel") == "enclosure" and str(l.get("type", "")).startswith(IMAGE_TYPES) and l.get("href"):
            return l["href"]
    return None


def _entry_summary(entry: Any, title: str = "") -> str:
    """Teaser aus dem Feed; Platzhalter ("None", "null" ...) und reine Titel-Wiederholung gelten als leer."""
    content = entry.get("content") or []
    for raw in (entry.get("summary"), content[0].get("value") if content else None):
        text = strip_html(raw or "")
        if not is_placeholder(text, title):
            return text
    return ""


def _entry_author(entry: Any) -> str | None:
    author = strip_html(entry.get("author") or "", limit=200)
    return None if is_placeholder(author) else author


def parse_feed(source: Source, raw: bytes) -> list[dict[str, Any]]:
    # Als Datei-Objekt übergeben: Bei bytes/str versucht feedparser zuerst, den Inhalt als
    # Dateipfad oder URL zu öffnen (ein Body "/proc/self/environ" würde gelesen).
    parsed = feedparser.parse(io.BytesIO(raw))
    now = utcnow()
    rows: list[dict[str, Any]] = []
    # Einträge mit Datum in der Zukunft würden sonst dauerhaft oben kleben.
    for e in parsed.entries:
        link = (e.get("link") or "").strip()
        if not link.startswith(("http://", "https://")):
            continue
        title = strip_html(e.get("title") or "", limit=300) or "(ohne Titel)"
        rows.append({
            "source_id": source.id,
            "guid": (e.get("id") or link).strip()[:500],
            "url": link[:1000],
            "title": title,
            "summary": _entry_summary(e, title),
            "author": _entry_author(e),
            "image_url": safe_http_url(_entry_image(e)),
            "published_at": min(_entry_datetime(e), now),
            "fetched_at": now,
        })
    return rows


class Fetcher:
    def __init__(self, db: Database, catalog: Catalog, settings: Settings):
        self.db = db
        self.catalog = catalog
        self.settings = settings
        self.state: dict[str, Any] = {
            "running": False, "last_started": None, "last_finished": None,
            "last_new": 0, "last_extracted": 0, "last_purged": 0, "runs": 0,
        }
        self._run_lock = asyncio.Lock()
        self._domain_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._domain_last: dict[str, float] = {}
        # Getrennte Grenzen für Hintergrund-Extraktionen und Abrufe auf Anforderung (API),
        # damit ein voller Hintergrundlauf das Lesen nicht blockiert und umgekehrt.
        self._extract_sem = asyncio.Semaphore(settings.extract_concurrency)
        self._priority_sem = asyncio.Semaphore(PRIORITY_CONCURRENCY)
        # Artikel-ID -> laufende Extraktion; verhindert doppelte Abrufe und doppelt gezählte Versuche.
        self._inflight: dict[int, asyncio.Task] = {}
        self._client: httpx.AsyncClient | None = None
        self._wake = asyncio.Event()
        # Extraktion (trafilatura/lxml) in eigenen Prozessen: Ein Thread ließe sich bei einer
        # hängenden Seite nicht abbrechen. Wird bei Bedarf (neu) angelegt, siehe _extraction_pool.
        self._pool: concurrent.futures.ProcessPoolExecutor | None = None
        # Höchstens so viele Aufträge im Pool wie Worker: Das Zeitlimit misst so nur die reine
        # Laufzeit (nicht die Wartezeit in der Pool-Warteschlange), und ein verworfener Pool
        # trifft nur die gerade laufenden Aufträge.
        self._pool_sem = asyncio.Semaphore(EXTRACT_WORKERS)
        # Wegen Zeitüberschreitung von uns verworfene Pools; die dadurch abgebrochenen Aufträge
        # der Nachbarn zählen nicht als Versuch.
        self._discarded: weakref.WeakSet = weakref.WeakSet()

    # ---- lifecycle ---------------------------------------------------

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept-Language": "de,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                # Unkomprimiert anfordern, damit die Größenlimits genau greifen (siehe _fetch_once).
                "Accept-Encoding": "identity",
            },
            # Umleitungen folgen wir selbst, damit jedes Ziel die SSRF-Prüfung durchläuft.
            follow_redirects=False,
            # Keine Proxy-Variablen aus der Umgebung: Die SSRF-Prüfung und der Abgleich der
            # Gegenstelle setzen direkte Verbindungen voraus.
            trust_env=False,
            timeout=httpx.Timeout(25.0, connect=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self._extraction_pool()

    async def close(self) -> None:
        # Laufende Extraktionen (per shield vom Aufrufer entkoppelt) beim Herunterfahren abbrechen.
        pending = list(self._inflight.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if self._client:
            await self._client.aclose()
        if self._pool is not None:
            _discard_pool(self._pool)
            self._pool = None

    def _extraction_pool(self) -> concurrent.futures.ProcessPoolExecutor:
        """Der Prozess-Pool für die Extraktion; wird beim ersten Bedarf angelegt.

        Startmethode ist bewusst "forkserver" statt des Linux-Standards "fork": uvicorn hat zu
        diesem Zeitpunkt schon Threads (to_thread für feedparser, httpx), und fork() in einem
        Prozess mit Threads kann im Kind gehaltene Locks (Logging, malloc) einfrieren. Der
        Forkserver wird einmal sauber per exec gestartet, die Worker forken von ihm. Die Worker
        führen nur die Modulfunktion extract() mit bytes und str aus und fassen weder Datenbank
        noch Event-Loop an. Sie starten beim ersten Auftrag, laufen im selben Container
        (pids_limit, mem_limit) und brauchen /dev/shm für die Semaphoren von multiprocessing.
        max_tasks_per_child ersetzt jeden Worker nach EXTRACT_TASKS_PER_CHILD Aufträgen durch
        einen frischen Prozess (begrenzt schleichend wachsenden Speicher; mit forkserver erlaubt).
        """
        if self._pool is None:
            ctx = multiprocessing.get_context("forkserver")
            self._pool = concurrent.futures.ProcessPoolExecutor(
                max_workers=EXTRACT_WORKERS, mp_context=ctx, max_tasks_per_child=EXTRACT_TASKS_PER_CHILD)
        return self._pool

    def _replace_pool(self, pool: concurrent.futures.ProcessPoolExecutor) -> None:
        """Verwirft den Pool (hängender oder gestorbener Worker); der nächste Auftrag legt einen
        neuen an. Nur, wenn nicht schon ein anderer Aufruf ihn ersetzt hat."""
        if self._pool is pool:
            self._pool = None
            _discard_pool(pool)

    async def _run_extract(self, page: bytes, url: str):
        """Führt extract() im Prozess-Pool aus, höchstens EXTRACT_TIMEOUT Sekunden reine Laufzeit.

        Wartet zuerst auf einen freien Worker-Platz (_pool_sem), erst danach läuft das Zeitlimit.
        Überschreitet ein Auftrag das Limit, wird der Pool verworfen (ExtractTimeout); die übrigen
        laufenden Aufträge dieses Pools enden dann mit PoolDiscarded (zählt nicht als Versuch).
        """
        async with self._pool_sem:
            pool = self._extraction_pool()
            loop = asyncio.get_running_loop()
            # extract wird zur Laufzeit aus dem Modul gelesen (in Tests austauschbar); an den Worker
            # geht nur eine Referenz auf die Modulfunktion plus bytes und str (picklebar).
            try:
                # submit wirft BrokenProcessPool sofort, wenn ein Worker schon vorher gestorben ist.
                future = loop.run_in_executor(pool, extract, page, url)
                return await asyncio.wait_for(future, timeout=EXTRACT_TIMEOUT)
            except asyncio.TimeoutError:
                # Nur hier verwerfen wir einen gesunden Pool selbst; stirbt ein Worker von allein
                # (z. B. Speicher), wissen wir nicht, welcher Auftrag schuld war, und alle zählen.
                self._discarded.add(pool)
                self._replace_pool(pool)
                raise ExtractTimeout("Extraktion Zeitüberschreitung") from None
            except asyncio.CancelledError:
                # Abgebrochen wurde nur der Pool-Auftrag (shutdown mit cancel_futures), nicht diese
                # Aufgabe selbst: wie ein abgebrochener Pool behandeln, nie als CancelledError
                # (und damit als 500) nach oben reichen.
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                if pool in self._discarded:
                    raise PoolDiscarded("Extraktion unterbrochen (Pool neu gestartet)") from None
                self._replace_pool(pool)
                raise BrokenProcessPool("Pool-Auftrag abgebrochen") from None
            except BrokenProcessPool:
                if pool in self._discarded:
                    raise PoolDiscarded("Extraktion unterbrochen (Pool neu gestartet)") from None
                self._replace_pool(pool)
                raise

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("Sammellauf fehlgeschlagen")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.settings.fetch_interval)
            except asyncio.TimeoutError:
                pass

    def trigger(self) -> bool:
        """Weckt die Schleife für einen sofortigen Lauf. False, wenn gerade einer läuft."""
        if self.state["running"]:
            return False
        self._wake.set()
        return True

    # ---- one cycle -----------------------------------------------------

    async def run_once(self) -> None:
        if self._run_lock.locked():
            return
        async with self._run_lock:
            self._prune_domains()
            self.state.update(running=True, last_started=utcnow())
            try:
                new = await self._fetch_all_feeds()
                extracted = await self._extract_pending()
                purged = self.db.purge(self.settings.retention_days)
                self.state.update(last_new=new, last_extracted=extracted, last_purged=purged)
                self.state["runs"] += 1
                log.info("Lauf fertig: %d neu, %d extrahiert, %d gelöscht", new, extracted, purged)
            finally:
                self.state.update(running=False, last_finished=utcnow())

    async def _fetch_all_feeds(self) -> int:
        ids = set(self.db.enabled_source_ids())
        sources = [s for s in self.catalog.all() if s.id in ids]
        sem = asyncio.Semaphore(FEED_CONCURRENCY)

        async def one(src: Source) -> int:
            async with sem:
                try:
                    return await self._fetch_feed(src)
                except Exception as e:  # noqa: BLE001
                    log.warning("Feed %s: %s", src.id, e)
                    self.db.record_fetch(src.id, ok=False, error=f"{type(e).__name__}: {e}")
                    return 0

        results = await asyncio.gather(*(one(s) for s in sources))
        return sum(results)

    # ---- HTTP ------------------------------------------------------------

    def _prune_domains(self) -> None:
        """Verwirft Domain-Einträge, die länger als DOMAIN_STATE_TTL nicht benutzt wurden."""
        cutoff = time.monotonic() - DOMAIN_STATE_TTL
        for domain in [d for d, last in self._domain_last.items() if last < cutoff]:
            lock = self._domain_locks.get(domain)
            if lock is not None and lock.locked():
                continue
            self._domain_last.pop(domain, None)
            self._domain_locks.pop(domain, None)

    async def _polite(self, url: str) -> None:
        """Hält den Mindestabstand pro Domain ein. Die Sperre gilt nur für die Wartezeit,
        nicht für die ganze Anfrage, damit eine langsame Domain nicht alles ausbremst."""
        domain = (urlparse(url).hostname or "").lower()
        async with self._domain_locks[domain]:
            wait = self._domain_last.get(domain, 0) + PER_DOMAIN_DELAY - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._domain_last[domain] = time.monotonic()

    async def _fetch_once(self, url: str, *, headers: dict | None, max_bytes: int) -> Fetched:
        """Ein Abruf mit eigenen Umleitungen, SSRF-Prüfung je Hop, Größen- und Gesamtzeitlimit."""
        assert self._client
        async with asyncio.timeout(TOTAL_TIMEOUT):
            current = url
            for _ in range(MAX_REDIRECTS + 1):
                await check_url(current)
                await self._polite(current)
                # Accept-Encoding auch pro Anfrage setzen (gilt dann unabhängig vom Client).
                req_headers = {**(headers or {}), "Accept-Encoding": "identity"}
                async with self._client.stream("GET", current, headers=req_headers) as r:
                    # Schutz gegen DNS-Rebinding: check_url hat den Namen aufgelöst, httpx löst ihn
                    # für die Verbindung aber erneut auf. Deshalb die tatsächliche Gegenstelle prüfen.
                    # Rest-Risiko: Die Anfrage selbst ist zu diesem Zeitpunkt schon gesendet, nur die
                    # Antwort wird verworfen. Ohne Netzwerk-Stream (MockTransport) keine Prüfung.
                    peer = _server_addr(r)
                    if server_addr_blocked(peer):
                        raise BlockedURL(f"Verbindung zu nicht erlaubter Adresse: {peer!r}")
                    location = r.headers.get("location")
                    if r.status_code in REDIRECT_CODES and location:
                        current = urljoin(str(r.url), location.strip())
                        continue
                    declared = r.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        raise FetchError(f"Antwort zu groß ({int(declared)} Bytes, Limit {max_bytes})")
                    return Fetched(r.status_code, r.headers, await _read_limited(r, max_bytes), str(r.url))
            raise FetchError(f"Zu viele Umleitungen (> {MAX_REDIRECTS})")

    async def _get(self, url: str, *, headers: dict | None = None, max_bytes: int) -> Fetched:
        """Abruf mit bis zu zwei Versuchen (zweiter mit anderem User-Agent), zusammen höchstens
        OVERALL_TIMEOUT Sekunden, jeder einzelne höchstens TOTAL_TIMEOUT."""
        async with asyncio.timeout(OVERALL_TIMEOUT):
            return await self._get_inner(url, headers=headers, max_bytes=max_bytes)

    async def _get_inner(self, url: str, *, headers: dict | None, max_bytes: int) -> Fetched:
        r = await self._fetch_once(url, headers=headers, max_bytes=max_bytes)
        if r.status_code == 429 and r.headers.get("retry-after"):
            return r  # Server nennt eine Wartezeit: respektieren statt mit anderem User-Agent nachzuhaken
        if r.status_code in (403, 406, 429) or (r.status_code == 200 and not r.content):
            alt = dict(headers or {})
            alt["User-Agent"] = READER_UA
            r2 = await self._fetch_once(url, headers=alt, max_bytes=max_bytes)
            if r2.status_code == 200 and r2.content:
                r = r2
        return r

    async def _fetch_feed(self, src: Source) -> int:
        state = self.db.source_states().get(src.id)
        headers: dict[str, str] = {"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"}
        if state is not None:
            if state["etag"]:
                headers["If-None-Match"] = state["etag"]
            if state["last_modified"]:
                headers["If-Modified-Since"] = state["last_modified"]
        r = await self._get(src.feed, headers=headers, max_bytes=FEED_MAX_BYTES)
        if r.status_code == 304:
            self.db.record_fetch(src.id, ok=True, new_count=0)
            return 0
        if r.status_code != 200:
            self.db.record_fetch(src.id, ok=False, error=f"HTTP {r.status_code}")
            return 0
        rows = await asyncio.to_thread(parse_feed, src, r.content)
        if not rows:
            self.db.record_fetch(src.id, ok=False, error="Feed enthält keine Einträge")
            return 0
        # Uralte Einträge (manche Feeds führen Evergreen-Artikel) gar nicht erst anlegen,
        # sonst würden sie bei jedem Lauf neu eingefügt, extrahiert und wieder gelöscht.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.settings.retention_days)).isoformat()
        rows = [row for row in rows if row["published_at"] >= cutoff]
        new = self.db.upsert_articles(rows)
        self.db.record_fetch(
            src.id, ok=True, new_count=new,
            etag=r.headers.get("etag"), last_modified=r.headers.get("last-modified"),
        )
        return new

    # ---- extraction ----------------------------------------------------

    async def _extract_pending(self) -> int:
        """Arbeitet die fälligen Artikel seitenweise ab (Keyset nach published_at, id), sodass
        Artikel, die nach ihrem Versuch 'pending' bleiben, die Schleife nicht vorzeitig beenden.
        Ein unerwarteter Fehler bei einem Artikel bricht den Lauf nicht ab."""
        done = 0
        after: tuple[str, int] | None = None
        while done < EXTRACT_MAX_PER_CYCLE:
            batch = self.db.pending_articles(min(EXTRACT_BATCH, EXTRACT_MAX_PER_CYCLE - done), before=after)
            if not batch:
                break
            after = (batch[-1]["published_at"], batch[-1]["id"])
            results = await asyncio.gather(
                *(self.extract_article(r["id"], r["url"]) for r in batch), return_exceptions=True,
            )
            for row, res in zip(batch, results):
                if isinstance(res, Exception):
                    log.error("Extraktion %s (%s) unerwartet gescheitert: %r", row["id"], row["url"], res)
            done += len(batch)
        return done

    def _spawn(self, article_id: int, coro) -> asyncio.Task:
        """Startet eine Extraktion als Task und trägt sie in _inflight ein. Der Eintrag wird nur
        entfernt, solange er noch zu genau diesem Task gehört."""
        task = asyncio.create_task(coro)
        self._inflight[article_id] = task

        def _done(t: asyncio.Task) -> None:
            if self._inflight.get(article_id) is t:
                del self._inflight[article_id]

        task.add_done_callback(_done)
        return task

    async def extract_article(self, article_id: int, url: str, *, priority: bool = False) -> None:
        """Lädt eine Artikelseite und speichert den bereinigten Volltext.

        Läuft für diese ID schon eine Extraktion, startet keine zweite: Abrufe auf Anforderung
        (priority=True, API) warten auf deren Ende, Hintergrund-Abrufe überspringen den Artikel.
        Hintergrund-Abrufe tragen sich erst in _inflight ein, wenn sie ihren Platz in der
        Hintergrund-Grenze haben; ein Abruf auf Anforderung hängt sich also nie an einen Artikel,
        der noch in der Warteschlange des Hintergrundlaufs steht, sondern startet sofort unter
        seiner eigenen kleinen Grenze.
        """
        if priority:
            task = self._inflight.get(article_id)
            if task is None:
                task = self._spawn(article_id, self._extract_priority(article_id, url))
            # shield: Bricht der Aufrufer ab (z. B. Browser schließt die Verbindung), läuft der
            # Abruf trotzdem zu Ende und sein Ergebnis wird gespeichert.
            await asyncio.shield(task)
            return
        async with self._extract_sem:
            # Zwischen Prüfung und Eintrag liegt kein await: keine doppelte Extraktion.
            if article_id in self._inflight:
                return  # läuft schon (Abruf auf Anforderung), dessen Ergebnis genügt
            # Kann seit dem Lesen der Liste per API erledigt oder neu eingeplant worden sein.
            if not self.db.is_due_pending(article_id):
                return
            task = self._spawn(article_id, self._extract(article_id, url))
            await asyncio.shield(task)

    def is_extracting(self, article_id: int) -> bool:
        """True, solange für diese ID eine Extraktion läuft."""
        return article_id in self._inflight

    def start_extraction(self, article_id: int, url: str) -> None:
        """Startet eine Extraktion auf Anforderung im Hintergrund, ohne auf sie zu warten
        (Leseansicht fragt per Polling nach). Läuft schon eine, passiert nichts."""
        if article_id not in self._inflight:
            self._spawn(article_id, self._extract_priority(article_id, url))

    async def _extract_priority(self, article_id: int, url: str) -> None:
        async with self._priority_sem:
            await self._extract(article_id, url)

    def _fail(self, article_id: int, error: str) -> None:
        # Vorhandener Text bleibt erhalten (siehe Database.store_extraction).
        self.db.store_extraction(article_id, status="failed", body_html=None, body_len=0,
                                 paywalled=False, error=error)

    def _retry(self, article_id: int, url: str, error: str, retry_after: float | None = None) -> None:
        status = self.db.schedule_retry(article_id, error, retry_after)
        log.info("Extraktion %s (%s): %s -> %s", article_id, url, error, status)

    async def _extract(self, article_id: int, url: str) -> None:
        try:
            r = await self._get(url, max_bytes=ARTICLE_MAX_BYTES)
        except BlockedURL as e:
            self._fail(article_id, f"Ziel blockiert: {e}")
            return
        except UnsupportedEncoding as e:
            self._fail(article_id, str(e))
            return
        except (httpx.InvalidURL, httpx.UnsupportedProtocol, ValueError) as e:
            # Kaputte URL (auch in einer Umleitung), z. B. ungültige IPv6-Klammer oder Port: ein
            # erneuter Versuch ändert daran nichts. Vor httpx.HTTPError, UnsupportedProtocol ist
            # eine Unterklasse davon.
            self._fail(article_id, f"Ungültige URL: {type(e).__name__}: {e}")
            return
        except (ResolveError, FetchError, httpx.HTTPError, TimeoutError) as e:
            self._retry(article_id, url, f"{type(e).__name__}: {e}")
            return
        except Exception as e:  # noqa: BLE001
            log.warning("Extraktion %s (%s): %s", article_id, url, e)
            self._retry(article_id, url, f"{type(e).__name__}: {e}")
            return

        if r.status_code != 200:
            error = f"HTTP {r.status_code}"
            if 400 <= r.status_code < 500 and r.status_code not in TRANSIENT_4XX:
                self._fail(article_id, error)
            else:
                retry_after = parse_retry_after(r.headers.get("retry-after")) if r.status_code == 429 else None
                self._retry(article_id, url, error, retry_after)
            return
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "xml" not in ctype:
            self._fail(article_id, f"Kein HTML ({ctype[:40]})")
            return
        try:
            result = await self._run_extract(r.content, r.url)
        except ExtractTimeout as e:
            self._retry(article_id, url, str(e))
            return
        except PoolDiscarded as e:
            self.db.requeue(article_id, str(e))
            log.info("Extraktion %s (%s): %s, erneut eingeplant", article_id, url, e)
            return
        except BrokenProcessPool as e:
            # Worker gestorben (z. B. Speicher) oder wegen eines hängenden Nachbarn beendet.
            self._retry(article_id, url, f"Extraktion abgebrochen: {type(e).__name__}")
            return
        except Exception as e:  # noqa: BLE001
            log.warning("Extraktion %s (%s): %s", article_id, url, e)
            self._fail(article_id, f"{type(e).__name__}: {e}")
            return
        row = self.db.get_article(article_id)
        result = drop_title_echo(result, row["title"] if row else None)
        # Feed-Bild nur als kleine Vorschau (Guardian ?width=140), die Seite nennt ein großes: das
        # große wird Titelbild. Nur beim ersten Holen des Textes; schon geholte Artikel bleiben, wie sie sind.
        feed_image = row["image_url"] if row and row["extracted_at"] is None else None
        replace_image = bool(feed_image and is_small_image_url(feed_image)
                             and result.image_url and not is_small_image_url(result.image_url))
        self.db.store_extraction(
            article_id, status=result.status, body_html=result.body_html, body_text=result.body_text,
            body_len=result.body_len, paywalled=result.paywalled, image_url=result.image_url,
            author=result.author, replace_image=replace_image,
        )


async def _read_limited(r: httpx.Response, max_bytes: int) -> bytes:
    """Liest die Antwort roh (ohne das Entpacken von httpx) und entpackt selbst, sodass weder die
    übertragenen noch die entpackten Bytes max_bytes überschreiten (Schutz vor Kompressionsbomben)."""
    if r.is_stream_consumed:
        # Nur bei vorgefertigten Antworten ohne Netzwerk (MockTransport in Tests): Der Inhalt liegt
        # schon vollständig im Speicher, es bleibt die Größenprüfung.
        if len(r.content) > max_bytes:
            raise FetchError(f"Antwort zu groß (über {max_bytes} Bytes)")
        return r.content
    if r.status_code in (204, 304):
        return b""  # nie ein Body, auch wenn Content-Encoding gesetzt ist
    decoder = _decoder(r.headers.get("content-encoding", ""))
    chunks: list[bytes] = []
    raw_total = total = 0

    def add(data: bytes) -> None:
        nonlocal total
        total += len(data)
        if total > max_bytes:
            raise FetchError(f"Antwort zu groß (über {max_bytes} Bytes)")
        chunks.append(data)

    async for chunk in r.aiter_raw():
        raw_total += len(chunk)
        if raw_total > max_bytes:
            raise FetchError(f"Antwort zu groß (über {max_bytes} Bytes)")
        if decoder is None:
            add(chunk)
            continue
        try:
            # max_length: Nie mehr als ein Byte über dem Rest-Limit entpacken. Bleibt die Ausgabe
            # darunter, ist die Eingabe vollständig verarbeitet (kein unconsumed_tail).
            add(decoder.decompress(chunk, max_bytes - total + 1))
        except zlib.error as e:
            raise FetchError(f"Entpacken fehlgeschlagen: {e}") from None
    if decoder is not None and raw_total == 0:
        return b""  # leerer Body trotz Content-Encoding-Header: nichts abgeschnitten
    if decoder is not None:
        try:
            add(decoder.flush())
        except zlib.error as e:
            raise FetchError(f"Entpacken fehlgeschlagen: {e}") from None
        # flush() meldet einen abgeschnittenen Datenstrom nicht als Fehler.
        if not decoder.eof:
            raise FetchError("Komprimierte Antwort unvollständig")
    return b"".join(chunks)


def _server_addr(response: httpx.Response) -> Any:
    """Adresse der Gegenstelle aus dem httpcore-Netzwerk-Stream, None wenn nicht verfügbar."""
    stream = response.extensions.get("network_stream")
    if stream is None or not hasattr(stream, "get_extra_info"):
        return None
    try:
        return stream.get_extra_info("server_addr")
    except Exception:  # noqa: BLE001
        return None
