"""Google-News-Sitemaps (sitemaps.org) als Quelle für Zeitungen ohne brauchbaren Feed.

Erkannt wird am Inhalt (Wurzelelement urlset bzw. sitemapindex im sitemaps.org-Namensraum), sources.yaml
nennt die Sitemap einfach als feed. Geparst wird mit expat direkt statt mit feedparser oder
xml.etree: So lassen sich DOCTYPE und Entity-Deklarationen schon beim ersten Auftreten ablehnen
(dasselbe Vorgehen wie defusedxml, ohne zusätzliche Abhängigkeit). Ohne DOCTYPE kann ein Dokument
keine Entities definieren; expat kennt dann nur die fünf vordefinierten und Zeichenreferenzen,
jede andere Referenz ist ein Parse-Fehler. Externe Entities und Entity-Expansion sind damit
ausgeschlossen. parse_sitemap und pick_child laufen wie parse_feed im Worker-Prozess mit Zeitlimit
(Fetcher._fetch_sitemap); sitemap_kind liest nur den Anfang des Dokuments bis zum Wurzelelement.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from xml.parsers import expat

from .db import utcnow
from .extract import safe_http_url, strip_html
from .netguard import DEFAULT_PORTS
from .sources import Source

SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
NEWS_NS = "http://www.google.com/schemas/sitemap-news/0.9"
IMAGE_NS = "http://www.google.com/schemas/sitemap-image/1.1"
# expat meldet Namen als "Namensraum-URI lokaler-Name"; Präfixe (news:, n:) spielen keine Rolle.
_SEP = " "
URLSET = f"{SITEMAP_NS}{_SEP}urlset"
SITEMAPINDEX = f"{SITEMAP_NS}{_SEP}sitemapindex"
# Nur so viele Bytes vom Anfang liest sitemap_kind (im App-Prozess), um das Wurzelelement zu finden.
SNIFF_BYTES = 64 * 1024
# Mehr Einträge erlaubt das Sitemap-Protokoll nicht (Google News: 1000); weitere werden ignoriert.
MAX_ITEMS = 50_000
MAX_VALUE = 2000    # Zeichen je gelesenem Wert (loc, Titel ...)


def _q(ns: str, name: str) -> str:
    return f"{ns}{_SEP}{name}"


# Gelesene Kindelemente je Eintrag: Pfad unterhalb von <url> bzw. <sitemap> -> Schlüssel.
_URL_FIELDS = {
    (_q(SITEMAP_NS, "loc"),): "loc",
    (_q(SITEMAP_NS, "lastmod"),): "lastmod",
    (_q(NEWS_NS, "news"), _q(NEWS_NS, "publication_date")): "published",
    (_q(NEWS_NS, "news"), _q(NEWS_NS, "title")): "title",
    (_q(IMAGE_NS, "image"), _q(IMAGE_NS, "loc")): "image",
}
_INDEX_FIELDS = {
    (_q(SITEMAP_NS, "loc"),): "loc",
    (_q(SITEMAP_NS, "lastmod"),): "lastmod",
}


class ParsedRows(list):
    """Zeilen aus parse_feed bzw. parse_sitemap (picklebar samt Attributen, kommen so aus dem
    Worker-Prozess zurück): `filtered_out` zählt die Einträge, die include_paths verworfen hat,
    `total` alle Einträge des Dokuments vor jeder Prüfung."""
    filtered_out = 0
    total = 0


class SitemapError(ValueError):
    """Sitemap abgelehnt (DOCTYPE/Entities) oder kein gültiges XML."""


class _Stop(Exception):
    """Wurzelelement gefunden: sitemap_kind hört auf zu lesen."""


def _reject_doctype(*_args) -> None:
    raise SitemapError("Sitemap mit DOCTYPE abgelehnt")


def _reject_entity(*_args) -> None:
    raise SitemapError("Sitemap mit Entity-Deklaration abgelehnt")


def _parser() -> Any:
    p = expat.ParserCreate(namespace_separator=_SEP)
    p.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    p.StartDoctypeDeclHandler = _reject_doctype
    p.EntityDeclHandler = _reject_entity
    p.UnparsedEntityDeclHandler = _reject_entity
    p.ExternalEntityRefHandler = _reject_entity
    p.buffer_text = True
    return p


def sitemap_kind(raw: bytes) -> str | None:
    """"urlset" oder "sitemapindex", wenn das Wurzelelement eine Sitemap im sitemaps.org-Namensraum
    ist; sonst None (dann ist es ein Feed für feedparser, auch bei DOCTYPE oder kaputtem XML)."""
    p = _parser()
    root: list[str] = []

    def start(name, _attrs):
        root.append(name)
        raise _Stop

    p.StartElementHandler = start
    try:
        p.Parse(raw[:SNIFF_BYTES], False)
    except _Stop:
        pass
    except Exception:  # noqa: BLE001
        # Das Erkennen darf nie einen Feed scheitern lassen: kaputtes XML, DOCTYPE, aber auch eine
        # unbekannte Kodierung (LookupError, z. B. encoding="x-mac-roman") gehen an feedparser.
        return None
    if root == [URLSET]:
        return "urlset"
    if root == [SITEMAPINDEX]:
        return "sitemapindex"
    return None


class _Reader:
    """Sammelt die Einträge (item) direkt unter der Wurzel (root), je Eintrag den ersten Wert der
    Kindelemente aus `fields`; alles andere wird überlesen. Höchstens MAX_ITEMS Einträge."""

    def __init__(self, root: str, item: str, fields: dict[tuple[str, ...], str]):
        self.root, self.item, self.fields = root, item, fields
        # Tiefer als der längste Feldpfad wird nicht nachgeschlagen: Ein Tupel aus dem ganzen Stapel
        # je Start-Tag wäre bei tief verschachtelten Elementen quadratisch.
        self._max_depth = 2 + max(len(k) for k in fields)
        self.items: list[dict[str, str]] = []
        self._stack: list[str] = []
        self._cur: dict[str, str] | None = None
        self._text: list[str] = []
        self._key: str | None = None

    def start(self, name, _attrs):
        if not self._stack and name != self.root:
            raise SitemapError("Wurzelelement ist keine erwartete Sitemap")
        self._stack.append(name)
        if len(self._stack) == 2 and name == self.item and len(self.items) < MAX_ITEMS:
            self._cur = {}
        self._text = []
        self._key = (self.fields.get(tuple(self._stack[2:]))
                     if self._cur is not None and len(self._stack) <= self._max_depth else None)

    def end(self, _name):
        if self._cur is not None and self._key and self._key not in self._cur:
            self._cur[self._key] = "".join(self._text).strip()[:MAX_VALUE]
        if len(self._stack) == 2 and self._cur is not None:
            self.items.append(self._cur)
            self._cur = None
        self._stack.pop()
        self._text, self._key = [], None

    def data(self, text):
        if self._key:
            self._text.append(text)


def _read(raw: bytes, root: str, item: str, fields: dict[tuple[str, ...], str]) -> list[dict[str, str]]:
    reader = _Reader(root, item, fields)
    p = _parser()
    p.StartElementHandler = reader.start
    p.EndElementHandler = reader.end
    p.CharacterDataHandler = reader.data
    try:
        p.Parse(raw, True)
    except (expat.ExpatError, LookupError) as e:   # LookupError: unbekannte Kodierung
        raise SitemapError(f"Sitemap ungültig: {e}") from None
    return reader.items


_YEAR_MONTH_RE = re.compile(r"\d{4}(?:-\d{2})?")


def w3c_timestamp(raw: str | None) -> int | None:
    """Unix-Sekunden aus einem W3C-Datum (YYYY, YYYY-MM, YYYY-MM-DD, mit Uhrzeit und Zeitzone);
    ohne Zeitzone gilt UTC. None, wenn leer oder unlesbar."""
    s = (raw or "").strip()
    if not s or len(s) > 64:
        return None
    if _YEAR_MONTH_RE.fullmatch(s):
        s = (s + "-01-01")[:10]
    if s[-1] in "Zz":
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts = math.floor(dt.timestamp())
        datetime.fromtimestamp(ts, tz=timezone.utc)  # außerhalb des darstellbaren Bereichs?
    except (OverflowError, ValueError, OSError):
        return None
    return ts


def url_host(url: str) -> str:
    """Hostname klein und ohne abschließenden Punkt, "" wenn keiner."""
    try:
        return (urlparse(url).hostname or "").rstrip(".").lower()
    except ValueError:
        return ""


def child_url(raw: str | None, hosts: set[str] | frozenset[str]) -> str | None:
    """URL einer Sitemap aus einem Index, wenn sie absolut ist, http(s) auf dem Standard-Port nutzt
    und auf einem der Hosts des Index liegt (das Sitemap-Protokoll erlaubt nur Sitemaps derselben
    Website); sonst None. Abgerufen wird sie danach wie jeder Feed (netguard, Größenlimit)."""
    url = safe_http_url(raw)
    if not url:
        return None
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return None
    if port is not None and port != DEFAULT_PORTS[parsed.scheme]:
        return None
    return url if url_host(url) in hosts else None


def pick_child(raw: bytes, hosts: set[str] | frozenset[str]) -> str | None:
    """Die Sitemap eines Index mit dem jüngsten lastmod, nur unter den nach child_url zulässigen;
    Sitemaps mit lesbarem lastmod vor denen ohne. Bei gleichem oder fehlendem lastmod gewinnt die
    lexikographisch größte URL: Verlage nummerieren die Dateien fortlaufend (GEA: 4-2026-3-0001.xml
    für das dritte Quartal). Am Quartalswechsel (1. Oktober) erscheint 4-2026-4-0001.xml mit dem
    jüngsten lastmod und wird ab dann genommen, auch solange sie noch leer ist (Lauf ohne Fehler,
    0 neue Artikel); Einträge, die das alte Quartal nach unserem letzten Abruf noch bekam, fehlen
    dann (höchstens ein Abrufintervall). Läuft im Worker-Prozess."""
    best: tuple[bool, int, str] | None = None
    for it in _read(raw, SITEMAPINDEX, _q(SITEMAP_NS, "sitemap"), _INDEX_FIELDS):
        url = child_url(it.get("loc"), hosts)
        if url is None:
            continue
        ts = w3c_timestamp(it.get("lastmod"))
        key = (ts is not None, ts or 0, url)
        if best is None or key > best:
            best = key
    return best[2] if best else None


def parse_sitemap(source: Source, raw: bytes, max_entries: int) -> ParsedRows:
    """Einträge einer (News-)Sitemap als Zeilen für upsert_articles, in derselben Form wie
    parse_feed: neueste zuerst (publication_date, sonst lastmod; ohne Datum ans Ende), höchstens
    max_entries, nur URLs nach source.include_paths; GUID ist die URL bzw. nach source.guid_pattern
    ein Teil davon. Läuft im Worker-Prozess."""
    now = utcnow()
    found: list[tuple[int | None, str, dict[str, str]]] = []
    items = _read(raw, URLSET, _q(SITEMAP_NS, "url"), _URL_FIELDS)
    rows = ParsedRows()
    rows.total = len(items)
    for it in items:
        url = safe_http_url(it.get("loc"))   # höchstens 1000 Zeichen wie in parse_feed
        if not url:
            continue
        if not source.wants_url(url):
            rows.filtered_out += 1
            continue
        ts = w3c_timestamp(it.get("published"))
        if ts is None:
            ts = w3c_timestamp(it.get("lastmod"))
        found.append((ts, url, it))
    found.sort(key=lambda f: math.inf if f[0] is None else -f[0])   # stabil: sonst Reihenfolge der Sitemap
    for ts, url, it in found[:max_entries]:
        published = now if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        rows.append({
            "source_id": source.id,
            "guid": source.guid_for(url, url)[:500],
            "url": url,
            "title": strip_html(it.get("title") or "", limit=300) or "(ohne Titel)",
            "summary": "",
            "author": None,
            "image_url": safe_http_url(it.get("image")),
            # Einträge mit Datum in der Zukunft würden sonst dauerhaft oben kleben.
            "published_at": min(published, now),
            "fetched_at": now,
        })
    return rows
