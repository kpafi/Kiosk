"""Holt Feeds, legt neue Artikel an und extrahiert im Hintergrund den Volltext."""
from __future__ import annotations

import asyncio
import calendar
import concurrent.futures
import contextvars
import multiprocessing
import io
import logging
import math
import re
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
from .db import Database, QueryTimeout, request_time_limit, utcnow
from .extract import (clean_author, drop_title_echo, extract, is_placeholder, is_small_image_url,
                      is_text_card_image, safe_http_url, strip_html)
from .netguard import BlockedURL, ResolveError, check_url, server_addr_blocked, set_deny_networks
from .sitemap import ParsedRows, parse_sitemap, pick_child, sitemap_kind, url_host
from .sources import Catalog, Source
from . import stories

log = logging.getLogger("kiosk.fetcher")

READER_UA = "Mozilla/5.0 (compatible; KioskReader/0.1; private RSS reader)"
FEED_CONCURRENCY = 8
EXTRACT_BATCH = 60
EXTRACT_MAX_PER_CYCLE = 900
STORIES_IDLE_REFRESH = timedelta(minutes=30)   # ohne neue Artikel: Geschichten höchstens so oft neu gruppieren
PER_DOMAIN_DELAY = 0.4
IMAGE_TYPES = ("image/",)
# Eingangsgrenzen; die Ausgabe begrenzen FEED_MAX_ENTRIES und extract.MAX_BODY_HTML, beides wird im
# Worker-Prozess mit Zeitlimit verarbeitet.
FEED_MAX_BYTES = 5 * 1024 * 1024
ARTICLE_MAX_BYTES = 5 * 1024 * 1024
# Ein feindlicher Feed mit Zehntausenden winziger Einträge: nur die ersten FEED_MAX_ENTRIES zählen,
# und je Quelle und Lauf kommen höchstens FEED_MAX_NEW neue Artikel dazu (der Rest beim nächsten Lauf).
FEED_MAX_ENTRIES = 300
FEED_MAX_NEW = 150
FEED_PARSE_TIMEOUT = 30     # Sekunden für feedparser bzw. das Sitemap-Parsing im Worker-Prozess
FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"
TOTAL_TIMEOUT = 45          # Sekunden pro Abruf inkl. Umleitungen, unabhängig von httpx-Timeouts
OVERALL_TIMEOUT = 60        # Sekunden für _get insgesamt, inkl. zweitem Versuch mit anderem User-Agent
PRIORITY_CONCURRENCY = 2    # gleichzeitige Extraktionen auf Anforderung (API)
PRIORITY_QUEUE_MAX = 20     # höchstens so viele Extraktionen auf Anforderung laufen oder warten
RETRY_MIN_INTERVAL = 120    # Sekunden: ?retry=true startet frühestens so lange nach dem letzten Versuch neu
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


class TooLarge(FetchError):
    """Antwort größer als erlaubt. Für eine Artikelseite endgültig: Die Seite wird beim nächsten
    Versuch nicht kleiner."""


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
    """Ein Auftrag im Worker-Prozess (Extraktion, Feed-Parsing) hat sein Zeitlimit überschritten."""


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
    """Retry-After als Sekunden (Zahl oder HTTP-Datum), begrenzt auf MAX_RETRY_AFTER. "nan" und
    "inf" (float() nimmt beides an) gelten als ungültig."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        seconds = float(raw)
        if not math.isfinite(seconds):
            return None
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)


def http_token(value: str | None) -> str | None:
    """Kopfzeilenwert (ETag, Last-Modified) nur aus druckbarem ASCII, sonst None. httpx kann
    andere Zeichen nicht senden (UnicodeEncodeError vor der Anfrage)."""
    if not value or len(value) > 500 or not all(" " <= ch <= "~" for ch in value):
        return None
    return value


def _entry_timestamp(entry: Any) -> int | None:
    """Zeitpunkt des Eintrags (Unix-Sekunden) aus dem ersten brauchbaren Datum, sonst None."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        st = entry.get(key)
        if st:
            try:
                ts = calendar.timegm(st)
                datetime.fromtimestamp(ts, tz=timezone.utc)  # außerhalb des darstellbaren Bereichs?
                return ts
            except (OverflowError, ValueError, OSError):
                continue
    return None


def _entry_datetime(entry: Any) -> str:
    ts = _entry_timestamp(entry)
    if ts is None:
        return utcnow()
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0).isoformat()


def _entry_image(entry: Any) -> str | None:
    """Bild des Feed-Eintrags; Texttafeln (Meduza) zählen nicht, BBC-Vorschaubilder kommen in größerer
    Breite (siehe _feed_image)."""
    return _feed_image(_entry_image_raw(entry))


# BBC liefert im Feed ein 240 px breites Vorschaubild (…/ace/standard/240/…); dasselbe Bild gibt es in
# 976 px. Das og:image der Seite (branded_news) trägt ein eingebranntes BBC-Logo, deshalb nicht das.
_BBC_THUMB_RE = re.compile(r"^(https://ichef\.bbci\.co\.uk/ace/standard/)240/")


def _feed_image(url: str | None) -> str | None:
    if not url or is_text_card_image(url):
        return None
    return _BBC_THUMB_RE.sub(r"\g<1>976/", url)


def _entry_image_raw(entry: Any) -> str | None:
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


# Autorenfeld mit Beiwerk: ZEIT „DIE ZEIT: Politik - Mounia Meiborg“ (ohne Autor „DIE ZEIT: News -“),
# SZ „Von Robert Roßmann, Berlin“ bzw. „Kommentar von …“ und im Liveblog „Alle Entwicklungen im
# Liveblog“ statt eines Namens. „Von“ nur vor einem großgeschriebenen Namen („Von der Leyen, Ursula“
# bleibt, ebenso „Von Hippel, Frank“: bliebe nur ein Wort, gehört „Von“ zum Namen); der Ort fällt nur weg,
# wenn er ein einzelnes großgeschriebenes Wort ist und davor nur Namen aus je zwei Wörtern stehen, ohne
# Komma („Von Anna Meier und Paul Schulz, Berlin“ → „Anna Meier, Paul Schulz“), oder ein bekannter Ort aus
# zwei Wörtern nach genau einem Namen („Von Anna Muster, New York“ → „Anna Muster“); „Von Anna Meier, Paul
# Schulz“ bleibt „Anna Meier, Paul Schulz“.
_ZEIT_AUTHOR_RE = re.compile(r"^DIE ZEIT:.*?\s-(?:\s+|$)")
_BYLINE_RE = re.compile(
    r"^(?:(?:Eine?\s+)?(?:Kommentar|Analyse|Interview|Gastbeitrag|Essay|Kolumne|Reportage)\s+)?[Vv]on\s+"
    r"(?=[A-ZÄÖÜ])(.+)$", re.S)
_BYLINE_PLACE_RE = re.compile(r"^(.+),\s*([A-ZÄÖÜ][\wäöüß.-]*(?:\s[A-ZÄÖÜ][\wäöüß-]*)?)$", re.S)
# Orte aus zwei Wörtern nur mit diesen Anfängen („New York“); „Paul Schulz“ könnte ein zweiter Name sein
_TWO_WORD_PLACES = {"New", "Los", "Las", "San", "Santa", "Sankt", "St.", "Bad", "Hong", "Tel", "Kuala", "Buenos",
                    "Den", "La", "Le", "El", "Rio", "Addis", "Abu", "Sri", "Neu", "Frankfurt"}
_FEED_AUTHOR_JUNK = {"alle entwicklungen im liveblog"}


def _entry_author(entry: Any) -> str | None:
    author = strip_html(entry.get("author") or "", limit=200)
    author = _ZEIT_AUTHOR_RE.sub("", author)
    m = _BYLINE_RE.match(author)
    if m:
        name = m.group(1)
        place = _BYLINE_PLACE_RE.match(name)
        if place and "," not in place.group(1):
            names = re.split(r"\s+(?:und|&)\s+", place.group(1))
            words = place.group(2).split()
            one_name = len(names) == 1 and len(names[0].split()) >= 2
            if all(len(n.split()) >= 2 for n in names) and (
                    len(words) == 1 or (one_name and words[0] in _TWO_WORD_PLACES)):
                name = place.group(1)   # ein Name oder eine Liste von Namen, danach ein Ort
        # „Von Hippel, Frank“, „Von Meier“: „Von“ gehört zum Namen, wenn danach nur ein Wort bleibt
        if len(name.split(",")[0].split()) >= 2:
            author = name
    if author.casefold() in _FEED_AUTHOR_JUNK:
        return None
    return clean_author(author)


def parse_feed(source: Source, raw: bytes) -> ParsedRows:
    """Einträge eines Feeds als Zeilen für upsert_articles, höchstens die ersten FEED_MAX_ENTRIES.
    Läuft im Worker-Prozess (Fetcher._fetch_feed): feedparser braucht für einen feindlichen Feed
    viel Speicher und Zeit."""
    # Als Datei-Objekt übergeben: Bei bytes/str versucht feedparser zuerst, den Inhalt als
    # Dateipfad oder URL zu öffnen (ein Body "/proc/self/environ" würde gelesen).
    parsed = feedparser.parse(io.BytesIO(raw))
    now = utcnow()
    rows = ParsedRows()
    rows.total = len(parsed.entries)
    # Nur gewünschte Bereiche (include_paths der Quelle), vor der Obergrenze: Sonst verdrängten
    # ungewollte Einträge die gewollten.
    entries = [e for e in parsed.entries if source.wants_url((e.get("link") or "").strip())]
    rows.filtered_out = len(parsed.entries) - len(entries)
    # Neueste zuerst, bevor gekürzt wird: Manche Feeds führen die ältesten Einträge oben. Einträge
    # ohne Datum kommen ans Ende, sonst bleibt die Reihenfolge des Feeds (sort ist stabil).
    stamps = [_entry_timestamp(e) for e in entries]
    order = sorted(range(len(stamps)), key=lambda i: math.inf if stamps[i] is None else -stamps[i])
    entries = [entries[i] for i in order]
    # Einträge mit Datum in der Zukunft würden sonst dauerhaft oben kleben.
    for e in entries[:FEED_MAX_ENTRIES]:
        link = (e.get("link") or "").strip()
        if not link.startswith(("http://", "https://")):
            continue
        title = strip_html(e.get("title") or "", limit=300) or "(ohne Titel)"
        rows.append({
            "source_id": source.id,
            "guid": source.guid_for(link, (e.get("id") or link).strip())[:500],
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
        self._extracted_ids: list[int] = []         # zuletzt extrahierte Artikel (siehe run_once)
        self._stories_at = -math.inf                 # monotone Zeit des letzten Laufs der Gruppierung
        # Extraktionen auf Anforderung: wie viele laufen oder warten (Grenze PRIORITY_QUEUE_MAX) und
        # wann die letzte je Artikel endete (monotone Zeit, für RETRY_MIN_INTERVAL).
        self._ondemand_count = 0
        self._ondemand_done: dict[int, float] = {}
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
        # Artikel, deren Extraktion schon einmal mit einem gestorbenen Worker endete. Wer schuld war,
        # ist nicht bekannt (z. B. ein Feed, der den Speicher sprengte): Beim ersten Mal wird ohne
        # Zählung neu eingeplant, erst ein zweiter Absturz mit demselben Artikel zählt als Versuch.
        self._crash_suspects: set[int] = set()
        # Zuletzt gewählte Sitemap je Quelle mit Sitemap-Index (nur fürs Protokoll bei einem Wechsel).
        self._sitemap_child: dict[str, str] = {}
        # Zusätzlich gesperrte Ziele (KIOSK_FETCH_DENY_IPS) gelten für alle Abrufe dieses Prozesses.
        set_deny_networks(settings.fetch_deny_ips)

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
        """Der Prozess-Pool für Extraktion und Feed-Parsing; wird beim ersten Bedarf angelegt.

        Startmethode ist bewusst "forkserver" statt des Linux-Standards "fork": uvicorn hat zu
        diesem Zeitpunkt schon Threads (asyncio.to_thread, httpx), und fork() in einem Prozess
        mit Threads kann im Kind gehaltene Locks (Logging, malloc) einfrieren. Der Forkserver
        wird einmal sauber per exec gestartet, die Worker forken von ihm. Die Worker führen nur
        die Modulfunktionen extract(), parse_feed() und für Sitemaps pick_child() und
        parse_sitemap() mit bytes, str, Source und Hostnamen aus und fassen
        weder Datenbank noch Event-Loop an. Sie starten beim ersten Auftrag, laufen im selben
        Container (pids_limit, mem_limit) und brauchen /dev/shm für die Semaphoren von multiprocessing.
        max_tasks_per_child ersetzt jeden Worker nach EXTRACT_TASKS_PER_CHILD Aufträgen durch
        einen frischen Prozess (begrenzt schleichend wachsenden Speicher; mit forkserver erlaubt).
        """
        if self._pool is None:
            ctx = multiprocessing.get_context("forkserver")
            # Der Forkserver lädt die Module der Aufträge (extract, parse_feed: trafilatura, lxml,
            # feedparser) einmal vorab; jeder neue Worker (alle EXTRACT_TASKS_PER_CHILD Aufträge)
            # forkt sie fertig geladen, statt sie erneut zu importieren. Wirkt nur, bevor der
            # Forkserver startet (erster Auftrag); Importfehler ignoriert multiprocessing dabei.
            ctx.set_forkserver_preload(["app.fetcher"])
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
        """Führt extract() im Prozess-Pool aus, höchstens EXTRACT_TIMEOUT Sekunden reine Laufzeit."""
        # extract wird zur Laufzeit aus dem Modul gelesen (in Tests austauschbar); an den Worker
        # geht nur eine Referenz auf die Modulfunktion plus bytes und str (picklebar).
        return await self._run_in_pool(extract, page, url, timeout=EXTRACT_TIMEOUT, what="Extraktion")

    async def _run_in_pool(self, fn, *args, timeout: float, what: str):
        """Führt fn(*args) im Prozess-Pool aus, höchstens `timeout` Sekunden reine Laufzeit.

        Wartet zuerst auf einen freien Worker-Platz (_pool_sem), erst danach läuft das Zeitlimit.
        Überschreitet ein Auftrag das Limit, wird der Pool verworfen (ExtractTimeout); die übrigen
        laufenden Aufträge dieses Pools enden dann mit PoolDiscarded (zählt nicht als Versuch).
        `what` benennt den Auftrag in den Fehlermeldungen.
        """
        async with self._pool_sem:
            pool = self._extraction_pool()
            loop = asyncio.get_running_loop()
            try:
                # submit wirft BrokenProcessPool sofort, wenn ein Worker schon vorher gestorben ist.
                future = loop.run_in_executor(pool, fn, *args)
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                # Nur hier verwerfen wir einen gesunden Pool selbst; stirbt ein Worker von allein
                # (z. B. Speicher), wissen wir nicht, welcher Auftrag schuld war, und alle zählen.
                self._discarded.add(pool)
                self._replace_pool(pool)
                raise ExtractTimeout(f"{what} Zeitüberschreitung") from None
            except asyncio.CancelledError:
                # Abgebrochen wurde nur der Pool-Auftrag (shutdown mit cancel_futures), nicht diese
                # Aufgabe selbst: wie ein abgebrochener Pool behandeln, nie als CancelledError
                # (und damit als 500) nach oben reichen.
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                if pool in self._discarded:
                    raise PoolDiscarded(f"{what} unterbrochen (Pool neu gestartet)") from None
                self._replace_pool(pool)
                raise BrokenProcessPool("Pool-Auftrag abgebrochen") from None
            except BrokenProcessPool:
                if pool in self._discarded:
                    raise PoolDiscarded(f"{what} unterbrochen (Pool neu gestartet)") from None
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
                try:
                    new = await self._fetch_all_feeds()
                    # Neue Artikel gleich gruppieren, nicht erst nach der (langen) Textextraktion. Ohne
                    # neue Artikel nur, wenn der letzte Lauf STORIES_IDLE_REFRESH her ist (das Fenster
                    # wandert trotzdem weiter).
                    if new or time.monotonic() - self._stories_at >= STORIES_IDLE_REFRESH.total_seconds():
                        await self.update_stories()
                    extracted = await self._extract_pending()
                    await self.update_stories(only_if_new_bodies=True)
                except Exception:
                    self.db.purge(self.settings.retention_days)  # aufräumen auch nach einem Fehler davor
                    raise
                purged = self.db.purge(self.settings.retention_days)
                self.state.update(last_new=new, last_extracted=extracted, last_purged=purged)
                self.state["runs"] += 1
                log.info("Lauf fertig: %d neu, %d extrahiert, %d gelöscht", new, extracted, purged)
            finally:
                self.state.update(running=False, last_finished=utcnow())

    async def update_stories(self, only_if_new_bodies: bool = False) -> None:
        """Geschichten neu gruppieren (app/stories.py): nach jedem Abruf der Feeds und einmal beim
        Start. Mit `only_if_new_bodies` (nach der Extraktion) nur, wenn ein Artikel ohne brauchbaren
        Anreißer gerade seinen Volltext bekam (den gruppiert erst der Volltext, siehe stories._fields).
        Ein Fehler wird protokolliert und beendet nie den Sammellauf."""
        try:
            if only_if_new_bodies and not self.db.short_summaries_with_body(
                    self._extracted_ids, stories.BODY_IF_SUMMARY_BELOW):
                return
            await stories.refresh(self.db, self.catalog)
            self._stories_at = time.monotonic()
        except Exception:  # noqa: BLE001
            log.exception("Geschichten: Lauf fehlgeschlagen")

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
                        raise TooLarge(f"Antwort zu groß ({int(declared)} Bytes, Limit {max_bytes})")
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
        headers: dict[str, str] = {"Accept": FEED_ACCEPT}
        if state is not None:
            # Gespeicherte Validatoren nur senden, wenn sie reines ASCII sind (ältere Datenbanken
            # können andere enthalten; httpx würde an ihnen bei jedem Abruf scheitern).
            if http_token(state["etag"]):
                headers["If-None-Match"] = state["etag"]
            if http_token(state["last_modified"]):
                headers["If-Modified-Since"] = state["last_modified"]
        r = await self._get(src.feed, headers=headers, max_bytes=FEED_MAX_BYTES)
        if r.status_code == 304:
            self.db.record_fetch(src.id, ok=True, new_count=0)
            return 0
        if r.status_code != 200:
            self.db.record_fetch(src.id, ok=False, error=f"HTTP {r.status_code}")
            return 0
        # Liest nur den Anfang bis zum Wurzelelement (höchstens sitemap.SNIFF_BYTES, ohne DOCTYPE).
        kind = sitemap_kind(r.content)
        if kind is not None:
            return await self._fetch_sitemap(src, r, kind)
        # Im Worker-Prozess mit Zeitlimit: Ein feindlicher Feed kostet dort Speicher und Zeit, nicht
        # in der App (deren Speicher bliebe fragmentiert). Fehler landen in _fetch_all_feeds.
        rows = await self._run_in_pool(parse_feed, src, r.content, timeout=FEED_PARSE_TIMEOUT,
                                       what="Feed-Parsing")
        # Hat nur include_paths alles verworfen, ist der Feed in Ordnung, nur ohne Gewolltes.
        return self._store_rows(src, rows, (http_token(r.headers.get("etag")),
                                            http_token(r.headers.get("last-modified"))),
                                empty_ok=getattr(rows, "filtered_out", 0) > 0)

    async def _fetch_sitemap(self, src: Source, r: Fetched, kind: str) -> int:
        """Google-News-Sitemap statt Feed (am Inhalt erkannt, siehe app/sitemap.py).

        Ein Sitemap-Index verweist auf die eigentliche URL-Liste (bei GEA wechselt deren Name jedes
        Quartal): Genommen wird die mit dem jüngsten lastmod, nur auf dem Host des Index (wie
        konfiguriert oder nach Umleitung; das Sitemap-Protokoll erlaubt nur Sitemaps derselben
        Website), abgerufen über denselben geprüften Weg wie jeder Feed (_get: netguard je Hop,
        Größen- und Zeitlimits), höchstens eine je Lauf; ein Index, der wieder auf einen Index
        zeigt, gilt als Fehler. Validatoren (ETag, Last-Modified) speichern Sitemaps nie, also
        schickt der nächste Abruf keine Bedingung: Ein 304 auf den Index sagte nichts darüber, ob
        sich die URL-Liste geändert hat.
        """
        if kind == "sitemapindex":
            hosts = frozenset(h for h in (url_host(src.feed), url_host(r.url)) if h)
            child = await self._run_in_pool(pick_child, r.content, hosts, timeout=FEED_PARSE_TIMEOUT,
                                            what="Sitemap-Parsing")
            if child is None:
                self.db.record_fetch(src.id, ok=False, error="Sitemap-Index nennt keine zulässige Sitemap")
                return 0
            if self._sitemap_child.get(src.id) != child:   # z. B. am Quartalswechsel
                log.info("Sitemap %s: %s", src.id, child)
                self._sitemap_child[src.id] = child
            r = await self._get(child, headers={"Accept": FEED_ACCEPT}, max_bytes=FEED_MAX_BYTES)
            if r.status_code != 200:
                self.db.record_fetch(src.id, ok=False, error=f"Sitemap: HTTP {r.status_code}")
                return 0
            if sitemap_kind(r.content) != "urlset":
                self.db.record_fetch(src.id, ok=False, error="Sitemap-Index verweist nicht auf eine URL-Liste")
                return 0
        rows = await self._run_in_pool(parse_sitemap, src, r.content, FEED_MAX_ENTRIES,
                                       timeout=FEED_PARSE_TIMEOUT, what="Sitemap-Parsing")
        # Leer ist nur dann kein Fehler, wenn die URL-Liste gar keine Einträge hat (neue Quartals-
        # Sitemap kurz nach dem Wechsel) oder include_paths welche verworfen hat. Blieb aus anderen
        # Gründen nichts übrig (z. B. lauter unbrauchbare URLs), ist es ein Fehler wie beim Feed.
        return self._store_rows(src, rows, (None, None),
                                empty_ok=getattr(rows, "total", 1) == 0 or getattr(rows, "filtered_out", 0) > 0)

    def _store_rows(self, src: Source, rows: list[dict[str, Any]],
                    validators: tuple[str | None, str | None], *, empty_ok: bool = False) -> int:
        """Legt die Einträge eines Abrufs an und speichert das Ergebnis samt Validatoren. Ohne
        Einträge ein Fehler, außer mit `empty_ok` (dann ein Abruf ohne neue Artikel)."""
        if not rows and not empty_ok:
            self.db.record_fetch(src.id, ok=False, error="Feed enthält keine Einträge")
            return 0
        # Uralte Einträge (manche Feeds führen Evergreen-Artikel) gar nicht erst anlegen,
        # sonst würden sie bei jedem Lauf neu eingefügt, extrahiert und wieder gelöscht.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.settings.retention_days)).isoformat()
        rows = [row for row in rows if row["published_at"] >= cutoff]
        new = self.db.upsert_articles(rows, max_new=FEED_MAX_NEW)
        # Grenze erreicht: Validatoren nicht speichern, sonst hielte ein 304 die übrigen Einträge
        # zurück; der nächste Abruf geht ohne Bedingung und holt sie.
        if new >= FEED_MAX_NEW:
            validators = (None, None)
        self.db.record_fetch(src.id, ok=True, new_count=new, validators=validators)
        return new

    # ---- extraction ----------------------------------------------------

    async def _extract_pending(self) -> int:
        """Arbeitet die fälligen Artikel seitenweise ab (Keyset nach published_at, id), sodass
        Artikel, die nach ihrem Versuch 'pending' bleiben, die Schleife nicht vorzeitig beenden.
        Ein unerwarteter Fehler bei einem Artikel bricht den Lauf nicht ab."""
        done = 0
        after: tuple[str, int] | None = None
        self._extracted_ids = []
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
                else:
                    self._extracted_ids.append(row["id"])
            done += len(batch)
        return done

    def _spawn(self, article_id: int, coro) -> asyncio.Task:
        """Startet eine Extraktion als Task und trägt sie in _inflight ein. Der Eintrag wird nur
        entfernt, solange er noch zu genau diesem Task gehört."""
        # Ohne die Frist einer API-Anfrage (request_time_limit), aus der start_extraction kommt:
        # Lesen im Sammler/Extraktor ist Hintergrundarbeit.
        ctx = contextvars.copy_context()
        ctx.run(request_time_limit.set, None)
        task = asyncio.create_task(coro, context=ctx)
        self._inflight[article_id] = task

        def _done(t: asyncio.Task) -> None:
            if self._inflight.get(article_id) is t:
                del self._inflight[article_id]

        task.add_done_callback(_done)
        return task

    async def extract_article(self, article_id: int, url: str) -> None:
        """Hintergrund-Extraktion: lädt eine Artikelseite und speichert den bereinigten Volltext.

        Läuft für diese ID schon eine Extraktion (z. B. auf Anforderung, start_extraction), wird
        der Artikel übersprungen. Hintergrund-Abrufe tragen sich erst in _inflight ein, wenn sie
        ihren Platz in der Hintergrund-Grenze haben; ein Abruf auf Anforderung hängt sich also nie
        an einen Artikel, der noch in der Warteschlange des Hintergrundlaufs steht, sondern startet
        sofort unter seiner eigenen kleinen Grenze.
        """
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

    def start_extraction(self, article_id: int, url: str, *, retry: bool = False) -> bool:
        """Startet eine Extraktion auf Anforderung im Hintergrund, ohne auf sie zu warten
        (Leseansicht fragt per Polling nach). Gibt zurück, ob jetzt eine läuft.

        Läuft schon eine, passiert nichts (True). Keine neue (False), wenn schon PRIORITY_QUEUE_MAX
        Abrufe auf Anforderung laufen oder warten, oder bei `retry` (erneuter Versuch für einen
        abgeschlossenen Artikel), wenn der letzte Abruf auf Anforderung keine RETRY_MIN_INTERVAL
        Sekunden zurückliegt: Wiederholtes Abfragen soll keine Abrufe stapeln."""
        if article_id in self._inflight:
            return True
        now = time.monotonic()
        if retry and now - self._ondemand_done.get(article_id, -math.inf) < RETRY_MIN_INTERVAL:
            return False
        if self._ondemand_count >= PRIORITY_QUEUE_MAX:
            return False
        self._ondemand_count += 1
        task = self._spawn(article_id, self._extract_priority(article_id, url))

        def _finished(_t: asyncio.Task) -> None:
            self._ondemand_count -= 1
            done = time.monotonic()
            if len(self._ondemand_done) >= 1000:  # nur die jüngsten Einträge zählen noch
                for aid in [a for a, t in self._ondemand_done.items() if done - t >= RETRY_MIN_INTERVAL]:
                    del self._ondemand_done[aid]
            self._ondemand_done[article_id] = done

        task.add_done_callback(_finished)
        return True

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
        """Ein Extraktionsversuch. Geht dabei unerwartet etwas schief (auch in der Buchführung,
        z. B. beim Speichern eines Fehlers), zählt das als vorübergehender Fehlschlag: Sonst bliebe
        der Artikel 'pending' und fällig und würde in jedem Lauf erneut geholt."""
        try:
            await self._extract_once(article_id, url)
        except QueryTimeout:
            # Nur möglich, wenn doch eine Frist galt (Hintergrund läuft ohne): kein Fehler des
            # Artikels, zählt nicht als Versuch.
            log.warning("Extraktion %s (%s): Datenbankabfrage abgebrochen, später erneut", article_id, url)
        except Exception as e:  # noqa: BLE001  (CancelledError ist keine Exception)
            log.exception("Extraktion %s (%s) unerwartet gescheitert", article_id, url)
            try:
                self.db.schedule_retry(article_id, f"Interner Fehler: {type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                log.exception("Extraktion %s: Fehlschlag ließ sich nicht speichern", article_id)

    async def _extract_once(self, article_id: int, url: str) -> None:
        try:
            r = await self._get(url, max_bytes=ARTICLE_MAX_BYTES)
        except BlockedURL as e:
            self._fail(article_id, f"Ziel blockiert: {e}")
            return
        except (UnsupportedEncoding, TooLarge) as e:
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
            # Worker gestorben (z. B. Speicher, auch durch einen Nachbarauftrag wie einen feindlichen
            # Feed): beim ersten Mal ohne Zählung neu einplanen, siehe _crash_suspects.
            if article_id in self._crash_suspects:
                self._crash_suspects.discard(article_id)
                self._retry(article_id, url, f"Extraktion abgebrochen: {type(e).__name__}")
            else:
                if len(self._crash_suspects) >= 1000:
                    self._crash_suspects.clear()
                self._crash_suspects.add(article_id)
                self.db.requeue(article_id, "Extraktion abgebrochen (Worker gestorben), erneut eingeplant")
                log.info("Extraktion %s (%s): Worker gestorben, erneut eingeplant", article_id, url)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("Extraktion %s (%s): %s", article_id, url, e)
            self._fail(article_id, f"{type(e).__name__}: {e}")
            return
        row = self.db.get_article(article_id)
        # Im Thread: arbeitet auf bis zu MAX_BODY_HTML Zeichen HTML und soll die Event-Loop nicht aufhalten.
        result = await asyncio.to_thread(drop_title_echo, result, row["title"] if row else None)
        # Feed-Bild nur als kleine Vorschau (Guardian ?width=140), die Seite nennt ein großes: das
        # große wird Titelbild. Nur beim ersten Holen des Textes; schon geholte Artikel bleiben, wie sie sind.
        feed_image = row["image_url"] if row and row["extracted_at"] is None else None
        replace_image = bool(feed_image and is_small_image_url(feed_image)
                             and result.image_url and not is_small_image_url(result.image_url))
        self._crash_suspects.discard(article_id)
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
            raise TooLarge(f"Antwort zu groß (über {max_bytes} Bytes)")
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
            raise TooLarge(f"Antwort zu groß (über {max_bytes} Bytes)")
        chunks.append(data)

    async for chunk in r.aiter_raw():
        raw_total += len(chunk)
        if raw_total > max_bytes:
            raise TooLarge(f"Antwort zu groß (über {max_bytes} Bytes)")
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
