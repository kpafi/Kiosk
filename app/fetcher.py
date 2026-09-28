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
import os
import pickle
import re
import resource
import signal
import time
import zlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import feedparser
import httpx

from .config import Settings
from .db import Database, QueryTimeout, _iso, request_time_limit, utcnow
from .extract import drop_title_echo, extract, is_placeholder, is_small_image_url, safe_http_url, strip_html
from .netguard import BlockedURL, GuardedBackend, ResolveError, check_url, server_addr_blocked, set_deny_networks
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
EXTRACT_WORKERS = 2         # gleichzeitige Worker-Prozesse für die Extraktion (begrenzt CPU und Speicher)
FEED_PARSE_WORKERS = 1      # eigener Platz fürs Feed-Parsing: wartet nie auf Extraktionen und umgekehrt
# Grenzen je Worker-Prozess (setrlimit im Prozess selbst, siehe _job_main): Adressraum (ein Auftrag,
# der mehr braucht, endet mit MemoryError statt den Container an sein mem_limit zu bringen; echte
# Seiten brauchen bis ca. 120 MB, eine 5-MB-Seite aus reinem Text ca. 190 MB, davon ca. 75 MB
# geladener Code) und CPU-Zeit als Rückfallebene zum Zeitlimit (danach SIGXCPU bzw. SIGKILL).
# (EXTRACT_WORKERS + FEED_PARSE_WORKERS) x JOB_MEMORY_LIMIT + App passen in mem_limit von
# docker-compose.yml (3 x 320 MB + 320 MB = 1280 MB), siehe dort.
JOB_MEMORY_LIMIT = 320 * 1024 * 1024
JOB_CPU_GRACE = 10          # Sekunden CPU-Zeit über dem Zeitlimit des Auftrags
# Leistungsschalter je Quelle: Nach so vielen Zeitüberschreitungen bzw. Abstürzen in einem Lauf (oder
# binnen SOURCE_TROUBLE_TTL Sekunden) werden weitere Artikel dieser Quelle in diesem Lauf nicht mehr
# versucht; sie bleiben fällig, ohne dass ein Versuch zählt. Ein feindlicher Feed mit 150 langsamen
# Seiten kostet so 2 x EXTRACT_TIMEOUT statt 150 x EXTRACT_TIMEOUT je Lauf.
SOURCE_TROUBLE_LIMIT = 2
SOURCE_TROUBLE_TTL = 3600
# Vorprüfung von Seiten und Feeds vor dem Parsen (markup_problem, linear): trafilatura/lxml und
# feedparser brauchen für manche Formen quadratisch Zeit oder sehr viel Speicher (250 000 kurze
# Absätze: 3 min; ein Tag mit 100 000 Attributen: 2,5 min; 650 000 leere Elemente: 1,8 GB;
# 20 000 xmlns-Deklarationen im Feed: 13 s). Eine Ablehnung ist endgültig, die Grenzen liegen deshalb
# weit über echten Seiten: bis ca. 36 000 Tags (heise-Bestenliste, 2 MB), Live-Blogs von Guardian und
# FAZ (1,4 MB) ca. 4 600. Die ungünstigste bekannte Form (lauter unbeendete <p>) kostet trafilatura
# bei 100 000 Tags ca. 50 s (unter EXTRACT_TIMEOUT), bei 150 000 schon über 140 s; was knapp unter der
# Grenze langsam ist, fängt das Zeitlimit im eigenen Prozess ab.
MAX_PAGE_TAGS = 100_000     # Tags außerhalb von script/style
# "<" insgesamt, auch in Skripten und Kommentaren: begrenzt, was dort (ob zu Recht oder nicht)
# übersprungen wird, und die Arbeit der Prüfung selbst. Echte Seiten bis ca. 36 000.
MAX_PAGE_LT = 250_000
MAX_TAG_ATTRS = 256         # Attribute (Stücke außerhalb von Attributwerten in Anführungszeichen) in einem Tag
MAX_FEED_TAGS = 100_000
MAX_FEED_LT = 250_000       # "<" insgesamt im Feed (echte Feeds: bis ca. 15 500, FOCUS)
# Verschiedene Namensraum-Präfixe (xmlns:x) im ganzen Feed; echte Feeds haben wenige (Der Standard
# deklariert sie an jedem Eintrag neu: 468 Deklarationen, aber wenige Präfixe).
MAX_FEED_PREFIXES = 200
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


class JobCrashed(Exception):
    """Der Worker-Prozess eines Auftrags endete ohne Ergebnis (Signal, z. B. SIGKILL durch den
    OOM-Killer oder SIGXCPU). Betrifft nur diesen Auftrag."""


class JobInfraError(Exception):
    """Fehler der Worker-Mechanik, nicht der Seite (Ergebnis nicht übertragbar, SystemExit im Worker):
    vorübergehend, zählt als Versuch."""


class JobStartFailed(JobInfraError):
    """Der Worker-Prozess ließ sich nicht starten (fork: EAGAIN an pids_limit, Forkserver weg). Kein
    Fehler des Artikels: wird ohne Zählung eines Versuchs erneut eingeplant."""


class JobSkipped(Exception):
    """Der Auftrag wurde nicht gestartet: Der Leistungsschalter der Quelle hat inzwischen ausgelöst."""


class JobOutOfMemory(Exception):
    """Der Auftrag brauchte mehr als JOB_MEMORY_LIMIT Adressraum (MemoryError im Worker)."""


class MarkupRejected(Exception):
    """Seite oder Feed abgelehnt, bevor ein Parser sie sieht (markup_problem). Endgültig."""


# Fehler mit fertiger deutscher Meldung für die Oberfläche (ohne Typname davor, siehe error_text).
GERMAN_ERRORS = (ExtractTimeout, JobCrashed, JobOutOfMemory, JobInfraError, MarkupRejected, BlockedURL,
                 ResolveError, FetchError, UnsupportedEncoding)


def error_text(e: BaseException) -> str:
    """Fehlermeldung zum Speichern: eigene (deutsche) Meldungen unverändert, fremde mit Typname."""
    return str(e) if isinstance(e, GERMAN_ERRORS) else f"{type(e).__name__}: {e}"


def markup_problem(raw: bytes, *, max_tags: int, max_attrs: int, max_lt: int,
                   max_prefixes: int | None = None) -> str | None:
    """Grund zur Ablehnung einer Seite bzw. eines Feeds, sonst None. Linear in der Größe.

    Zählt "<" insgesamt (höchstens `max_lt`, auch in Skripten und Kommentaren: Obergrenze für alles,
    was die Prüfung selbst überspringt), Tags (höchstens `max_tags`) und Attribute je Start-Tag
    (höchstens `max_attrs`), optional verschiedene Namensraum-Präfixe (xmlns:…, `max_prefixes`;
    feedparser wird erst mit vielen verschiedenen quadratisch). Ein kleiner Zustandsautomat
    (_Scanner): Start-Tags mit Anführungszeichen (ein ">" oder "<!--" in einem Attributwert beendet
    nichts), Kommentare ("-->" oder "--!>"), Inhalt von script/style bis zum schließenden Tag, CDATA
    (der Inhalt zählt mit: feedparser wertet HTML darin aus). Was nicht sauber endet (Kommentar,
    Skript, Anführungszeichen), gilt ab dort wieder als Markup: im Zweifel mehr zählen."""
    low = raw.lower()
    if low.count(b"<") > max_lt:
        return f"zu viel Markup (über {_num(max_lt)} Zeichen „<“)"
    if max_prefixes is not None:
        prefixes: set[bytes] = set()
        for m in _XMLNS_PREFIX_RE.finditer(low):
            prefixes.add(m[1])
            if len(prefixes) > max_prefixes:
                return f"zu viele Namensräume (über {_num(max_prefixes)} Präfixe)"
    return _Scanner(low, max_attrs).run(max_tags)


class _Scanner:
    """Zustandsautomat für markup_problem über den klein geschriebenen Text, angelehnt an den
    HTML5-Tokenizer (libxml2 2.14). Jede Suche nach einem Ende (Kommentar, schließendes Tag,
    Anführungszeichen), die nichts findet, wird gemerkt: Weitere Suchen ab späteren Stellen finden
    dann auch nichts und entfallen. So bleibt es auch mit Hunderttausenden unbeendeten "<!--" linear."""

    def __init__(self, low: bytes, max_attrs: int):
        self.low, self.size, self.max_attrs = low, len(low), max_attrs
        self._missing: dict[bytes, int] = {}      # Suchbegriff -> ab hier gibt es ihn nicht mehr
        # Bis hierhin Inhalt eines Textelements (title, textarea ...): Darin beginnt nichts, was
        # übersprungen würde (kein Skript, kein Kommentar); Tags zählen trotzdem weiter.
        self._text_until = -1

    def _find(self, key: bytes, start: int, search) -> int:
        """Position des Treffers von search(start) oder -1; merkt sich Fehlschläge je `key`."""
        if start >= self._missing.get(key, self.size + 1):
            return -1
        found = search(start)
        if found < 0:
            self._missing[key] = start
        return found

    def _closing(self, name: bytes, start: int) -> int:
        """Position des schließenden Tags </name ab `start` oder -1."""
        closing = b"</" + name
        return self._find(closing, start, lambda s: self.low.find(closing, s))

    def run(self, max_tags: int) -> str | None:
        low, pos, tags = self.low, 0, 0
        while True:
            start = low.find(b"<", pos)
            if start < 0:
                return None
            tags += 1
            if tags > max_tags:
                return f"zu viele Tags (über {_num(max_tags)})"
            skipping = start >= self._text_until
            nxt = low[start + 1:start + 2]
            if nxt == b"!":
                if skipping and low.startswith(b"<!--", start):
                    # Ab start + 2 gesucht: "<!-->" und "<!--->" sind schon beendete Kommentare.
                    end = self._find(b"-->", start + 2, self._comment_end)
                    # Unbeendet: ab hier wieder als Markup lesen (der Parser könnte es auch tun).
                    pos = start + 4 if end < 0 else end
                else:
                    pos = start + 2       # <!DOCTYPE, <![CDATA[: Inhalt zählt weiter mit
                continue
            if nxt == b"/" and low[start + 2:start + 3].isalpha():
                end, attrs = self._tag(start + 1)     # auch Attribute an End-Tags zerlegt der Parser
            elif nxt.isalpha():
                end, attrs = self._tag(start)
            else:
                pos = start + 1           # <?xml, "a < b" im Text
                continue
            if attrs > self.max_attrs:
                return f"zu viele Attribute in einem Tag (über {_num(self.max_attrs)})"
            pos = end
            if nxt == b"/" or not skipping:
                continue
            name = _TAG_NAME_RE.match(low, start + 1).group(0)
            if name in _RAWTEXT:
                # Skript und Style: Rohtext bis zum schließenden Tag; fehlt es, zählt der Rest wieder.
                close = self._closing(name, end)
                if close >= 0:
                    pos = close
            elif name in _TEXT_ELEMENTS:
                # title, textarea, xmp ...: Inhalt ist Text, "<script" oder "<!--" darin beginnt
                # nichts. Weiter gezählt wird trotzdem (fehlt das Ende: bis zum Schluss).
                close = self._closing(name, end)
                self._text_until = self.size if close < 0 else close

    def _comment_end(self, start: int) -> int:
        m = _COMMENT_END_RE.search(self.low, start)
        return -1 if m is None else m.end()

    def _tag(self, start: int) -> tuple[int, int]:
        """(Position nach dem Tag, Zahl der Attribute) für das Tag bei `start` ("<" bzw. "/" vor dem
        Namen), wie der HTML5-Tokenizer: Ein Attributname beginnt mit jedem Zeichen außer Leerraum,
        "/" und ">" (auch mit "="); nach "=" und Leerraum öffnet ein Anführungszeichen nur als erstes
        Zeichen einen Wert (auch einfache: data-x='{"a": "b"}'), sonst reicht der Wert bis Leerraum
        oder ">". Bricht ab, sobald mehr als max_attrs gezählt sind."""
        low, size, attrs = self.low, self.size, 0
        i = _TAG_NAME_RE.match(low, start + 1).end()
        while True:
            i = _TAG_SPACE_RE.match(low, i).end()
            if i >= size:
                return size, attrs
            if low[i] == 0x3E:                    # ">"
                return i + 1, attrs
            attrs += 1
            if attrs > self.max_attrs:
                return i, attrs
            i = _ATTR_NAME_RE.match(low, i).end()
            j = _SPACE_RE.match(low, i).end()
            if j >= size or low[j] != 0x3D:       # kein "=": Attribut ohne Wert
                i = j
                continue
            i = _SPACE_RE.match(low, j + 1).end()
            if i < size and low[i] in (0x22, 0x27):   # " oder '
                quote = low[i:i + 1]
                close = self._find(quote, i + 1, lambda s, q=quote: low.find(q, s))
                if close < 0:
                    # Unbeendeter Wert: der Parser läse bis zum Schluss; hier zählt der Rest wieder.
                    return i + 1, attrs
                i = close + 1
            else:
                i = _UNQUOTED_RE.match(low, i).end()


_XMLNS_PREFIX_RE = re.compile(rb"xmlns:([^\s=>/\"']{1,100})")
_COMMENT_END_RE = re.compile(rb"--!?>")
_TAG_NAME_RE = re.compile(rb"[^ \t\n\r\f/>]*")
_TAG_SPACE_RE = re.compile(rb"[ \t\n\r\f/]*")
_SPACE_RE = re.compile(rb"[ \t\n\r\f]*")
_ATTR_NAME_RE = re.compile(rb"[^ \t\n\r\f/>][^ \t\n\r\f/>=]*")
_UNQUOTED_RE = re.compile(rb"[^ \t\n\r\f>]*")
_RAWTEXT = (b"script", b"style")
_TEXT_ELEMENTS = (b"title", b"textarea", b"xmp", b"iframe", b"noembed", b"noframes")


def _num(n: int) -> str:
    """Zahl mit schmalem geschütztem Leerzeichen als Tausendertrenner ("100 000")."""
    return f"{n:,}".replace(",", " ")


def _extract_job(extract_fn, page: bytes, url: str):
    """Läuft im Worker-Prozess: Vorprüfung der Seite, dann extract_fn (app.extract.extract, in Tests
    austauschbar; wird als Referenz mitgeschickt)."""
    problem = markup_problem(page, max_tags=MAX_PAGE_TAGS, max_attrs=MAX_TAG_ATTRS, max_lt=MAX_PAGE_LT)
    if problem:
        raise MarkupRejected(f"Seite abgelehnt: {problem}")
    return extract_fn(page, url)


def _feed_job(fn, *args):
    """Läuft im Worker-Prozess: Vorprüfung des Feeds bzw. der Sitemap (das bytes-Argument), dann fn
    (parse_feed, pick_child, parse_sitemap)."""
    raw = next(a for a in args if isinstance(a, bytes))
    problem = markup_problem(raw, max_tags=MAX_FEED_TAGS, max_attrs=MAX_TAG_ATTRS, max_lt=MAX_FEED_LT,
                             max_prefixes=MAX_FEED_PREFIXES)
    if problem:
        raise MarkupRejected(f"Feed abgelehnt: {problem}")
    return fn(*args)


def _portable(exc: BaseException) -> BaseException:
    """Die Exception selbst, wenn sie sich zum Elternprozess schicken lässt, sonst ein RuntimeError
    mit Typ und Text."""
    try:
        pickle.loads(pickle.dumps(exc))
        return exc
    except Exception:  # noqa: BLE001
        return RuntimeError(f"{type(exc).__name__}: {str(exc)[:500]}")


def _job_main(conn, fn, args: tuple, memory_limit: int, cpu_seconds: int) -> None:
    """Einstieg des Worker-Prozesses für genau einen Auftrag (vom Forkserver geforkt): Grenzen
    setzen, fn(*args) ausführen, (True, Ergebnis) bzw. (False, Exception) schicken und sofort enden
    (os._exit: kein Aufräumen großer Bäume, keine Finalizer)."""
    try:
        try:
            resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 5))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))    # SIGXCPU ohne Core-Datei
            msg = (True, fn(*args))
        except BaseException as e:  # noqa: BLE001  (auch MemoryError, SystemExit)
            msg = (False, _portable(e))
        try:
            conn.send(msg)
        except BaseException as e:  # noqa: BLE001  (Ergebnis nicht picklebar oder zu groß)
            conn.send((False, JobInfraError(f"Ergebnis nicht übertragbar ({type(e).__name__})")))
    finally:
        os._exit(0)


def _collect(conn, proc) -> tuple[Any, int | None]:
    """Im Thread: wartet auf die Nachricht des Worker-Prozesses (None, wenn er ohne endete) und auf
    sein Ende; gibt (Nachricht, Exit-Code) zurück und schließt Pipe und Prozess-Objekt."""
    try:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            msg = None
    finally:
        conn.close()
    proc.join()
    code = proc.exitcode
    proc.close()
    return msg, code


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
        # Leistungsschalter: Quelle (bzw. Host) -> Zeitpunkte von Zeitüberschreitungen und Abstürzen,
        # zählen ab Beginn des laufenden Sammellaufs (siehe _tripped).
        self._trouble: dict[str, list[float]] = {}
        self._cycle_started = -math.inf
        self._extracted_ids: list[int] = []         # zuletzt extrahierte Artikel (siehe run_once)
        self._stories_at = -math.inf                 # monotone Zeit des letzten Laufs der Gruppierung
        # Extraktionen auf Anforderung: wie viele laufen oder warten (Grenze PRIORITY_QUEUE_MAX) und
        # wann die letzte je Artikel endete (monotone Zeit, für RETRY_MIN_INTERVAL).
        self._ondemand_count = 0
        self._ondemand_done: dict[int, float] = {}
        self._client: httpx.AsyncClient | None = None
        self._wake = asyncio.Event()
        # Extraktion (trafilatura/lxml) und Feed-Parsing in eigenen Prozessen, einer je Auftrag (siehe
        # _run_job): Ein Thread ließe sich bei einer hängenden Seite nicht abbrechen, und ein
        # hängender oder abgestürzter Auftrag reißt so keinen Nachbarn mit (in einem Prozess-Pool
        # machte ein getöteter Worker den ganzen Pool unbrauchbar). Die Semaphoren begrenzen die
        # gleichzeitigen Prozesse; das Zeitlimit zählt erst ab dem eigenen Platz.
        self._job_sem = asyncio.Semaphore(EXTRACT_WORKERS)
        self._feed_sem = asyncio.Semaphore(FEED_PARSE_WORKERS)
        self._pids: set[int] = set()                 # PIDs laufender Worker-Prozesse (für close)
        self._threads: concurrent.futures.ThreadPoolExecutor | None = None   # siehe _job_threads
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
            transport=_guarded_transport(),
        )
        self._mp_context()

    async def close(self) -> None:
        # Laufende Extraktionen (per shield vom Aufrufer entkoppelt) beim Herunterfahren abbrechen;
        # ihre Worker-Prozesse beendet _run_job dabei selbst.
        pending = list(self._inflight.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if self._client:
            await self._client.aclose()
        for pid in list(self._pids):  # z. B. Feed-Parsing eines abgebrochenen Laufs
            _kill(pid)
        if self._threads is not None:
            self._threads.shutdown(wait=False)
            self._threads = None

    def _job_threads(self) -> concurrent.futures.ThreadPoolExecutor:
        """Threads für Start und Einsammeln der Worker-Prozesse: je gleichzeitigem Auftrag einer
        (EXTRACT_WORKERS + FEED_PARSE_WORKERS), dazu Reserve für das Einsammeln abgebrochener."""
        if self._threads is None:
            self._threads = concurrent.futures.ThreadPoolExecutor(
                max_workers=EXTRACT_WORKERS + FEED_PARSE_WORKERS + 2, thread_name_prefix="kiosk-job")
        return self._threads

    @staticmethod
    def _mp_context():
        """Startmethode der Worker-Prozesse: bewusst "forkserver" statt des Linux-Standards "fork".
        uvicorn hat zu diesem Zeitpunkt schon Threads (asyncio.to_thread, httpx), und fork() in einem
        Prozess mit Threads kann im Kind gehaltene Locks (Logging, malloc) einfrieren. Der Forkserver
        wird einmal sauber per exec gestartet (beim ersten Auftrag), die Worker forken von ihm. Sie
        führen nur Modulfunktionen (extract, parse_feed, pick_child, parse_sitemap) mit bytes, str,
        Source und Hostnamen aus, fassen weder Datenbank noch Event-Loop an und laufen im selben
        Container (pids_limit, mem_limit)."""
        ctx = multiprocessing.get_context("forkserver")
        # Der Forkserver lädt die Module der Aufträge (trafilatura, lxml, feedparser) einmal vorab;
        # jeder Worker forkt sie fertig geladen, statt sie je Auftrag zu importieren. Wirkt nur,
        # bevor der Forkserver startet; Importfehler ignoriert multiprocessing dabei.
        ctx.set_forkserver_preload(["app.fetcher"])
        return ctx

    async def _run_extract(self, page: bytes, url: str, skip=None):
        """Vorprüfung und extract() in einem eigenen Worker-Prozess, höchstens EXTRACT_TIMEOUT
        Sekunden reine Laufzeit."""
        # extract wird zur Laufzeit aus dem Modul gelesen (in Tests austauschbar); an den Worker
        # geht nur eine Referenz auf die Modulfunktion plus bytes und str (picklebar).
        return await self._run_job(_extract_job, extract, page, url, timeout=EXTRACT_TIMEOUT,
                                   what="Extraktion", slots=self._job_sem, skip=skip)

    async def _run_job(self, fn, *args, timeout: float, what: str, slots: asyncio.Semaphore, skip=None):
        """Führt fn(*args) in einem eigenen, frisch geforkten Worker-Prozess aus (ein Prozess je
        Auftrag), höchstens `timeout` Sekunden ab dem freien Platz in `slots`.

        Zeitüberschreitung: nur dieser Prozess wird beendet (ExtractTimeout). Endet der Prozess ohne
        Ergebnis (Signal): JobCrashed; MemoryError (JOB_MEMORY_LIMIT): JobOutOfMemory; andere
        Exceptions des Auftrags kommen unverändert an. Wird die aufrufende Aufgabe abgebrochen,
        endet auch der Prozess. `what` benennt den Auftrag in den Fehlermeldungen. `skip`: wird mit
        dem freien Platz noch einmal gefragt (Leistungsschalter); True: JobSkipped, kein Prozess.
        """
        async with slots:
            if skip is not None and skip():
                raise JobSkipped(what)
            ctx = self._mp_context()
            recv, send = ctx.Pipe(duplex=False)
            proc = ctx.Process(target=_job_main, daemon=True,
                               args=(send, fn, args, JOB_MEMORY_LIMIT, math.ceil(timeout) + JOB_CPU_GRACE))
            loop = asyncio.get_running_loop()
            # Start und Einsammeln in eigenen Threads (_job_threads), nicht im Standard-Executor, den
            # getaddrinfo, scrypt und die Gruppierung brauchen: _collect wartet bis zum Zeitlimit.
            # Der erste Auftrag startet den Forkserver (Import von trafilatura, ca. 1-2 s).
            # Abgeschirmt: Ein Abbruch währenddessen wartet den Start ab, beendet den Prozess und
            # sammelt ihn im Hintergrund ein (join, close).
            starting = loop.run_in_executor(self._job_threads(), proc.start)
            try:
                await asyncio.shield(starting)
            except BaseException as e:
                await asyncio.gather(starting, return_exceptions=True)
                send.close()
                if proc.pid is not None:
                    _kill(proc.pid)
                    loop.run_in_executor(self._job_threads(), _collect, recv, proc)
                else:
                    recv.close()
                if isinstance(e, Exception):
                    raise JobStartFailed(f"{what}: Worker-Prozess ließ sich nicht starten "
                                         f"({type(e).__name__}: {e})") from e
                raise
            send.close()  # sonst sähe _collect nach dem Ende des Prozesses kein EOF
            pid = proc.pid
            self._pids.add(pid)
            waiter = loop.run_in_executor(self._job_threads(), _collect, recv, proc)
            waiter.add_done_callback(lambda _w: self._pids.discard(pid))
            try:
                msg, code = await asyncio.wait_for(asyncio.shield(waiter), timeout=timeout)
            except asyncio.TimeoutError:
                _kill(pid, waiter)
                await asyncio.gather(waiter, return_exceptions=True)
                raise ExtractTimeout(f"{what}: Zeitüberschreitung") from None
            except asyncio.CancelledError:
                _kill(pid, waiter)
                raise
        if msg is None:
            raise JobCrashed(f"{what}: abgebrochen (Worker-Prozess beendet, Code {code})")
        ok, payload = msg
        if ok:
            return payload
        if isinstance(payload, MemoryError):
            raise JobOutOfMemory(f"{what}: zu viel Speicher (über {_num(JOB_MEMORY_LIMIT // 2**20)} MB)")
        if not isinstance(payload, Exception):
            # SystemExit, KeyboardInterrupt o. Ä. aus dem Worker nie in der Event-Loop weiterwerfen.
            raise JobInfraError(f"{what}: Worker-Prozess meldete {type(payload).__name__}")
        raise payload

    # ---- Leistungsschalter je Quelle ---------------------------------------

    @staticmethod
    def _trouble_key(url: str, source: str | None) -> str:
        if source:
            return source
        try:
            return (urlparse(url).hostname or "").lower()
        except ValueError:          # kaputte URL: scheitert gleich beim Abruf
            return ""

    def _note_trouble(self, key: str) -> None:
        self._trouble.setdefault(key, []).append(time.monotonic())

    def _tripped(self, key: str) -> bool:
        """True, wenn diese Quelle im laufenden Sammellauf (höchstens SOURCE_TROUBLE_TTL zurück) schon
        SOURCE_TROUBLE_LIMIT Zeitüberschreitungen bzw. Abstürze hatte."""
        since = max(self._cycle_started, time.monotonic() - SOURCE_TROUBLE_TTL)
        return sum(t >= since for t in self._trouble.get(key, ())) >= SOURCE_TROUBLE_LIMIT

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
                    await self._purge()  # aufräumen auch nach einem Fehler davor
                    raise
                purged = await self._purge()
                self.state.update(last_new=new, last_extracted=extracted, last_purged=purged)
                self.state["runs"] += 1
                log.info("Lauf fertig: %d neu, %d extrahiert, %d gelöscht", new, extracted, purged)
            finally:
                self.state.update(running=False, last_finished=utcnow())

    async def _purge(self) -> int:
        """Alte Artikel löschen, blockweise (Database.purge_steps): Zwischen den Blöcken kommen
        API-Anfragen dran, statt sekundenlang auf die eine Transaktion zu warten."""
        purged = 0
        for n in self.db.purge_steps(self.settings.retention_days):
            purged += n
            await asyncio.sleep(0)
        return purged

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
                    # Eigene Fehler tragen schon eine deutsche Meldung (erscheint so in der Quellenliste),
                    # bei fremden hilft der Typ bei der Diagnose.
                    self.db.record_fetch(src.id, ok=False, error=error_text(e))
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
                    # Schutz gegen DNS-Rebinding: Die Verbindung geht schon an die beim Aufbau geprüfte
                    # Adresse (GuardedBackend, _guarded_transport). Zusätzlich die tatsächliche
                    # Gegenstelle prüfen (zweite Ebene, z. B. falls das Backend fehlt). Ohne
                    # Netzwerk-Stream (MockTransport) keine Prüfung.
                    peer = _server_addr(r)
                    if server_addr_blocked(peer):
                        raise BlockedURL(f"Verbindung zu nicht erlaubter Adresse: {peer!r}")
                    location = r.headers.get("location")
                    if r.status_code in REDIRECT_CODES and location:
                        current = urljoin(str(r.url), location.strip())
                        continue
                    declared = r.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        raise TooLarge(f"Antwort zu groß ({_num(int(declared))} Bytes, Grenze {_num(max_bytes)})")
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
        # Im Worker-Prozess mit Vorprüfung und Zeitlimit: Ein feindlicher Feed kostet dort Speicher und
        # Zeit, nicht in der App (deren Speicher bliebe fragmentiert). Fehler landen in _fetch_all_feeds.
        rows = await self._parse_job(parse_feed, src, r.content, what="Feed-Parsing")
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
            child = await self._parse_job(pick_child, r.content, hosts, what="Sitemap-Parsing")
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
        rows = await self._parse_job(parse_sitemap, src, r.content, FEED_MAX_ENTRIES, what="Sitemap-Parsing")
        # Leer ist nur dann kein Fehler, wenn die URL-Liste gar keine Einträge hat (neue Quartals-
        # Sitemap kurz nach dem Wechsel) oder include_paths welche verworfen hat. Blieb aus anderen
        # Gründen nichts übrig (z. B. lauter unbrauchbare URLs), ist es ein Fehler wie beim Feed.
        return self._store_rows(src, rows, (None, None),
                                empty_ok=getattr(rows, "total", 1) == 0 or getattr(rows, "filtered_out", 0) > 0)

    async def _parse_job(self, fn, *args, what: str):
        """Feed bzw. Sitemap im Worker-Prozess parsen (mit Vorprüfung, _feed_job), eigener Platz."""
        return await self._run_job(_feed_job, fn, *args, timeout=FEED_PARSE_TIMEOUT, what=what,
                                   slots=self._feed_sem)

    def _store_rows(self, src: Source, rows: list[dict[str, Any]],
                    validators: tuple[str | None, str | None], *, empty_ok: bool = False) -> int:
        """Legt die Einträge eines Abrufs an und speichert das Ergebnis samt Validatoren. Ohne
        Einträge ein Fehler, außer mit `empty_ok` (dann ein Abruf ohne neue Artikel)."""
        if not rows and not empty_ok:
            self.db.record_fetch(src.id, ok=False, error="Feed enthält keine Einträge")
            return 0
        # Uralte Einträge (manche Feeds führen Evergreen-Artikel) gar nicht erst anlegen,
        # sonst würden sie bei jedem Lauf neu eingefügt, extrahiert und wieder gelöscht.
        cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=self.settings.retention_days))
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
        # Neuer Lauf: Der Leistungsschalter beginnt für alle Quellen von vorn.
        self._trouble.clear()
        self._cycle_started = time.monotonic()
        while done < EXTRACT_MAX_PER_CYCLE:
            batch = self.db.pending_articles(min(EXTRACT_BATCH, EXTRACT_MAX_PER_CYCLE - done), before=after)
            if not batch:
                break
            after = (batch[-1]["published_at"], batch[-1]["id"])
            results = await asyncio.gather(
                *(self.extract_article(r["id"], r["url"], r["source_id"]) for r in batch), return_exceptions=True,
            )
            # Gezählt (Budget je Lauf, last_extracted) wird nur, was wirklich versucht wurde: nicht,
            # was der Leistungsschalter übersprang oder was schon lief bzw. nicht mehr fällig war.
            for row, res in zip(batch, results):
                if isinstance(res, Exception):
                    log.error("Extraktion %s (%s) unerwartet gescheitert: %r", row["id"], row["url"], res)
                    done += 1
                elif res is not False:
                    self._extracted_ids.append(row["id"])
                    done += 1
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

    async def extract_article(self, article_id: int, url: str, source: str | None = None) -> bool:
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
                return False  # läuft schon (Abruf auf Anforderung), dessen Ergebnis genügt
            # Kann seit dem Lesen der Liste per API erledigt oder neu eingeplant worden sein.
            if not self.db.is_due_pending(article_id):
                return False
            task = self._spawn(article_id, self._extract(article_id, url, source))
            return await asyncio.shield(task)

    def is_extracting(self, article_id: int) -> bool:
        """True, solange für diese ID eine Extraktion läuft."""
        return article_id in self._inflight

    def start_extraction(self, article_id: int, url: str, *, retry: bool = False,
                         source: str | None = None) -> bool:
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
        task = self._spawn(article_id, self._extract_priority(article_id, url, source))

        def _finished(_t: asyncio.Task) -> None:
            self._ondemand_count -= 1
            done = time.monotonic()
            if len(self._ondemand_done) >= 1000:  # nur die jüngsten Einträge zählen noch
                for aid in [a for a, t in self._ondemand_done.items() if done - t >= RETRY_MIN_INTERVAL]:
                    del self._ondemand_done[aid]
            self._ondemand_done[article_id] = done

        task.add_done_callback(_finished)
        return True

    async def _extract_priority(self, article_id: int, url: str, source: str | None) -> None:
        async with self._priority_sem:
            await self._extract(article_id, url, source, on_demand=True)

    def _fail(self, article_id: int, error: str) -> None:
        # Vorhandener Text bleibt erhalten (siehe Database.store_extraction).
        self.db.store_extraction(article_id, status="failed", body_html=None, body_len=0,
                                 paywalled=False, error=error)

    def _retry(self, article_id: int, url: str, error: str, retry_after: float | None = None) -> None:
        status = self.db.schedule_retry(article_id, error, retry_after)
        log.info("Extraktion %s (%s): %s -> %s", article_id, url, error, status)

    async def _extract(self, article_id: int, url: str, source: str | None = None, *,
                       on_demand: bool = False) -> bool:
        """Ein Extraktionsversuch. Geht dabei unerwartet etwas schief (auch in der Buchführung,
        z. B. beim Speichern eines Fehlers), zählt das als vorübergehender Fehlschlag: Sonst bliebe
        der Artikel 'pending' und fällig und würde in jedem Lauf erneut geholt. Gibt zurück, ob
        wirklich ein Versuch stattfand (False: vom Leistungsschalter übersprungen)."""
        try:
            return await self._extract_once(article_id, url, source, on_demand=on_demand) is not False
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
        return True

    def _skip_tripped(self, article_id: int, url: str, key: str) -> bool:
        """Leistungsschalter der Quelle ausgelöst: Artikel in diesem Lauf nicht versuchen. Er bleibt
        fällig, kein Versuch zählt, nichts wird gespeichert."""
        if not self._tripped(key):
            return False
        log.info("Extraktion %s (%s): übersprungen, %s hatte in diesem Lauf %d Zeitüberschreitungen "
                 "bzw. Abstürze", article_id, url, key, SOURCE_TROUBLE_LIMIT)
        return True

    async def _extract_once(self, article_id: int, url: str, source: str | None = None, *,
                            on_demand: bool = False) -> bool | None:
        """Ein Versuch. Den Leistungsschalter der Quelle prüft nur der Hintergrund (vor dem Abruf, vor
        dem Parsen und noch einmal mit dem freien Worker-Platz). Wer einen Artikel öffnet oder "Erneut
        versuchen" drückt (`on_demand`), bekommt einen echten Versuch statt eines stillen Nichts, nach
        dem die Oberfläche 30 s wartet; begrenzt ist das durch PRIORITY_CONCURRENCY,
        PRIORITY_QUEUE_MAX und RETRY_MIN_INTERVAL. Seine Zeitüberschreitungen zählen trotzdem für
        den Schalter."""
        key = self._trouble_key(url, source)
        tripped = (lambda: False) if on_demand else (lambda: self._skip_tripped(article_id, url, key))
        if tripped():
            return False
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
            self._retry(article_id, url, error_text(e))
            return
        except Exception as e:  # noqa: BLE001
            log.warning("Extraktion %s (%s): %s", article_id, url, e)
            self._retry(article_id, url, error_text(e))
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
        # Der Abruf kann gedauert haben: Hat die Quelle inzwischen ausgelöst, gar nicht erst parsen.
        if tripped():
            return False
        try:
            result = await self._run_extract(r.content, r.url, skip=tripped)
        except JobSkipped:
            return False
        except JobStartFailed as e:
            # Kein Prozess (z. B. pids_limit): nicht die Schuld des Artikels, ohne Zählung neu einplanen.
            self.db.requeue(article_id, str(e))
            log.warning("Extraktion %s (%s): %s", article_id, url, e)
            return True
        except JobInfraError as e:
            self._retry(article_id, url, str(e))
            return True
        except MarkupRejected as e:
            # Vorprüfung: Die Seite wird beim nächsten Versuch nicht harmloser (wie TooLarge).
            self._fail(article_id, str(e))
            return
        except JobOutOfMemory as e:
            self._note_trouble(key)
            self._fail(article_id, str(e))
            return
        except (ExtractTimeout, JobCrashed) as e:
            # Nur dieser Auftrag ist betroffen (eigener Prozess); zählt als Versuch und für den
            # Leistungsschalter der Quelle.
            self._note_trouble(key)
            self._retry(article_id, url, str(e))
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
            raise TooLarge(f"Antwort zu groß (über {_num(max_bytes)} Bytes)")
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
            raise TooLarge(f"Antwort zu groß (über {_num(max_bytes)} Bytes)")
        chunks.append(data)

    async for chunk in r.aiter_raw():
        raw_total += len(chunk)
        if raw_total > max_bytes:
            raise TooLarge(f"Antwort zu groß (über {_num(max_bytes)} Bytes)")
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


def _guarded_transport() -> httpx.AsyncHTTPTransport:
    """Transport mit GuardedBackend (netguard): Die Verbindung geht an genau die beim Aufbau geprüfte
    Adresse (Schutz vor DNS-Rebinding). httpx 0.27 reicht kein network_backend durch, deshalb wird es
    im httpcore-Pool des Transports gesetzt (vor der ersten Verbindung)."""
    transport = httpx.AsyncHTTPTransport(limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                                         trust_env=False)
    pool = transport._pool
    if not hasattr(pool, "_network_backend"):   # andere httpcore-Version: lieber laut scheitern
        raise RuntimeError("httpcore ohne _network_backend: Schutz vor DNS-Rebinding nicht einsetzbar")
    pool._network_backend = GuardedBackend()
    return transport


def _kill(pid: int, waiter: asyncio.Future | None = None) -> None:
    """SIGKILL an einen Worker-Prozess, solange _collect ihn noch nicht eingesammelt hat (danach
    könnte die PID schon einem anderen Prozess gehören)."""
    if waiter is not None and waiter.done():
        return
    try:
        os.kill(pid, signal.SIGKILL)   # eigener Kindprozess des Forkservers, gleiche uid
    except (ProcessLookupError, PermissionError):
        pass


def _server_addr(response: httpx.Response) -> Any:
    """Adresse der Gegenstelle aus dem httpcore-Netzwerk-Stream, None wenn nicht verfügbar."""
    stream = response.extensions.get("network_stream")
    if stream is None or not hasattr(stream, "get_extra_info"):
        return None
    try:
        return stream.get_extra_info("server_addr")
    except Exception:  # noqa: BLE001
        return None
