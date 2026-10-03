"""Volltext-Extraktion aus Artikelseiten und Bereinigung des HTML."""
from __future__ import annotations

import copy
import html
import json
import logging
import math
import os
import re
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import trafilatura
from trafilatura.utils import load_html

from .config import DEFAULT_TZ

log = logging.getLogger("kiosk.extract")

# Schema.org-Markierung, die viele Verlage (Spiegel, NYT, Zeit ...) für Bezahlartikel setzen, als JSON-LD
# ("isAccessibleForFree": false) oder als Attribut. Es zählt nur das ERSTE Vorkommen, die Angabe zum
# Artikel der Seite: heise listet weiter unten empfohlene Artikel mit eigenen Angaben (nach 200 000
# Zeichen). Gesucht wird in der ganzen Seite (der Abruf liest höchstens 5 MB, fetcher.ARTICLE_MAX_BYTES):
# Le Monde setzt das JSON-LD erst nach rund 240 000 Zeichen. Nicht als Paywall gilt der Wert als URL
# ("http:\/\/schema.org\/False"): Der Christian Science Monitor setzt ihn auf jede Seite, auch auf frei
# lesbare mit vollständigem Text. Handelsblatt (H+) braucht keine eigene Regel: Bezahlartikel tragen
# "isAccessibleForFree":"False"; das Element <app-paywall> steht versteckt auch in freien Artikeln.
# Dazu Markierungen einzelner Verlage ohne Schema.org-Angabe: GEA+ (Reutlinger General-Anzeiger) ersetzt
# den Bezahltext durch einen unscharf dargestellten Absatz <p class=nfy-text-wrapper-blur
# aria-label="Unkenntlich gemachter Text">Lorem ipsum …</p> (bei rund 50 KB großen Seiten nach etwa
# 48 000 Zeichen). Alle Suchen sind linear (feste Wörter, danach nur begrenzte bzw. einmal durchlaufene
# Zeichenklassen).
_ACCESS_RE = re.compile(r"""isaccessibleforfree["']?\s*[:=]\s*["']?([a-z]*)""", re.I)
PAYWALL_RE = re.compile(r"""aria-label\s*=\s*["']?Unkenntlich gemachter Text""", re.I)
# Nur als Klasse, nicht z. B. in CSS. Der Abstand ist begrenzt: Ohne Grenze wäre die Suche bei vielen
# "class=" ohne Anführungszeichen quadratisch; mit Grenze ist sie linear, aber mit großem Faktor, daher
# nur im Anfang der Seite (GEA-Seiten sind rund 50 KB groß).
_BLUR_CLASS_RE = re.compile(r"""class\s*=\s*["']?[^"'>]{0,200}?\bnfy-text-wrapper-blur\b""", re.I)
PAYWALL_SCAN_CHARS = 6_000_000
BLUR_SCAN_CHARS = 200_000
# Hinweis im Artikeltext statt einer Markierung (Le Monde: „You have 81.65% of this article left to
# read. The rest is for subscribers only.“); der Absatz fällt als Müll weg (_PAYWALL_NOTE_RE).
_PAYWALL_TEXT_RE = re.compile(r"left to read\.?\s*The rest is for subscribers only", re.I)
MIN_FULL_CHARS = 600
# Obergrenzen für den gespeicherten Text: So viele Zeichen bereinigtes HTML (danach endet der
# Text am nächsten Element der obersten Ebene, Status "partial") und so viele Bilder (weitere
# fallen weg). Echte Artikel liegen weit darunter; eine feindliche Seite soll weder Datenbank
# noch Leseansicht (Bildvergleich im Browser) überfordern.
MAX_BODY_HTML = 500_000
MAX_IMAGES = 100

ALLOWED_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote",
    "strong", "em", "b", "i", "u", "s", "a", "img", "table", "thead", "tbody", "tr", "td", "th",
    "br", "hr", "code", "pre", "figure", "figcaption", "sub", "sup", "cite", "q", "dl", "dt", "dd",
}
VOID_TAGS = {"br", "hr", "img"}
DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "form", "svg", "noscript", "head", "title"}
# Tags, die keinen Wortumbruch im reinen Text erzeugen; alle anderen trennen Wörter.
INLINE_TAGS = {"a", "strong", "em", "b", "i", "u", "s", "code", "sub", "sup", "cite", "q", "span", "abbr", "mark", "small"}
ALLOWED_ATTRS = {
    "a": {"href"},
    "img": {"src", "alt"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}


@dataclass
class Extraction:
    status: str                 # full | partial | empty
    body_html: str | None
    body_text: str | None       # reiner Text für Suche und Längenangabe
    body_len: int
    paywalled: bool
    image_url: str | None
    author: str | None
    page_title: str | None = None   # Titel der Seite (og:title), für drop_title_echo


def safe_http_url(raw: str | None) -> str | None:
    """Gibt die URL nur zurück, wenn sie absolut ist und http(s) verwendet."""
    if not raw:
        return None
    url = raw.strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return url[:1000]


def is_paywalled(page_text: str, ld_decision: bool | None = None) -> bool:
    """Bezahlartikel? `ld_decision`: Angabe aus dem JSON-LD-Objekt des Artikels (_ld_paywall); ohne sie
    zählt das erste Vorkommen von isAccessibleForFree im Seitentext. Die Verlagsmarkierungen (GEA) gelten
    immer."""
    text = page_text[:PAYWALL_SCAN_CHARS]
    if ld_decision is None:
        first = _ACCESS_RE.search(text)
        ld_decision = bool(first and first.group(1).lower() == "false")
    return bool(ld_decision or PAYWALL_RE.search(text) or _BLUR_CLASS_RE.search(page_text[:BLUR_SCAN_CHARS]))


# Interner Link auf einen Artikel der App (Hash-Route der Oberfläche), siehe _Sanitizer.handle_starttag.
_INTERNAL_LINK_RE = re.compile(r"#/a/[1-9][0-9]{0,17}")


class _Sanitizer(HTMLParser):
    """Whitelist-Bereinigung. Mit `max_chars` endet die Ausgabe am ersten Element der obersten
    Ebene nach dieser Länge (spätestens bei der doppelten Länge, dann mit geschlossenen Tags);
    `truncated` sagt, ob gekürzt wurde. Mit `max_images` fallen weitere <img> weg."""

    def __init__(self, base_url: str, max_chars: int | None = None, max_images: int | None = None,
                 allow_internal: bool = False):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.allow_internal = allow_internal   # interne Links #/a/<id> (nur sanitize_fragment)
        self.out: list[str] = []
        self.text: list[str] = []
        self._drop_depth = 0
        self.max_chars, self.max_images = max_chars, max_images
        self.truncated = False
        self._size = 0
        self._images = 0
        self._open: list[str] = []   # offene erlaubte Tags (ohne Void-Tags)
        # Wie oft jeder Name in _open steht: "tag in self._open" wäre bei vielen nie geschlossenen
        # Tags quadratisch (40 000 offene <p> kosteten Sekunden).
        self._open_count: Counter[str] = Counter()

    def _emit(self, piece: str) -> None:
        self.out.append(piece)
        self._size += len(piece)

    def _full(self) -> bool:
        """True, sobald die Ausgabe (hier) enden soll; schließt bei der harten Grenze offene Tags."""
        if self.truncated:
            return True
        if self.max_chars is None or self._size <= self.max_chars:
            return False
        if self._open and self._size <= 2 * self.max_chars:
            return False  # mitten in einem Element: erst an dessen Ende aufhören
        self._truncate()
        return True

    def _truncate(self) -> None:
        """Ab hier nichts mehr ausgeben; offene Tags schließen."""
        for tag in reversed(self._open):
            self.out.append(f"</{tag}>")
        self._open.clear()
        self._open_count.clear()
        self.truncated = True

    def _safe_url(self, raw: str | None) -> str | None:
        if not raw:
            return None
        absolute = urljoin(self.base, raw.strip())
        if urlparse(absolute).scheme not in ("http", "https"):
            return None
        return absolute

    def _break(self, tag: str) -> None:
        if not self._drop_depth and tag not in INLINE_TAGS:
            self.text.append(" ")

    def handle_starttag(self, tag, attrs):
        if self._full():
            return
        self._break(tag)
        if tag in DROP_WITH_CONTENT:
            self._drop_depth += 1
            return
        if self._drop_depth or tag not in ALLOWED_TAGS:
            return
        kept: list[str] = []
        allowed = ALLOWED_ATTRS.get(tag, set())
        internal = False
        for name, value in attrs:
            if name not in allowed:
                continue
            if name == "href" and self.allow_internal and _INTERNAL_LINK_RE.fullmatch((value or "").strip()):
                # Interner Link auf einen Kiosk-Artikel (#/a/<id>), nur ausdrücklich in eigenen Texten
                # (KI-Ausgabe über sanitize_fragment); in Artikelseiten bleibt ein solcher Anker ein Link aufs
                # Original bzw. fällt ohne Basis-URL weg.
                value = value.strip()
                internal = True
            elif name in ("href", "src"):
                value = self._safe_url(value)
                if not value:
                    if tag == "img":
                        return  # Bild ohne brauchbare Quelle weglassen
                    continue
                if tag == "img" and value.endswith(_IMG_EXT_FRAGMENT):
                    value = value[:-len(_IMG_EXT_FRAGMENT)]   # Hilfsendung aus _fix_tree (WELT)
            kept.append(f' {name}="{html.escape(value or "", quote=True)}"')
        if tag == "a" and not internal:
            kept.append(' target="_blank" rel="noopener noreferrer"')
        if tag == "img":
            if self.max_images is not None and self._images >= self.max_images:
                return
            self._images += 1
            kept.append(' loading="lazy"')
        elif tag not in VOID_TAGS:
            self._open.append(tag)
            self._open_count[tag] += 1
        self._emit(f"<{tag}{''.join(kept)}>")

    def handle_startendtag(self, tag, attrs):
        # <svg/> oder <iframe/> haben keinen Inhalt: nicht den Drop-Zähler erhöhen,
        # sonst verschwindet der restliche Artikel.
        if tag in DROP_WITH_CONTENT or self._full():
            return
        self.handle_starttag(tag, attrs)
        # Erlaubte Nicht-Void-Tags wie <a/> sofort schließen, sonst bleibt der Tag offen.
        if not self._drop_depth and tag in ALLOWED_TAGS and tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.truncated:
            return
        self._break(tag)
        if tag in DROP_WITH_CONTENT:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if self._drop_depth or tag not in ALLOWED_TAGS or tag in VOID_TAGS:
            return
        if self._open_count[tag]:  # bis zu diesem Tag schließen (dazwischen offen gebliebene gelten als zu)
            while True:
                closed = self._open.pop()
                self._open_count[closed] -= 1
                if closed == tag:
                    break
        elif self.max_chars is not None and self._size > self.max_chars:
            return  # verwaistes End-Tag über der Grenze: würde die Ausgabe sonst beliebig verlängern
        self._emit(f"</{tag}>")

    def handle_data(self, data):
        if self._drop_depth or self._full():
            return
        data = data.translate(_INVISIBLE)
        if not data:
            return
        escaped = html.escape(data, quote=False)
        if self.max_chars is not None and self._size + len(escaped) > 2 * self.max_chars:
            # Ein einziger riesiger Textknoten ginge sonst an beiden Grenzen vorbei (die greifen nur
            # zwischen Knoten): bis zur Grenze behalten, möglichst an einem Leerzeichen und nie
            # mitten in einer Entität (&amp;), dann Schluss.
            keep = max(0, self.max_chars - self._size)
            space = escaped.rfind(" ", 0, keep)
            cut = space if space > keep * 0.9 else keep
            amp = escaped.rfind("&", 0, cut)
            if amp != -1 and ";" not in escaped[amp:cut]:
                cut = amp
            piece = escaped[:cut]
            self._emit(piece)
            self.text.append(html.unescape(piece))
            self._truncate()
            return
        self._emit(escaped)
        self.text.append(data)


_EMPTY_P_RE = re.compile(r"(<p>\s*</p>\s*)+")
# Unsichtbare Zeichen, die Verlage in den Text streuen: weiches Trennzeichen (taz, trennt Wörter für die
# Volltextsuche: „Bundes\u00adregierung“ wird nicht gefunden), Nullbreite-Leerzeichen und -Nichtverbinder,
# Wortverbinder (Agenturtexte bei ZEIT und DLF) und das Byte-Order-Mark. Sie fallen aus HTML und Text.
_INVISIBLE = dict.fromkeys(map(ord, "\u00ad\u200b\u200c\u2060\ufeff"))


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(_INVISIBLE)).strip()


def sanitize(fragment: str, base_url: str) -> tuple[str, str]:
    """Bereinigt HTML auf eine Whitelist und liefert (html, reiner Text); Obergrenzen wie
    sanitize_limited."""
    clean, text, _ = sanitize_limited(fragment, base_url)
    return clean, text


def sanitize_fragment(fragment: str) -> tuple[str, str]:
    """Nur die Whitelist (_Sanitizer ohne Basis-URL, mit internen Links #/a/<id>), ohne die
    Heuristiken für Verlagsseiten (Müll-Absätze, Bildzeilen, führende <h1>, doppelte Bilder): für eigene,
    schon escapte Texte (KI-Ausgabe), in denen ein Zwischentitel „Anzeige“ oder ein Absatz auf „Name/dpa“
    nicht still verschwinden darf. Liefert (html, reiner Text)."""
    s = _Sanitizer("", allow_internal=True)
    s.feed(fragment)
    s.close()
    return _EMPTY_P_RE.sub("", "".join(s.out)), _normalize_ws("".join(s.text))


def sanitize_limited(fragment: str, base_url: str) -> tuple[str, str, bool]:
    """Wie sanitize, mit den Obergrenzen MAX_BODY_HTML und MAX_IMAGES; liefert zusätzlich, ob
    der Text gekürzt wurde."""
    s = _Sanitizer(base_url, max_chars=MAX_BODY_HTML, max_images=MAX_IMAGES)
    s.feed(fragment)
    s.close()
    body = _EMPTY_P_RE.sub("", "".join(s.out))
    text = _normalize_ws("".join(s.text))
    # Erst Müll-Absätze weg, dann doppelte Bilder: sonst bliebe nur das erste Vorkommen eines Bildes
    # stehen, und fiele es danach mit seinem Müll-Absatz weg, wäre das Bild ganz verloren. Die
    # führende <h1> (Titel, steht separat) erst nach den Müll-Absätzen: Ein leerer Absatz davor
    # verdeckt sie sonst, und ein zweiter Durchlauf käme zu einem anderen Ergebnis.
    cleaned = _drop_junk_paragraphs(body)
    cleaned = re.sub(r"^\s*<h1>.*?</h1>\s*", "", cleaned, count=1, flags=re.S)
    cleaned = _EMPTY_P_RE.sub("", _drop_duplicate_images(cleaned))
    if cleaned != body:   # entfernte Absätze und Bildunterschriften ändern auch den reinen Text
        text = html_to_text(cleaned)
    return cleaned.strip(), text, s.truncated


# ---- Bild-Schlüssel: dasselbe Bild in anderer Größe erkennen ----
# Gleiche Regeln wie imageKey() in static/js/format.js (beide Stellen gemeinsam ändern!):
#  1. Fragment (#…) und Schema (http:// bzw. https://, //) weg; nur der Host wird klein geschrieben,
#     Pfad und Query bleiben, wie sie sind (Bild-IDs können Groß-/Kleinschreibung unterscheiden).
#  2. Query (?…): bleibt, nur ohne Größen- und Signatur-Parameter (w, width, h, height, q, quality,
#     dpr, fit, resize, func, org_if_sml, auto, format, fm, s, sig, itok, smart; Name ohne Beachtung
#     der Schreibung). Zuschnitt-Parameter (crop, rect, im) bleiben hier (strict, Standard des
#     Servers): anderer Ausschnitt kann ein anderes Bild sein (CBC im=Crop,rect=(…)). Nur mit
#     strict=False (Standard von imageKey() im Browser, für die Anzeige) fallen auch sie weg.
#     Steht bei einem Pfad ohne Bild-Endung in url= oder src= eine URL (absolut oder mit / beginnend,
#     dann auf dem Host der äußeren URL), ist deren Schlüssel der Schlüssel (Bild-Proxys wie
#     /_next/image?url=…&w=640), höchstens 5 Ebenen tief; die übrigen Parameter der äußeren URL
#     (ohne Größen-Parameter) werden angehängt, außer die innere URL hat eine Bild-Endung
#     (/thumb?src=/v.mp4&t=10 und &t=50 sind verschiedene Bilder).
#  3. Pfadsegmente, die nur eine Größe angeben, weg: /w960/, /h540/ (mind. zwei Ziffern), /1024x683/.
#  4. Im Dateinamen die Endung(en) weg (.jpg, .JPG, .jpg.webp …), dann Größen-Token weg:
#     _w960 bzw. -w960, _1024x683 bzw. -1024x683, @2x, -scaled; die Spiegel-Zuschnitte _r1.5,
#     _fpx43.2, _fpy41 nur, wenn der Dateiname auch ein _w960-Token hat (Token enden an _ - . @
#     oder am Namensende).
#  Schlüssel = (Host+Pfad[+?Query], bereinigter Dateiname falls ≥ 8 Zeichen, sonst "").
#  Der Server (same_image) wertet nur gleiche Schlüssel als gleiches Bild; die unscharfe
#  Erkennung (gleicher Dateiname, abweichende Größen-Segmente) macht nur sameImage() im Browser,
#  denn ein verlorenes Bild wiegt schwerer als ein doppeltes.
_SIZE_DIR_RE = re.compile(r"/(?:[wh]\d{2,}|\d+x\d+)(?=/)")
_SIZE_TOKEN_RE = re.compile(r"(?:[_-]w\d+|[_-]\d+x\d+|@\d+x|-scaled)(?=[_.@-]|\Z)")
_WIDTH_TOKEN_RE = re.compile(r"_w\d+(?=[_.@-]|\Z)")
_CROP_TOKEN_RE = re.compile(r"(?:_r\d+(?:\.\d+)?|_fp[xy]\d+(?:\.\d+)?)(?=[_.@-]|\Z)")
_IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|avif)\Z", re.I)
_EXT_RE = re.compile(r"(?:\.(?:jpe?g|png|webp|gif|avif))+\Z|\.[a-z][a-z0-9]{1,4}\Z", re.I)
_SIZE_PARAMS = {"w", "width", "h", "height", "q", "quality", "dpr", "fit", "resize",
                "func", "org_if_sml", "auto", "format", "fm", "s", "sig", "itok", "smart"}
_CROP_PARAMS = {"crop", "rect", "im"}   # nur mit strict=False entfernt (siehe oben)
_URL_PARAM_RE = re.compile(r"^(?:(?:https?:)?//|/)", re.I)
_IMAGE_BASE_MIN = 8
_URL_PARAM_DEPTH = 5
_URL_SPACE = " \t\n\r\f\v"   # nur ASCII-Leerraum abschneiden (JS trim() und Python strip() unterscheiden sich)


def image_key(url: str | None, strict: bool = True, _depth: int = 0) -> tuple[str, str]:
    """Schlüssel einer Bild-URL: (Host+Pfad[+?Query] bereinigt, bereinigter Dateiname oder "")."""
    raw = re.sub(r"#.*\Z", "", (url or "").strip(_URL_SPACE), flags=re.S)
    raw = re.sub(r"^//", "", re.sub(r"^[a-z][a-z0-9+.-]*://", "", raw, flags=re.I))
    path, _, query = raw.partition("?")
    host, slash, rest = path.partition("/")
    path = host.lower() + slash + rest
    drop = _SIZE_PARAMS if strict else _SIZE_PARAMS | _CROP_PARAMS
    kept = []
    inner = None
    for part in query.split("&") if query else ():
        name, _, value = part.partition("=")
        name = name.lower()
        if (inner is None and name in ("url", "src") and not _IMAGE_EXT_RE.search(path)
                and _depth < _URL_PARAM_DEPTH):
            candidate = unquote(value).strip(_URL_SPACE)
            if _URL_PARAM_RE.match(candidate):
                if candidate.startswith("/") and not candidate.startswith("//"):
                    candidate = host + candidate   # relativ: Host der äußeren URL
                inner = candidate
                continue
        if part and name not in drop:
            kept.append(part)
    if inner is not None:
        full, base = image_key(inner, strict, _depth + 1)
        inner_path = re.sub(r"[?#].*\Z", "", inner, flags=re.S)
        if kept and not _IMAGE_EXT_RE.search(inner_path):
            full += ("&" if "?" in full else "?") + "&".join(kept)
        return full, base
    cut = path.rfind("/")
    folder = _SIZE_DIR_RE.sub("", path[:cut + 1])
    stem = _EXT_RE.sub("", path[cut + 1:])
    if _WIDTH_TOKEN_RE.search(stem):
        stem = _CROP_TOKEN_RE.sub("", stem)
    stem = _SIZE_TOKEN_RE.sub("", stem)
    full = folder + stem + ("?" + "&".join(kept) if kept else "")
    return full, stem if len(stem) >= _IMAGE_BASE_MIN else ""


def same_image(a: str | None, b: str | None) -> bool:
    """True nur, wenn beide URLs sicher dasselbe Bild zeigen (gleicher Schlüssel)."""
    full_a, full_b = image_key(a)[0], image_key(b)[0]
    return bool(full_a) and full_a == full_b


# Kleines Bild laut URL (Breite < 200 px; _w120, w_64 in Bild-Diensten, /80x80/, width=, dimensions=): Autorenfoto, Wappen, Symbol. Solche Bilder wiederholen
# sich absichtlich (z. B. Vereinswappen in einer Ergebnistabelle) und werden nie als Duplikat
# entfernt. Gleiche Regel wie isSmallImageUrl() in static/js/format.js.
_SMALL_URL_RE = re.compile(r"_w(\d+)(?=[_.@-]|$)|(?:,|%2c|/)w_(\d+)|/(\d{2,3})x\d{2,3}/|[?&](?:width|dimensions)=(\d+)")
_SMALL_MAX = 200


def is_small_image_url(url: str | None) -> bool:
    for m in _SMALL_URL_RE.finditer((url or "").lower()):
        value = next(g for g in m.groups() if g)
        if int(value) < _SMALL_MAX:
            return True
    return False


# Bild im bereinigten HTML, samt einem Link, der nur das Bild umschließt; danach optional das
# schließende </p> bzw. </figure> und ein direkt folgender Absatz (möglicher Bildquellen-Absatz).
_IMG_RE = re.compile(
    r"(?:<a\b[^>]*>\s*(<img\b[^>]*>)\s*</a>|(<img\b[^>]*>))"
    r"(\s*(?:</(?:p|figure)>\s*)?)(<p>(?:(?!</p>|<img\b).)*</p>)?", re.S)
_SRC_RE = re.compile(r'\ssrc="([^"]*)"')
# Platzhalter für ein entferntes Bild: Kommentare erzeugt der Sanitizer nie (Text ist escaped).
_DROP_MARK = "<!--dup-->"
# <figure>, die durch das Entfernen (Platzhalter) leer geworden ist, ggf. mit ihrer Bildunterschrift.
# Andere <figure> ohne Bild (nur Bildunterschrift) bleiben unangetastet.
_EMPTY_FIGURE_RE = re.compile(
    r"<figure>\s*(?:<p>\s*)?" + re.escape(_DROP_MARK) + r"\s*(?:</p>\s*)?"
    r"(?:<figcaption>(?:(?!</figcaption>).)*</figcaption>\s*)?</figure>\s*", re.S)
# Absatz, der nur die Bildquelle nennt („Foto: dpa“); gleiche Regel wie CAPTION_RE in article.js.
CAPTION_RE = re.compile(r"^\s*\(?\s*(?:Fotos?|Bild(?:er)?|Quelle|Photo|Image)\s*:", re.I)


def _drop_duplicate_images(body: str) -> str:
    """Dasselbe Bild (gleicher Schlüssel) nur beim ersten Vorkommen behalten, samt dem direkt
    folgenden Bildquellen-Absatz; kleine Bilder bleiben immer. Nur die <figure>, die dadurch leer
    geworden ist, fällt weg (leere <p> entfernt sanitize danach)."""
    seen: set[str] = set()
    dropped = False

    def repl(m: re.Match) -> str:
        nonlocal dropped
        src_m = _SRC_RE.search(m.group(1) or m.group(2))
        src = html.unescape(src_m.group(1)) if src_m else ""
        key = image_key(src)[0] if src and not is_small_image_url(src) else ""
        if not key:
            return m.group(0)
        if key not in seen:
            seen.add(key)
            return m.group(0)
        dropped = True
        caption = m.group(4) or ""
        if caption and CAPTION_RE.match(html_to_text(caption)):
            caption = ""
        return _DROP_MARK + m.group(3) + caption

    out = _IMG_RE.sub(repl, body)
    return _EMPTY_FIGURE_RE.sub("", out).replace(_DROP_MARK, "") if dropped else body


# Absätze ohne Artikelinhalt, die trafilatura bei manchen Verlagen mitnimmt (z. B. Al Jazeera):
# kurze Datumszeile „Published On 12 Sep 2026“ und ein Absatz, der nur aus dem Link „NewsFeed“ besteht;
# außerdem Absätze, die nur aus einem Bedienelement- oder Werbe-Etikett bestehen (NPR „hide caption“),
# Absätze ohne Text und ohne Bild (<p><strong></strong></p>) und Blindtext („Lorem ipsum dolor sit
# amet …“), den Verlage wie GEA an Stelle des Bezahltextes (unscharf) anzeigen.
_PARA_RE = re.compile(r"<p>(.*?)</p>\s*", re.S)
# Zwischenüberschrift, die nur ein Etikett ist (heise „Lesen Sie auch“, Al Jazeera „NewsFeed“).
_HEADING_RE = re.compile(r"<(h[2-4])>(.*?)</\1>\s*", re.S)
_PUBLISHED_RE = re.compile(r"^\s*Published On\s+\d{1,2}\s+\w+\s+\d{4}")
_PUBLISHED_MAX_LEN = 60     # längere Absätze mit „Published On …“ sind Artikeltext
_NEWSFEED_RE = re.compile(r"^\s*<a\b[^>]*>\s*NewsFeed\s*</a>\s*$")
# Etiketten, verglichen ohne Groß-/Kleinschreibung und ohne Doppelpunkt am Ende: Überschriften von
# Teaser-Kästen (WELT, SZ, heise), Teilen-Knöpfe, Metazeilen ohne Wert („Stand:“ beim Tagesspiegel, der
# Zeitpunkt selbst fällt weg), BBC „Get in touch“, die Sprungmarken um die Newsletter-Werbung des Guardian
# (zweite Sicherung neben _CHROME_ATTRS), ZEIT-Liveblog „Nach oben“ (Link zum Seitenanfang).
_JUNK_LABELS = {"hide caption", "toggle caption", "advertisement", "werbung", "anzeige",
                "lesen sie auch", "lesen sie mehr zum thema", "mehr zum thema", "stand", "lesezeit",
                "videos by heise", "link kopieren", "auf facebook teilen", "auf x teilen", "per e-mail teilen",
                "uns auf google folgen", "newsfeed", "get in touch",
                "skip past newsletter promotion", "after newsletter promotion", "nach oben"}
# Metazeile, deren Wert nur Datum, Uhrzeit oder Dauer ist: „Stand: 28.09.2026 18:07 Uhr“, „Stand: 28.
# September 2026“, „Lesezeit: 3 Min.“; „Stand: 2:1 für Leverkusen nach 70 Minuten.“ bleibt.
# Die Wiederholung ist possessiv (++, Python ≥ 3.11): Ohne sie probiert die Suche bei „12.12.12…x“
# exponentiell viele Aufteilungen durch. Deshalb stehen längere Alternativen vor ihren Präfixen
# („minuten“ vor „min“), ein einmal gefundenes Stück wird nicht mehr neu aufgeteilt.
_META_VALUE = (r"(?:heute|gestern|\d{1,2}\.\d{1,2}\.(?:\d{2,4})?|\d{1,2}:\d{2}|uhr|\d{4}|\d{1,3}\s*(?:minuten|min\.?)"
               r"|\d{1,2}\.?\s*(?:jan|feb|mär|apr|mai|jun|jul|aug|sep|okt|nov|dez)[a-zä]*\.?|[,•|–-])")
_META_LINE_RE = re.compile(rf"^(?:stand|lesezeit)\s*:(?:\s*{_META_VALUE})++\s*$", re.I)
_LOREM_RE = re.compile(r"^\s*Lorem ipsum dolor sit amet\b", re.I)
# Kurze Absätze (< _PROMO_MAX_LEN) mit Eigenwerbung: Google „bevorzugte Quelle“ (WELT, NDR, Tagesspiegel,
# FR, DW „Preferred Source“) bzw. „Folgen Sie uns auf Google“; Newsletter-Anmeldung („Sign up for our
# weekly newsletter …“, El País, Straits Times, NPR; BBC „Get our flagship newsletter … Sign up here.“;
# Guardian-Knopf „Sign up for the Breaking News Australia email“);
# Meduza (Hinweis auf KI-Übersetzung, Kontaktadresse für Fehler, Newsletter-Werbung), BBC-Radiowerbung, die
# App-Werbung am Ende des Times-of-India-Textes und der Rechtevermerk unter WELT-Videos („© Axel Springer
# Deutschland GmbH. Alle Rechte vorbehalten.“).
_PROMO_MAX_LEN = 300
# Nur mit Werbe-Wortlaut: „Die EU wirft Google vor, eigene Angebote bevorzugt anzuzeigen.“ bleibt.
_GOOGLE_PROMO_RE = re.compile(
    r"bevorzugte?n? quelle|in der google-suche[^.]{0,40}bevorzugt|als bevorzugte|preferred source"
    r"|folgen sie uns (?:auf|bei) google|uns (?:auf|bei) google(?: news)? (?:zu )?folgen",
    re.I | re.S)
_PROMO_RE = re.compile(
    r"^sign up\b.{0,200}\bnewsletters?\b"
    r"|^get our\b.{0,120}\bnewsletter\b.{0,120}\bsign up here\b"
    r"|^sign up for\b.{0,80}\bemail\.?$"
    r"|errors in this translation, please contact us\b"
    r"|^to read meduza.s exclusive content"
    r"|^listen to the best of bbc radio"
    r"|download the toi app"
    r"|^©\s*axel springer\b.{0,60}\balle rechte vorbehalten\.?$", re.I | re.S)
# Meduza: Hinweis auf die KI-Übersetzung, länger als _PROMO_MAX_LEN. Guardian-Liveblogs: stehender Hinweis zu
# den Kommentaren („I’m afraid we are no longer able to have comments open below the line every day on this
# blog. If you want to contact me, please email …“), je nach Ressort unterschiedlich lang.
_MEDUZA_AI_RE = re.compile(
    r"^At Meduza, we are committed to transparency about our use of artificial intelligence"
    r"|^I[’']m afraid we are no longer able to have comments open below the line\b")
# Le Monde: Rest des Artikels hinter der Paywall (markiert den Artikel, siehe _PAYWALL_TEXT_RE).
_PAYWALL_NOTE_RE = re.compile(
    r"^You have [\d.,]+\s?% of this article left to read\.?(?:\s*The rest is for subscribers only\.?)?$", re.I)
# Einwilligungs-Dialog (Cookies), der vor dem Artikel steht (derStandard, zweite Sicherung neben
# _CHROME_XPATH); eine Seite, die nur aus dem Dialog besteht (Golem), erkennt _CONSENT_WALL_RE.
# Dazu Platzhalter für eingebettete Inhalte (SPIEGEL „Ich bin damit einverstanden, dass mir externe Inhalte
# angezeigt werden …“, taz „Empfohlener externer Inhalt“ / „Wir würden Ihnen hier gerne einen externen
# Inhalt zeigen“).
_CONSENT_RE = re.compile(
    r"Sie entscheiden darüber, wie Sie unsere Inhalte nutzen wollen"
    r"|Bitte deaktivieren Sie sämtliche Hard- und Software-Komponenten"
    r"|Ich bin damit einverstanden, dass mir externe Inhalte angezeigt werden"
    r"|^Empfohlener externer Inhalt|Wir würden Ihnen hier gerne einen externen Inhalt zeigen")
_CONSENT_WALL_RE = re.compile(r"Besuchen Sie Golem\.de wie gewohnt mit Werbung und Tracking")
_CONSENT_WALL_MAX = 2500    # so lang ist der Dialog höchstens (Golem: rund 2100 Zeichen)
_CONSENT_WALL_HEAD = 300    # oder der Text beginnt mit dem Dialog


def _is_consent_wall(body: str) -> bool:
    """True, wenn der extrahierte Text im Wesentlichen der Einwilligungs-Dialog ist: der Satz steht darin
    und der Text ist kurz oder beginnt mit dem Dialog. Ein Artikel, der den Satz zitiert, bleibt."""
    m = _CONSENT_WALL_RE.search(body)
    if not m:
        return False
    text = html_to_text(body)
    return len(text) < _CONSENT_WALL_MAX or _CONSENT_WALL_RE.search(text[:_CONSENT_WALL_HEAD]) is not None


def _is_label(text: str) -> bool:
    return text.casefold().rstrip(": ").strip() in _JUNK_LABELS


def _is_junk_text(text: str) -> bool:
    """True für den reinen Text eines Absatzes ohne Artikelinhalt (siehe oben)."""
    if _is_label(text) or _LOREM_RE.match(text) or _MEDUZA_AI_RE.match(text):
        return True
    if len(text) < _PUBLISHED_MAX_LEN and _PUBLISHED_RE.match(text):
        return True
    if len(text) < _PROMO_MAX_LEN:
        if "google" in text.casefold() and _GOOGLE_PROMO_RE.search(text):
            return True
        if _PROMO_RE.search(text) or _PAYWALL_NOTE_RE.match(text) or _META_LINE_RE.match(text):
            return True
    return bool(_CONSENT_RE.search(text))


# Lesezeit- und Vorlese-Etiketten aus Bedienleisten und „Mehr zum Thema“-Kästen (SPIEGEL: „3 Min“,
# auch doppelt „3 Min 3 Min“ für schmale und breite Anzeige, „Artikel anhören (3 Minuten)“). Nur wenn
# der ganze Listenpunkt daraus besteht; „Nach 3 Min war alles vorbei“ oder „3 Min Anfahrt“ bleiben.
# Ein Absatz fällt nur mit „Artikel anhören …“ weg: „90 Min“ allein kann in einem Liveticker stehen.
_MINUTES = r"\d{1,3}\s*(?:min\.?|minuten)"
_LISTEN = r"artikel anhören(?:\s*\(\d{1,3}\s*minuten?\))?"
_READING_TIME_RE = re.compile(rf"^(?=\S)(?:{_LISTEN})?(?:\s*{_MINUTES})*$", re.I)
_LISTEN_LABEL_RE = re.compile(rf"^{_LISTEN}(?:\s*{_MINUTES})*$", re.I)
_MINUTES_ONLY_RE = re.compile(rf"^{_MINUTES}(?:\s*{_MINUTES})*$", re.I)
_READING_TIME_HINT_RE = re.compile(r"\d\s*min|anhören", re.I)
# Teaser-Rest: genau ein Link (die Überschrift), danach nur Lesezeit(en).
_LINK_THEN_REST_RE = re.compile(r"^\s*<a\b[^>]*>(?:(?!</?a\b).)*</a>(.*)$", re.S)
# Innerster Listenpunkt (enthält keinen weiteren): Entfernen lässt das HTML wohlgeformt.
_LI_RE = re.compile(r"<li>((?:(?!<li\b|</li>).)*)</li>\s*", re.S)
_EMPTY_LIST_RE = re.compile(r"<(ul|ol)>\s*</\1>\s*")
_LI_MAX = 2000              # längere Listenpunkte sind Inhalt (Teaser-Links mit langer URL passen)
_LIST_PASSES = 5            # verschachtelte Listen: je Ebene ein Durchgang


def _drop_junk_paragraphs(body: str) -> str:
    def repl(m: re.Match) -> str:
        inner = m.group(1)
        if _NEWSFEED_RE.match(inner):
            return ""
        text = html_to_text(inner)
        if (not text and "<img" not in inner) or (text and _is_junk_text(text)):
            return ""
        if "google.com/preferences" in inner and len(text) < _PROMO_MAX_LEN:
            return ""   # Link „als bevorzugte Quelle einrichten“
        return m.group(0)

    def heading(m: re.Match) -> str:
        inner = m.group(2)
        if len(inner) > 500 or "<img" in inner:
            return m.group(0)
        return "" if _is_label(html_to_text(inner)) else m.group(0)

    body = _DANGLING_MORE_RE.sub("", _HEADING_RE.sub(heading, _PARA_RE.sub(repl, body)))
    return _drop_orphan_captions(_drop_reading_times(body))


# Hinweis auf einen eingebetteten Inhalt, den die Extraktion verwirft (ZEIT-Liveblog: „Lesen Sie hier mehr zum
# Frontverlauf:“ vor einer Karte als Tickaroo-Einbettung): nur als eigener Absatz mit Doppelpunkt am Ende und
# nur, wenn danach nichts kommt, was er einleiten könnte (Ende des Textes, Zeitzeile des nächsten Eintrags,
# Zwischenüberschrift).
_DANGLING_MORE_RE = re.compile(
    r"<p>\s*(?:<(?:i|em|b|strong)>\s*)?Lesen Sie (?:hier |dort )?mehr\b[^<:]{0,80}:\s*(?:</(?:i|em|b|strong)>\s*)?"
    r"</p>\s*(?=\Z|<p>\s*<strong>\d{2}\.\d{2}\. \d{2}:\d{2}</strong>\s*</p>|<h[2-4]>)")


def _drop_reading_times(body: str) -> str:
    """Absätze mit „Artikel anhören …“ und Listenpunkte, die nur eine Lesezeit („3 Min“) oder den
    Rest eines Teasers (ein Link, danach nur Lesezeiten) enthalten, samt dadurch leer gewordener
    Listenpunkte und Listen. Auch für schon gespeicherte Artikel (strip_reading_times)."""

    def para(m: re.Match) -> str:
        inner = m.group(1)
        if len(inner) > 500 or "<img" in inner or "anhören" not in inner.lower():
            return m.group(0)
        return "" if _LISTEN_LABEL_RE.match(html_to_text(inner)) else m.group(0)

    def item(m: re.Match) -> str:
        inner = m.group(1)
        # Kurz-Prüfungen vor html_to_text (eigener Parser je Element): Etiketten sind kurz, enthalten
        # nie ein Bild und immer „<Zahl> Min“ bzw. „anhören“. Ein leerer Listenpunkt (etwa
        # <li><p>3 Min</p></li> nach dem Absatz, oder eine geleerte innere Liste) fällt mit weg.
        if len(inner) > _LI_MAX or "<img" in inner:
            return m.group(0)
        if not _TAG_RE.sub("", inner).strip():
            return ""
        if not _READING_TIME_HINT_RE.search(inner):
            return m.group(0)
        if _READING_TIME_RE.match(html_to_text(inner)):
            return ""
        teaser = _LINK_THEN_REST_RE.match(inner)
        if teaser and _MINUTES_ONLY_RE.match(html_to_text(teaser.group(1))):
            return ""   # reine Linkliste (ohne Lesezeit) bleibt
        return m.group(0)

    out = _PARA_RE.sub(para, body)
    for _ in range(_LIST_PASSES):
        step = _LI_RE.sub(item, out)
        if step == out:
            break
        out = _EMPTY_LIST_RE.sub("", step)
    return out


# Bildnachweis als eigener Absatz („(Bild: Robert Way/Shutterstock.com)“ bei heise, „Foto: dpa“), dessen
# Bild die Extraktion verloren hat: kein Anker davor (Bild, Ende einer Tabelle) oder mehr als _ORPHAN_GAP
# Zeichen Text dazwischen. „Quelle: …“ nur in Klammern: ohne Klammern nennt es oft die Quelle einer
# Statistik oder des Textes („Quelle: Statistisches Bundesamt“).
_ORPHAN_CAPTION_RE = re.compile(
    r"^\(\s*(?:Fotos?|Bild(?:er)?|Quelle|Photo|Image)\s*:[^()]{1,120}\)$"
    r"|^(?:Fotos?|Bild(?:er)?|Photo|Image)\s*:[^()]{1,120}$", re.I)
# Dazu kurze Absätze, die mit einem Agentur-Bildnachweis enden (SZ: „Sven Hoppe/dpa“, „Zwei Männer, ein
# Wagen. Sven Hoppe/dpa“): Name aus großgeschriebenen Wörtern, „/“, Agentur. „berichteten dpa/AFP“ bleibt.
_AGENCY_CREDIT_RE = re.compile(
    r"(?:^|\s)(?:[A-ZÄÖÜ][\w.'-]*\s+){1,3}[A-ZÄÖÜ][\w.'-]*\s*/\s*(?:[\w .-]{1,30}/\s*)?"
    r"(?:dpa|AP|Reuters|AFP|IMAGO|imago images|picture alliance|epd|KNA|Getty Images|ddp|Xinhua|EPA)\s*$")
_AGENCY_CREDIT_MAX = 150
_ORPHAN_GAP = 300
_ANCHOR_RE = re.compile(r"<img\b|</table>")


def _drop_orphan_captions(body: str) -> str:
    """Linear: Jeder Abschnitt zwischen zwei Kandidaten wird einmal (höchstens zweimal) gelesen."""
    if ":" not in body and "/" not in body:
        return body
    out: list[str] = []
    pos = 0         # bis hier ist body übernommen bzw. verworfen
    scanned = 0     # bis hier ist der Abstand zum letzten Anker bekannt
    gap = None      # Textlänge seit dem letzten Anker, None: noch kein Anker
    for m in _PARA_RE.finditer(body):
        inner = m.group(1)
        if len(inner) > 400 or "<img" in inner or (":" not in inner and "/" not in inner):
            continue
        text = html_to_text(inner)
        if not ((_ORPHAN_CAPTION_RE.match(text) and _credit_label(text))
                or (len(text) <= _AGENCY_CREDIT_MAX and _AGENCY_CREDIT_RE.search(text))):
            continue
        segment = body[scanned:m.start()]
        anchors = list(_ANCHOR_RE.finditer(segment))
        if anchors:
            gap = len(html_to_text(segment[anchors[-1].start():]))
        elif gap is not None:
            gap += len(html_to_text(segment))
        scanned = m.end()
        if gap is None or gap > _ORPHAN_GAP:
            out.append(body[pos:m.start()])
            pos = m.end()
        elif gap is not None:
            gap += len(text)
    if not out:
        return body
    out.append(body[pos:])
    return "".join(out)


def strip_reading_times(body_html: str | None) -> str | None:
    """Wie _drop_reading_times, für die Auslieferung schon gespeicherter Artikel (vor der Bereinigung
    extrahiert, z. B. SPIEGEL mit Liste „3 Min / 6 Min“). Ohne „<Zahl> Min“ bzw. „anhören“ im Text
    nur eine Suche im String."""
    if not body_html or not _READING_TIME_HINT_RE.search(body_html):
        return body_html
    return _drop_reading_times(body_html)


_LEAD_RE = re.compile(r"\s*<(figcaption|h2|p)>(.*?)</\1>\s*", re.S)
# Element der obersten Ebene im bereinigten HTML (Bild oder Element mit End-Tag, nicht verschachtelt).
_TOP_ELEMENT_RE = re.compile(r"\s*(?:<img\b[^>]*>|<(\w+)\b[^>]*>.*?</\1>)\s*", re.S)
_H1_RE = re.compile(r"<h1>(.*?)</h1>\s*", re.S)
_HEADLINE_TOP = 2           # so viele Elemente am Anfang werden auf die Seitenüberschrift geprüft
_HEADLINE_OVERLAP = 0.6     # Anteil gemeinsamer Wörter (bezogen auf die längere Wortmenge)
_NAME_PARTICLES = {"von", "van", "de", "der", "den", "da", "di", "du", "le", "la", "al", "bin", "ten", "ter", "zu"}
_HEADLINE_CONTAINED = 0.8   # Anteil der Titelwörter, die in der Überschrift stehen (Dachzeile davor)


def _norm_title(text: str) -> str:
    return _normalize_ws(text).casefold()


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.casefold()))


def _is_headline_echo(heading: str, title: str) -> bool:
    """Wörter zu ≥ _HEADLINE_OVERLAP gemeinsam, oder der Titel (ab 3 Wörtern) steht zu ≥ _HEADLINE_CONTAINED
    in der Überschrift, die davor eine Dachzeile trägt („Verhandlungen in der Chemiebranche Evonik schlägt
    BASF-Übernahmeangebot aus“ zum Titel „Evonik schlägt BASF-Übernahmeangebot aus“)."""
    a, b = _words(heading), _words(title)
    if not a or not b:
        return False
    common = len(a & b)
    return (common / max(len(a), len(b)) >= _HEADLINE_OVERLAP
            or (len(b) >= 3 and common / len(b) >= _HEADLINE_CONTAINED and len(a) <= 3 * len(b)))


def _drop_echo(body: str, title: str) -> str:
    """Titel-Wiederholungen aus `body` entfernen (Regeln siehe drop_title_echo). Ein führendes Bild samt
    <figcaption> wird dabei übersprungen: _arrange_top zieht es vor einen Absatz mit dem Titel."""
    start = re.match(r"\s*", body).end()
    img = _LEADING_IMAGE_RE.match(body, start)
    skip = img.end() if img else 0
    m = _LEAD_RE.match(body, skip)
    if m and _norm_title(html_to_text(m.group(2))) == _norm_title(title):
        body = body[:m.start()] + body[m.end():]
    pos = skip
    for _ in range(_HEADLINE_TOP):
        m = _TOP_ELEMENT_RE.match(body, pos)
        if not m:
            break
        if m.group(1) in ("h1", "h2") and _is_headline_echo(html_to_text(m.group(0)), title):
            body = body[:m.start()] + body[m.end():]
            break
        if m.group(1) == "p" and html_to_text(m.group(0)):
            break   # erster Absatz mit Text: danach ist eine Überschrift Teil des Artikels
        pos = m.end()
    if "<h1>" in body:
        # Nur Buchstaben und Ziffern vergleichen: Dachzeile und Überschrift sind im <h1> oft ohne
        # Doppelpunkt verbunden („Fußball-Bundesliga BVB verlängert …“ zum Titel „Fußball-Bundesliga: …“).
        body = _H1_RE.sub(lambda h: "" if _squash(html_to_text(h.group(1))) == _squash(title) else h.group(0),
                          body)
    return body.strip()


def drop_title_echo(result: Extraction, title: str | None) -> Extraction:
    """Entfernt Wiederholungen des Titels, der in der Leseansicht separat steht, verglichen mit dem Titel
    aus dem Feed und dem der Seite selbst (og:title, Extraction.page_title):
    - ein <figcaption>, <h2> oder <p> mit genau dem Titel am Anfang (nach einem führenden Bild;
      Groß-/Kleinschreibung und Leerraum egal);
    - die Seitenüberschrift: ein <h1>/<h2> unter den ersten _HEADLINE_TOP Elementen (vor dem ersten
      Absatz mit Text), dessen Wörter zu mindestens _HEADLINE_OVERLAP mit dem Titel übereinstimmen
      (SPIEGEL, tagesschau, FOCUS, Handelsblatt setzen Dachzeile und Überschrift zusammen);
    - jedes <h1> mit genau dem Titel, auch nach dem Text (GEA).
    Außerdem gilt ein Autor, der im Titel steht und kein Name ist (ein Wort oder mit kleingeschriebenem
    Wort), als falsch erkannt (GEA-Dachzeile „Auffahrunfall“).
    Bliebe kein Text, bleibt der Text, wie er ist."""
    titles = [t for t in (title, result.page_title) if t and t.strip()]
    if not titles:
        return result
    author = result.author
    # Nur ein einzelnes Wort oder eine Wortgruppe mit kleingeschriebenem Wort (GEA-Dachzeilen „Auffahrunfall“,
    # „Einbrecher durchwühlen zwei Kindergärten“), kein Name wie „Friedrich Merz“ im Titel „Gastbeitrag von …“.
    if author and "," not in author and any(
            re.search(rf"(?<!\w){re.escape(_norm_title(author))}(?!\w)", _norm_title(t)) for t in titles):
        words = author.split()
        if len(words) == 1 or any(w[:1].islower() and w.casefold() not in _NAME_PARTICLES for w in words):
            author = None
    body = result.body_html or ""
    for t in titles:
        body = _drop_echo(body, t)
    if body == (result.body_html or "").strip():
        return result if author == result.author else replace(result, author=author)
    text = html_to_text(body)
    if not text:
        # nur der Titel da: lieber so lassen als leeren Text speichern
        return result if author == result.author else replace(result, author=author)
    return replace(result, body_html=body, body_text=text, body_len=len(text), author=author)


def html_to_text(fragment: str | None) -> str:
    """Reiner Text aus (bereits bereinigtem) HTML, z. B. für die Migration bestehender Artikel."""
    if not fragment:
        return ""
    s = _Sanitizer("http://localhost/")
    s.feed(fragment)
    s.close()
    return _normalize_ws("".join(s.text))


# Klasse der unscharf dargestellten Platzhalter-Elemente (GEA+), die an Stelle des Bezahltextes
# stehen; sie fallen vor der Extraktion samt Inhalt weg (die Lorem-Regel in _drop_junk_paragraphs
# greift nur für Absätze, die mit dem Blindtext beginnen).
PLACEHOLDER_CLASS = "nfy-text-wrapper-blur"


def _cls(name: str) -> str:
    """XPath-Bedingung: Das Element trägt die Klasse `name` (ganzes Wort)."""
    return f"contains(concat(' ', normalize-space(@class), ' '), ' {name} ')"


# Bedienelemente, die nie Artikeltext sind, deren Beschriftung trafilatura aber teils stehen lässt:
# Knöpfe (SPIEGEL „Artikel anhören (3 Minuten)“ mit „3 Min“, „Zur Merkliste hinzufügen“) und die
# Lesezeit neben einem Symbol in „Mehr zum Thema“-Kästen mitten im Artikel (data-icon-auxiliary,
# „1 Min“; die Überschrift des Teasers verwirft trafilatura, die Lesezeit blieb als Liste stehen).
# Knöpfe mit Bild bleiben (Bildvergrößerung um ein Artikelbild). aria-hidden="true" bewusst nicht:
# El País versteckt so echte Bildunterschriften, DW Datumsangaben.
# SPIEGEL kennzeichnet den ganzen „Mehr zum Thema“-Kasten mit data-area="related_articles"; ohne ihn
# rutschen auch keine Teaser-Überschriften mehr in den Text.
# FOCUS online hängt an manche Artikel einen Werbekasten „So richten Sie FOCUS online als bevorzugte
# Quelle bei Google ein“ (Article-ReusableText); dieselbe Klasse trägt auch Autorenkästen, die bleiben.
# Dazu die Newsletter-Anmeldung („Hier für den Auto-Newsletter anmelden!“) und die Autorenzeile samt
# Porträt über dem Text (Content-Author-List); der Autor steht schon in der Metazeile.
_CHROME_XPATH = (
    "//button[not(.//img or .//picture)]"
    " | //*[@data-contains-auxiliary-icons or @data-icon-auxiliary]"
    " | //*[@data-area='play_button' or @data-area='bookmark_button' or @data-area='related_articles']"
    f" | //*[{_cls('Article-ReusableText')}][contains(normalize-space(.), 'bevorzugte Quelle bei Google')]"
    " | //*[@data-element-tracking-name='newsletter-widget']"
    f" | //*[{_cls('Content-Author-List')}]"
)
# Weitere Kästen und Etiketten (_chrome_elements: ein Durchlauf über alle Elemente, linear):
# - Text nur für Screenreader (visually-hidden, screen-reader-text, sr-only): Sprungmarken, „, external“
#   hinter BBC-Links, „Published“ vor dem Datum. SRF versteckt Autorennamen mit h-offscreen, die bleibt.
# - Teaser und Empfehlungen: BBC (promo, topic-list, Metadatenleiste, Autorenzeile, „Get in touch“),
#   Al Jazeera (more-on), NPR (internallink), WELT (c-inline-teaser-list, c-inline-video, Google-Kasten), tagesschau
#   (teaser-absatz, teaser-xs), NDR (div.teaser), n-tv (TeaserInline_wrapper, VideoInline_wrapper),
#   Handelsblatt (verwandte Themen, Teaser, Hinweis auf die Richtlinien), heise (RecommendationBox,
#   „Videos by heise“, a-opt-in, Newsletter-Werbung figure.branding), Le Monde (catcher,
#   js-article-read-also), CSM (aside.injection), CNA (Spiele), Independent (Merken, Kommentare), Moscow
#   Times und taz (Spendenaufrufe), FR (Google-Folgen-Knopf), GEA (Logo, Teaser nfy-ar-teaser und
#   nfy-teaser-box), Tickaroo-Liveblogs (FAZ, SZ: Teilen-Knöpfe tik4-sharing, Autorennamen und -fotos,
#   Übersicht „Wichtige Updates“ tik4-hs mit den Überschriften).
# - Tagesspiegel debug-piano (Liste „showPaywall: false …“), ZEIT #summy-content (KI-Zusammenfassung),
#   NPR „hide caption“/„toggle caption“, Guardian-Datumszeile (dateline; den angehefteten Eintrag eines
#   Liveblogs behandelt _pinned_post), ZEIT-Liveblog „Nach oben“, derStandard
#   tcf-notloaded (versteckter Einwilligungs-Dialog).
# - Der kleinste Kasten um einen Link „als bevorzugte Quelle bei Google“ (google.com/preferences/source).
_CHROME_CLASSES = frozenset({
    PLACEHOLDER_CLASS, "visually-hidden", "screen-reader-text", "sr-only", "internallink", "hide-caption",
    "toggle-caption", "debug-piano", "tzi-bottom-container", "c-article-page__preferred-sources",
    "c-inline-teaser-list", "c-inline-video", "teaser-absatz", "teaser-xs", "tik4-sharing",
    "tik4-author", "tik4-author__thumb", "tik4-hs", "ad--inread-header", "id-Story-googleFollowButton",
    "bookmark-experience", "show-comments", "js-comment", "contribute-article", "js-article-read-also",
    "in-article-games", "tcf-notloaded", "nfy-ar-teaser", "nfy-teaser-box",
    "liveblog__chapter-button--back-to-top",
})
_CHROME_CLASS_PARTS = ("TeaserInline_wrapper", "VideoInline_wrapper")   # CSS-Module: Klasse mit Suffix
_CHROME_TAG_CLASSES = {"div": {"teaser"}, "figure": {"branding"}, "section": {"more-on", "catcher"},
                       "aside": {"injection"}}
_CHROME_ATTRS = {
    "data-testid": {"promo", "topic-list", "metadata", "single-byline", "multi-byline"},
    "data-block": {"uploaderEmbed"},
    "data-component": {"RecommendationBox"},   # Guardian pinned-post: siehe _fix_tree
    "data-gu-name": {"dateline"},
    "id": {"summy-content"},
    "data-area": {"embed"},   # SPIEGEL: eingebetteter Inhalt, ohne Einwilligung nur ein Platzhalter
    # Guardian: Newsletter-Werbung im Text („skip past newsletter promotion“ … „after newsletter promotion“),
    # eine <figure> um <gu-island name="EmailSignUpWrapper">
    "data-spacefinder-type": {"model.dotcomrendering.pageElements.NewsletterSignupBlockElement"},
    "name": {"EmailSignUpWrapper"},
}
# ZEIT-Liveblogs: Im Kasten „Überblick“ (section.liveblog-preceding-content) über den Einträgen steht eine
# Liste mit stehenden Hinweisen („Alle Artikel zum Thema finden Sie auf unserer Themenseite …“, „Neben eigenen
# Recherchen verwenden wir Material der Nachrichtenagenturen …“, „Wie wir bei der ZEIT Nachrichten zum
# Ukrainekrieg machen …“, „Russische oder ukrainische Angaben zu Details einzelner Angriffe … lassen sich
# derzeit nicht unabhängig verifizieren.“). Diese Listenpunkte fallen weg, andere im Kasten bleiben, auch
# Meldungen mit der Agenturformel „Die Angaben lassen sich nicht unabhängig überprüfen.“: Der Hinweis zählt nur
# als ganzer Listenpunkt in genau diesem Wortlaut.
_ZEIT_OVERVIEW_CLASS = "liveblog-preceding-content"
_ZEIT_NOTE_RE = re.compile(
    r"^(?:Alle Artikel zum Thema finden Sie auf unserer|Neben eigenen Recherchen verwenden wir Material"
    r"|Wie wir bei der ZEIT Nachrichten zum"
    r"|(?:\w+ (?:oder|und) )?\w+ Angaben zu Details einzelner Angriffe\b[^.]{0,150}\blassen sich (?:derzeit )?"
    r"nicht unabhängig (?:überprüfen|verifizieren|bestätigen)\.?$)")
_ZEIT_NOTE_MAX = 400
_ZEIT_NOTE_TAGS = 50        # Hinweise haben wenige Elemente (Links, <br>); größere Listenpunkte gar nicht lesen


def _is_zeit_note(li) -> bool:
    # Erst begrenzt zählen: Ohne Grenze läse jeder verschachtelte Listenpunkt alle inneren noch einmal
    # (quadratisch). Ein Listenpunkt mit einem inneren ist kein Hinweis.
    for n, el in enumerate(li.iter()):
        if n >= _ZEIT_NOTE_TAGS or (n and el.tag == "li"):
            return False
    text = _short_text(li, _ZEIT_NOTE_MAX)
    return bool(text) and len(text) <= _ZEIT_NOTE_MAX and _ZEIT_NOTE_RE.search(text) is not None


_CHROME_TAGS = {"app-storyline-related-topics", "app-default-teaser", "app-guidelines-reference", "a-opt-in"}
_GOOGLE_PREFS_XPATH = "//a[contains(@href, 'google.com/preferences/source')]/.."


def _chrome_elements(tree) -> list:
    """Elemente aus _CHROME_XPATH und den Listen oben. Die Listen prüft ein einziger Durchlauf in Python
    (Klassen als Menge): Eine XPath-Abfrage mit dreißig contains(concat(normalize-space(@class)))
    brauchte bei großen Seiten Sekunden."""
    found = list(tree.xpath(_CHROME_XPATH))
    zeit_notes = False
    for el in tree.iter():
        tag = el.tag
        if not isinstance(tag, str):
            continue
        if tag in _CHROME_TAGS:
            found.append(el)
            continue
        cls = el.get("class")
        if cls:
            names = set(cls.split())
            if not zeit_notes and _ZEIT_OVERVIEW_CLASS in names:
                zeit_notes = True   # nur der erste Kasten: verschachtelte würden sonst mehrfach durchsucht
                found += [li for li in el.iter("li") if _is_zeit_note(li)]
            if (not names.isdisjoint(_CHROME_CLASSES) or any(part in cls for part in _CHROME_CLASS_PARTS)
                    or not names.isdisjoint(_CHROME_TAG_CLASSES.get(tag, ()))):
                found.append(el)
                continue
        if any(el.get(attr) in values for attr, values in _CHROME_ATTRS.items()):
            found.append(el)
        elif tag == "img" and "/pu_gea/image/logo" in (el.get("src") or ""):
            found.append(el)
        elif el.get("data-spacefinder-type") == _GUARDIAN_LINK_BLOCK and _is_newsletter_button(el):
            found.append(el)
        elif tag == "img" and "bbci.co.uk/" in (el.get("src") or ""):
            block = _bbc_banner_block(el)
            if block is not None:
                found.append(block)
    # Der kleinste Kasten um den Link „als bevorzugte Quelle bei Google einrichten“
    found += [el for el in tree.xpath(_GOOGLE_PREFS_XPATH) if len(el.text_content()) < 600
              and len(_normalize_ws(el.text_content())) < 300]
    # Ein Element kann mehrfach passen (<button class="sr-only">, WELT-Kasten mit Google-Link): nur einmal
    return list(dict.fromkeys(found))


# Guardian: Knopf zur Newsletter-Anmeldung im Text („Sign up for the Breaking News Australia email“), eine
# <figure data-spacefinder-type="…LinkBlockElement"> mit einem Link auf /email-newsletters. Andere Knöpfe bleiben.
_GUARDIAN_LINK_BLOCK = "model.dotcomrendering.pageElements.LinkBlockElement"
_NEWSLETTER_LINK_RE = re.compile(r"/(?:email-)?newsletters?(?:[/?#]|$)", re.I)


def _is_newsletter_button(el) -> bool:
    links = el.xpath(".//a/@href")[:5]
    return bool(links) and all(_NEWSLETTER_LINK_RE.search(h) for h in links)


# BBC: Werbebanner am Ende des Textes (Newsletter „News Daily“, „Politics Essential“, „Your Voice“, BBC News
# Africa) stehen wie Artikelbilder in <div data-block="image">, ohne eigene Kennung. Erkannt am Bild: PNG von
# ichef.bbci.co.uk, sehr breit (1920 × 316, Seitenverhältnis ab _BANNER_RATIO) oder mit dem Alt-Text der
# Banner („A thin, grey banner promoting the News Daily newsletter“, „Your Voice banner image“). Nur „banner“
# reicht nicht: „The Daily Mirror front page carries the banner headline …“ ist ein Artikelbild. Fotos sind
# JPEG; PNG-Grafiken im Text (Karten, Diagramme) sind nicht so breit. Ohne lesbare Breite und Höhe gilt nur
# der Alt-Text.
_BANNER_RATIO = 4
_BANNER_ALT_RE = re.compile(r"\bbanner promoting\b|^\s*your voice banner\b|\bnewsletter banner\b", re.I)
_BANNER_LEVELS = 10         # so viele Ebenen über dem Bild wird der Block gesucht (BBC: 7)


def _bbc_banner_block(img):
    """Der <div data-block="image"> um ein BBC-Werbebanner (siehe oben), sonst None. Sucht vom Bild aus nach
    oben (höchstens _BANNER_LEVELS Ebenen): linear, auch bei verschachtelten Blöcken."""
    src = img.get("src") or ""
    if not src.split("?", 1)[0].lower().endswith(".png"):
        return None
    if not _BANNER_ALT_RE.search(img.get("alt") or ""):
        try:
            width, height = float(img.get("width") or 0), float(img.get("height") or 0)
        except ValueError:
            return None
        if not (math.isfinite(width) and math.isfinite(height) and width > 0 and height > 0
                and width / height >= _BANNER_RATIO):
            return None
    for level, block in enumerate(img.iterancestors()):
        if level >= _BANNER_LEVELS:
            break
        if block.get("data-block") == "image":
            return block
    return None


_SCREEN_READER_CLASSES = frozenset({"visually-hidden", "screen-reader-text", "sr-only"})


def _is_separator(el) -> bool:
    """Screenreader-Text ohne Buchstaben und Ziffern (nur Satzzeichen, „: “) und ohne Kindelemente."""
    if len(el) or not _SCREEN_READER_CLASSES.intersection((el.get("class") or "").split()):
        return False
    text = el.text or ""
    return bool(text.strip()) and len(text) <= 5 and not re.search(r"\w", text)


def _cleaned_tree(page: bytes):
    """(Baum, geändert): die Seite als lxml-Baum ohne Platzhalter- und Bedienelemente, mit den Umbauten aus
    _fix_tree. Der Baum dient Extraktion, Metadaten, Vorspann und JSON-LD (nur einmal geparst). Keine
    Vorprüfung auf den Bytes mehr: Irgendeines der Merkmale steht ohnehin in fast jeder Seite. (None,
    False), wenn das Parsen scheitert."""
    try:
        tree = load_html(page)
    except Exception:  # noqa: BLE001
        return None, False
    if tree is None:
        return None, False
    dropped = changed = False
    try:
        for el in _chrome_elements(tree):
            # Ohne Elternelement (die Wurzel <html class=…>, oder schon entfernt) lässt sich nichts entfernen.
            if el.getparent() is None:
                continue
            if _is_separator(el):
                el.drop_tag()    # FAZ: <span class="sr-only">: </span> trennt Dachzeile und Titel, bleibt als Text
            else:
                el.drop_tree()   # behält den Text nach dem Element (tail); verschachtelte gehen mit
            dropped = True
    except Exception:  # noqa: BLE001
        # Ein Fehler hier soll nicht die übrige Arbeit (Umbauten, JSON-LD, Vorspann) kosten.
        log.warning("Bereinigung der Seite gescheitert", exc_info=True)
    if _TREE_FIXES_RE.search(page):
        try:
            changed = _fix_tree(tree)
        except Exception:  # noqa: BLE001
            log.warning("Umbau der Seite gescheitert", exc_info=True)
    return tree, dropped or changed


def _precleaned(page: bytes):
    """Wie _cleaned_tree, aber die Bytes unverändert, wenn nichts zu tun war oder das Parsen scheitert.
    Nur für Tests: extract() arbeitet direkt mit _cleaned_tree."""
    tree, changed = _cleaned_tree(page)
    return tree if changed and tree is not None else page


# Umbauten im Baum, damit trafilatura Inhalt nicht verwirft (Vorprüfung auf den Bytes: _TREE_FIXES_RE):
# - heise: <a-img src=…> mit einem SVG-Platzhalter als <img>, das echte Bild steht in <noscript>
#   daneben (trafilatura verwirft <noscript>). Das <a-img> wird zum <img> mit der Adresse aus <noscript>
#   (verkleinertes Bild), sonst der eigenen; Vorlagen mit „${image}“ fallen weg.
# - WELT: Bildadressen ohne Dateiendung („…/dpa-com-260928-935-1641004-jpg“) verwirft trafilatura
#   (utils.is_image_file sucht eine Endung); sie bekommen ein Fragment „#.jpg“, das der Sanitizer
#   wieder entfernt (_IMG_EXT_FRAGMENT).
# - Guardian-Liveblogs: Jeder Eintrag ist ein <article id="block-…"> (in #liveblog-body) mit <header>;
#   trafilatura behandelt beides als Seitenrahmen und behält nur Absätze, ohne Zwischenüberschriften,
#   Zitate und Bilder. Der Eintrag wird zu <section>, der Kopf zu <div>: vorn die Uhrzeit wie bei Tickaroo
#   als <p><strong>28.09. 17:31</strong></p> (trennt die Einträge), dann die Überschrift; relative Zeit („18m
#   ago“), Autorenname und -foto fallen weg, ebenso die Teilen-Leiste; Bildunterschriften in den Einträgen
#   werden Absätze (die <figcaption> verwarf trafilatura). (<hr> zwischen den Einträgen
#   verwirft trafilatura, die Uhrzeit trennt sie.)
# - Tickaroo-Liveblogs (FAZ, SZ): Die Zeit eines Eintrags steht in <time class=tik4-meta__ts>, die
#   trafilatura verwirft, und relativ („Heute 12:43 Uhr“, „Vor 44 Minuten“), was im gespeicherten Text
#   bald falsch ist. Sie wird <p><strong>28.09. 14:43</strong></p> aus dem datetime-Attribut (Zeitzone
#   der App, _local_tz); ohne lesbares datetime fällt sie weg.
# - Tickaroo: Der Text eines Eintrags steht in <div class=tik4-rich-text><div>Satz<br>Satz<br><br>Satz</div>;
#   trafilatura gab ihn einmal als Absatz mit <br> und noch einmal Satz für Satz aus (ein Drittel doppelt).
#   Jeder innere <div> (ohne Blockelemente darin) wird ein Absatz, an jedem <br> geteilt.
# - FAZ: Die Bildunterschrift des Titelbilds steht im Kopf in <div class="meta2"> (Text und <small>Nachweis),
#   den trafilatura verwirft; er wird ein Absatz mit <br> vor dem Nachweis, _arrange_top macht daraus die
#   <figcaption> (mit dem Absatz, wenn trafilatura ihn behält, sonst aus _page_captions).
_TREE_FIXES_RE = re.compile(
    rb"<a-img\b|-(?:jpe?g|png|webp)[\"'\s]|id=\"block-|tik4-meta__ts|tik4-rich-text|header-teaser-image"
    rb"|taz\.de/picture/\d+/\d{1,2}/", re.I)
# taz: Im Text steht ein 14 px breiter Platzhalter (https://taz.de/picture/<id>/14/…webp), das echte Bild
# daneben in <noscript>; der Platzhalter bekommt dessen Adresse (ohne <noscript>-Bild fällt er weg). Nur
# Bilder von taz.de: Andere Seiten können denselben Pfad für echte Bilder nutzen.
_TAZ_LQIP_RE = re.compile(r"^\s*https?://(?:[\w-]+\.)*taz\.de/picture/\d+/\d{1,2}/[^/]+$", re.I)
_IMG_EXT_FRAGMENT = "#.jpg"
_EXTLESS_IMG_RE = re.compile(r"^https?://[^?#]+-(?:jpe?g|png|webp)$", re.I)


def _fix_tree(tree) -> bool:
    changed = False
    for el in tree.xpath("//a-img"):
        src = el.get("src") or ""
        parent = el.getparent()
        if parent is None:
            continue
        noscript = el.getnext()
        real = noscript.xpath(".//img/@src") if noscript is not None and noscript.tag == "noscript" else []
        if real and "${" not in real[0]:
            src = real[0]
        if not src or "${" in src:
            el.drop_tree()
            changed = True
            continue
        alt = el.get("alt") or ""
        for child in list(el):
            el.remove(child)
        el.attrib.clear()
        el.tag = "img"
        el.set("src", src)
        if alt:
            el.set("alt", alt)
        changed = True
    for img in tree.xpath("//img[contains(@src, '/picture/')]"):
        if not _TAZ_LQIP_RE.search(img.get("src") or ""):
            continue
        parent = img.getparent()
        real = parent.xpath("./noscript//img/@src") if parent is not None else []
        if real and not _TAZ_LQIP_RE.search(real[0]):
            img.set("src", real[0].strip())
        else:
            img.drop_tree()
        changed = True
    for img in tree.xpath("//img[@src or @data-src]"):
        for attr in ("src", "data-src"):
            value = img.get(attr) or ""
            if _EXTLESS_IMG_RE.match(value):
                img.set(attr, value + _IMG_EXT_FRAGMENT)
                changed = True
    for caption in tree.xpath("//*[@data-external-selector='header-teaser-image']"
                              "/following-sibling::div[1][contains(concat(' ', @class, ' '), ' meta2 ')]"):
        for credit in caption.xpath("./small"):
            # „… ausbilden lassen.<br>AFP“: Unterschrift und Nachweis getrennt wie in den übrigen <figcaption>
            credit.addprevious(credit.makeelement("br", {}))
        caption.tag = "p"   # FAZ: Bildunterschrift des Titelbilds im Kopf, trafilatura verwarf den <div>
        caption.set("data-kiosk-caption", "1")   # für _page_captions, falls trafilatura den Absatz verwirft
        changed = True
    pinned_posts = tree.xpath("//*[@id='liveblog-body']//*[@data-component='pinned-post']")
    if pinned_posts:
        # Echte Seiten haben einen angehefteten Eintrag; weitere fallen weg (je Eintrag die ganze Eintragsliste
        # zu lesen wäre auf feindlichen Seiten quadratisch: 500 Einträge und 2,9 MB Text kosteten 30 s).
        for extra in pinned_posts[1:]:
            if extra.getparent() is not None:
                extra.drop_tree()
        _pinned_post(pinned_posts[0], tree)
        changed = True
    for block in tree.xpath("//*[@id='liveblog-body']//article[starts-with(@id, 'block-')]"):
        block.tag = "section"
        for header in block.xpath("./header"):
            stamps = header.xpath(".//time/@datetime")
            stamp = _local_time(stamps[0]) if stamps else None
            for junk in header.xpath(".//*[@name='RelativeTime']"):
                junk.drop_tree()
            if not stamp:   # ohne datetime: die angezeigte feste Zeit („17.31 CEST“)
                stamp = next((t for t in (_normalize_ws(a.text_content()) for a in header.xpath(".//a")) if t), "")
            for child in list(header):
                if child.tag not in ("h2", "h3"):
                    child.drop_tree()   # Zeit-Link, Autorenname und -foto
            if stamp and len(stamp) <= 30 and re.search(r"\d", stamp):
                header.insert(0, _strong_para(header, stamp))
            header.tag = "div"
        for footer in block.xpath("./footer"):
            footer.drop_tree()
        for caption in block.xpath(".//figcaption"):
            caption.tag = "p"   # trafilatura verwirft <figcaption> in den Einträgen, einen Absatz nicht
        changed = True
    for div in tree.xpath(f"//*[{_cls('tik4-rich-text')}]/div[not(.//div or .//p or .//ul or .//ol or .//table "
                          "or .//blockquote or .//figure or .//h1 or .//h2 or .//h3 or .//h4 or .//h5 or .//h6)]"):
        _split_at_br(div)   # auch ohne <br>: aus dem <div> wird ein Absatz, wie bei den übrigen Einträgen
        changed = True
    for ts in tree.xpath(f"//*[{_cls('tik4-meta__ts')}]"):
        label = _local_time(ts.get("datetime"))
        if label and ts.getparent() is not None:
            para = _strong_para(ts, label)
            para.tail = ts.tail
            ts.getparent().replace(ts, para)
        else:
            ts.drop_tree()
        changed = True
    return changed


_PINNED_COVERED = 0.8   # so viele Absätze des angehefteten Eintrags stehen auch in der Eintragsliste


def _pinned_post(pinned, tree) -> None:
    """Guardian: Der angeheftete Eintrag ist meist eine Kopie eines Eintrags der Liste (dann weg: mit ihm
    wählte trafilatura nur einen Teil der Einträge). Steht sein Text nicht in der Liste (Schlussbeitrag
    „Closing post“), bleibt er als eigener Eintrag oben: sein <article> wird wie die übrigen behandelt."""
    blocks = [b for b in tree.xpath("//*[@id='liveblog-body']//article[starts-with(@id, 'block-')]")
              if not any(a is pinned for a in b.iterancestors())]
    timeline = _squash(" ".join(b.text_content() for b in blocks))   # einmal je Seite (nur ein Eintrag)
    paras = [_squash(el.text_content()) for el in pinned.xpath(".//p | .//li | .//h2 | .//h3 | .//blockquote")]
    paras = [x for x in paras if len(x) >= 20][:100]
    if not paras or sum(x[:60] in timeline for x in paras) >= _PINNED_COVERED * len(paras):
        pinned.drop_tree()
        return
    for junk in pinned.xpath("./input | ./div[not(.//article)]"):
        junk.drop_tree()   # Umschalter und „From 5h ago“
    for article in pinned.xpath(".//article[not(@id)]"):
        article.set("id", "block-pinned")


def _split_at_br(div) -> None:
    """<div>a<br>b<br><br>c</div> → <p>a</p><p>b</p><p>c</p> an derselben Stelle; leere Stücke fallen weg,
    der Text nach dem <div> (tail) bleibt auch dann. Linear: jedes Stück kommt mit addprevious vor den
    <div> (kein index/insert je Stück)."""
    parent = div.getparent()
    if parent is None:
        return
    paras = [div.makeelement("p", {})]
    paras[0].text = div.text
    for child in list(div):
        if isinstance(child.tag, str) and child.tag == "br":
            para = div.makeelement("p", {})
            para.text = child.tail
            paras.append(para)
        else:
            paras[-1].append(child)   # Element samt Text danach (tail) wandert mit
    for para in paras:
        if len(para) or (para.text or "").strip():
            div.addprevious(para)
    tail, div.tail = div.tail, None
    if tail:
        prev = div.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail
    parent.remove(div)


# heise schließt jede Meldung mit dem Kürzel der Redaktion („(fpi)“, „(mki, tobe)“) als eigenem Absatz.
_LAST_PARA_RE = re.compile(r"<p>((?:(?!<p>|</p>).){0,300})</p>\s*\Z", re.S)
_INITIALS_RE = re.compile(r"^\((?:[a-z]{2,5})(?:\s*,\s*[a-z]{2,5})*\)$")


def _drop_trailing_initials(clean: str) -> str:
    m = _LAST_PARA_RE.search(clean)
    if m and _INITIALS_RE.match(html_to_text(m.group(1))):
        return clean[:m.start()].rstrip()
    return clean


def _local_tz():
    """Zeitzone der App (KIOSK_TZ wie in config.Settings.tz; der Extraktions-Worker erbt die Umgebung),
    sonst config.DEFAULT_TZ (Europe/Berlin)."""
    for name in (os.environ.get("KIOSK_TZ", "").strip(), DEFAULT_TZ):
        if name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError, OSError):   # wie config._tz
                continue
    return timezone.utc


def _local_time(value: str | None) -> str | None:
    """ISO-Zeitpunkt („2026-09-28T12:43:55.000Z“) als „28.09. 14:43“ in der Zeitzone der App, sonst None.
    Immer mit Datum: „heute“ wäre im gespeicherten Text bald falsch."""
    if not value or not re.search(r"\d[T ]\d{1,2}:\d{2}", value):
        return None   # ohne Uhrzeit („2026-09-28“) keine Zeitzeile (sonst „28.09. 02:00“)
    try:
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(_local_tz()).strftime("%d.%m. %H:%M")
    except (ValueError, OverflowError, OSError):
        return None


def _strong_para(near, text: str):
    """<p><strong>text</strong></p> als neues Element im Baum von `near`."""
    para = near.makeelement("p", {})
    strong = near.makeelement("strong", {})
    strong.text = text
    para.append(strong)
    return para


def extract(page: bytes, url: str) -> Extraction:
    """Extrahiert den Artikel aus den rohen Bytes; trafilatura erkennt die Kodierung selbst."""
    tree, _ = _cleaned_tree(page)
    doc = tree if tree is not None else page

    body = trafilatura.extract(
        # Eine Kopie des Baums: trafilatura soll den für die Metadaten nicht verändern.
        copy.deepcopy(doc) if tree is not None else doc,
        url=url,
        output_format="html",
        include_images=True,
        include_links=True,
        include_formatting=True,
        include_tables=True,
        include_comments=False,
        favor_recall=True,
    )
    image_url = author = title = None
    try:
        # Ohne die ausführliche Datumssuche (htmldate/dateparser über jeden kurzen Text mit einer Zahl:
        # 12 000 solche Absätze kosteten 29 s); das Datum der Seite nutzt die App nicht.
        meta = trafilatura.extract_metadata(doc, default_url=url, extensive=False)
        if meta:
            image_url = safe_http_url(meta.image)
            author = clean_author(meta.author)
            title = meta.title
    except Exception:
        pass

    ld = _ld_objects(tree) if tree is not None else []
    page_urls = _page_urls(tree, url) if tree is not None else frozenset()
    paywalled = is_paywalled(page[:PAYWALL_SCAN_CHARS].decode("utf-8", errors="ignore"),
                             _ld_paywall(ld, page_urls, title))
    image_url = _preferred_image(image_url, ld)

    clean, text, truncated = sanitize_limited(body, url) if body else ("", "", False)
    if body and _PAYWALL_TEXT_RE.search(body):
        paywalled = True   # Le Monde: Hinweis im Text statt einer Markierung
    if text and body and _is_consent_wall(body):
        # Die Seite ist nur ein Einwilligungs-Dialog (Golem): kein Text, die Leseansicht zeigt den Anreißer.
        clean, text = "", ""
    if tree is not None and not paywalled:
        try:
            ld_clean, ld_text, ld_truncated = _ld_fallback(ld, text, url, title, page_urls)
        except Exception:  # noqa: BLE001
            ld_text = ""
        if ld_text:
            clean, text, truncated = ld_clean, ld_text, ld_truncated
    # Titel-Wiederholung vor dem Umordnen entfernen (sonst stünde sie hinter dem Titelbild), verglichen mit
    # og:title und der sichtbaren <h1> der Seite (SPIEGEL: og:title ist ein Suchmaschinen-Titel, die <h1>
    # aus Dachzeile und Überschrift steht als <h2> im Text)
    for page_title in (title, *(_page_headlines(tree) if tree is not None else ())):
        if text and page_title:
            echo_free = _drop_echo(clean, page_title)
            if echo_free != clean.strip() and html_to_text(echo_free):
                clean, text = echo_free, html_to_text(echo_free)
    if text and (urlparse(url).hostname or "").endswith("heise.de"):
        trimmed = _drop_trailing_initials(clean)
        if trimmed != clean and html_to_text(trimmed):
            clean, text = trimmed, html_to_text(trimmed)
    if text:
        arranged = _arrange_top(clean, _page_captions(tree) if tree is not None else {},
                                _page_description(tree) if tree is not None else "")
        if arranged != clean:
            clean, text = arranged, html_to_text(arranged)
    if text and tree is not None:
        clean, text = _with_lead(clean, text, tree, title)
    if text:
        unlabeled = _drop_liveblog_label(clean)
        if unlabeled != clean and html_to_text(unlabeled):
            clean, text = unlabeled, html_to_text(unlabeled)
    if not text:
        return Extraction("empty", None, None, 0, paywalled, image_url, author, title)
    status = "partial" if (paywalled or truncated or len(text) < MIN_FULL_CHARS) else "full"
    return Extraction(status, clean, text, len(text), paywalled, image_url, author, title)


# ---- Vorspann (Lead) -------------------------------------------------------------------------
# trafilatura verliert bei vielen Verlagen den Vorspann, weil er im Kopf des Artikels steht (SPIEGEL
# header[data-area=intro], ZEIT header .summary, DLF p.article-header-description, Handelsblatt
# hmg-lead-text, FOCUS div.Article-Description, n-tv StorylineLeadText, ORF p.story-lead-text, Daily
# Maverick div.top-summary). Allgemeine Regel: Die Seitenbeschreibung (og:description, sonst
# description, _LEAD_MIN bis _LEAD_MAX Zeichen, nicht bloß der Titel) steht nicht im Text, aber ein
# sichtbares Element außerhalb von Überschriften, Links, Listen, Bildunterschriften, Navigation und
# Fußzeile hat genau diesen Text (verglichen ohne Satzzeichen, Leerraum und Groß-/Kleinschreibung). Die
# SZ kürzt die Beschreibung, dort gilt p[data-manual=teaserText] direkt.
# Der Vorspann kommt nach führenden Überschriften und nach einem führenden Bild samt Bildunterschrift
# bzw. Bildnachweis: So bleibt das Bild vorn, und die Leseansicht erkennt es weiter als Titelbild (und
# zeigt es nicht zusätzlich zum Feed-Bild im Text).
_LEAD_MIN = 60
_LEAD_MAX = 1500
_LEAD_WORD_MAX = 40         # Suchwort höchstens so lang (ein riesiges „Wort“ bremst die XPath-Suche)
_LEAD_SKIP = {"a", "li", "figcaption", "nav", "footer", "button", "script", "style", "noscript", "template",
              "head", "title", "select", "option", "textarea", "h1", "h2", "h3", "h4", "h5", "h6"}
_LEAD_XPATH = "//p[@data-manual='teaserText']"
_LEAD_CANDIDATES = 50       # höchstens so viele Textknoten mit dem Suchwort
_LEAD_LEVELS = 6            # und von jedem höchstens so viele Ebenen nach oben
_LEADING_HEADINGS_RE = re.compile(r"^(?:\s*<(h[1-6])>.*?</\1>)*\s*", re.S)
# Führendes Bild: <figure> mit <img>, <img> (auch in einem Link oder einem eigenen Absatz), samt einer
# direkt folgenden <figcaption>.
_LEADING_IMAGE_RE = re.compile(
    r"(?:<figure>(?:(?!</figure>).)*?<img\b(?:(?!</figure>).)*</figure>"
    r"|<p>\s*(?:<a\b[^>]*>\s*)?<img\b[^>]*>\s*(?:</a>\s*)?</p>"
    r"|(?:<a\b[^>]*>\s*)?<img\b[^>]*>(?:\s*</a>)?)\s*(?:<figcaption>.*?</figcaption>\s*)?", re.S)
_CAPTION_MAX = 250          # kurzer Absatz direkt nach dem Bild: Bildunterschrift
_CAPTION_PARTS = 3          # höchstens so viele Teile (Bildunterschrift, „Foto:“, Nachweis)
_ALT_PREFIX_MIN = 40        # Alt-Text als Anfang der Unterschrift: mindestens so lang (_squash) …
_ALT_PREFIX_SHARE = 0.6     # … oder dieser Anteil des Absatzes („Donald Trump“ reicht nicht)
_NEXT_PARA_RE = re.compile(r"<p>((?:(?!</p>).)*)</p>\s*", re.S)
_LOOSE_TEXT_RE = re.compile(r"[^<]+")   # Text ohne Tag direkt nach dem Bild (Leerraum rechts in Python)
_STRONG_ONLY_RE = re.compile(r"^\s*<(strong|b)>(?:(?!</?\1>).)*</\1>\s*$", re.S)
_ALT_RE = re.compile(r'\salt="([^"]*)"')
_FIGCAPTION_RE = re.compile(r"<figcaption>(.*?)</figcaption>\s*", re.S)
# Keine Bildunterschrift trotz kurzem Absatz ohne Punkt: Zitat, Auslassung, Klammer am Ende, Ortsmarke
# einer Agenturmeldung („Berlin (dpa) –“).
_NOT_CAPTION_RE = re.compile(
    r"^[„“\"«»‚‘]|[„“\"«»‚‘’…)]$"
    r"|^[A-ZÄÖÜ][\w./ -]{0,40}\((?:dpa|AP|AFP|Reuters|KNA|epd|dts|rtr|sda|apa)[^)]*\)\s*[-–—]")


def _squash(text: str) -> str:
    """Nur Buchstaben und Ziffern, klein: für den Vergleich von Texten aus Seite und Extraktion."""
    return re.sub(r"\W+", "", text).casefold()


def _near(a: str, b: str) -> bool:
    """True, wenn zwei Texte (fast) gleich sind: gleich oder einer enthält den anderen zu ≥ 80 %."""
    a, b = _squash(a), _squash(b)
    if not a or not b:
        return False
    short, long_ = sorted((a, b), key=len)
    return short in long_ and len(short) >= 0.8 * len(long_)


def _skipped(el) -> bool:
    """True, wenn das Element oder ein Vorfahr in _LEAD_SKIP steht (Link, Liste, Überschrift …)."""
    return any(isinstance(a.tag, str) and a.tag in _LEAD_SKIP for a in (el, *el.iterancestors()))


def _short_text(el, limit: int) -> str | None:
    """Text des Elements, None wenn er (vor dem Zusammenfassen von Leerraum) länger als 2·limit ist; bricht
    früh ab, statt den Text großer Container ganz zusammenzusetzen."""
    parts, size = [], 0
    for piece in el.itertext():
        size += len(piece)
        if size > 2 * limit:
            return None
        parts.append(piece)
    return _normalize_ws("".join(parts))


def _find_lead(tree, text: str, title: str | None = None) -> str | None:
    have = _squash(text)
    for el in tree.xpath(_LEAD_XPATH):
        lead = None if _skipped(el) else _normalize_ws(el.text_content())
        if lead and _LEAD_MIN <= len(lead) <= _LEAD_MAX and not (title and _near(lead, title)):
            return lead if _squash(lead)[:80] not in have else None
    metas = (tree.xpath("//meta[@property='og:description']/@content")
             or tree.xpath("//meta[@name='description']/@content"))
    desc = _normalize_ws(metas[0]) if metas else ""
    # Gekürzte Beschreibung („… Namibia,…“, Daily Maverick): Das Element muss nur damit beginnen.
    cut = desc.endswith(("…", "..."))
    want = _squash(desc)
    if not _LEAD_MIN <= len(desc) <= _LEAD_MAX or not want or want[:80] in have:
        return None
    if title and _near(desc, title):
        return None   # nur die Überschrift: die steht separat
    # Kandidaten: Textknoten mit dem längsten Wort der Beschreibung (XPath, linear), von dort höchstens
    # _LEAD_LEVELS Ebenen nach oben, solange der Text nicht zu lang wird.
    words = re.findall(r"\w+", desc)
    if not words:
        return None
    word = max(words, key=len)[:_LEAD_WORD_MAX]
    limit = int(len(desc) * (4 if cut else 1.5)) + 50
    best = None
    for node in tree.xpath("//body//text()[contains(., $w)]", w=word)[:_LEAD_CANDIDATES]:
        el = node.getparent()
        if el is not None and node.is_tail:
            el = el.getparent()
        for _ in range(_LEAD_LEVELS):
            if el is None or not isinstance(el.tag, str) or el.tag in ("body", "html"):
                break
            lead = _short_text(el, limit)
            if lead is None:
                break   # weiter oben wird der Text nur länger
            found = _squash(lead)
            if (found == want or (cut and found.startswith(want))) and not _skipped(el):
                if best is None or len(lead) < len(best):
                    best = lead   # verschachtelte Treffer: das innerste Element
            el = el.getparent()
    return best


# Bildunterschrift, die mit einem Nachweis in Klammern endet (AP-Stil, Kyiv Independent: „…, 2026.
# (Nikoletta Stoyanova/Getty Images)“): Schrägstrich oder Wort eines Nachweises in der Klammer.
_TRAILING_CREDIT_RE = re.compile(
    r"\((?=[^()]{2,80}\)$)[^()]*(?:/|\b(?:Getty|Photo|Foto|AP|AFP|Reuters|EPA|Bild|dpa|IMAGO|Anadolu)\b)[^()]*\)$")


def _credit_label(text: str) -> bool:
    """„Foto: …“, „(Bild: …)“ als Bildnachweis – nicht eine Presseschau-Zeile „Bild: Die Regierung muss …“:
    der Rest ist kurz und enthält einen Schrägstrich oder endet ohne Satzzeichen."""
    if not CAPTION_RE.match(text):
        return False
    rest = text.split(":", 1)[1].strip().rstrip(")").strip()
    return len(rest) <= _CREDIT_REST_MAX and ("/" in rest or not rest.endswith((".", "!", "?")))


_CREDIT_REST_MAX = 60


def _is_credit(text: str) -> bool:
    return (_credit_label(text) or text.startswith("©")
            or (len(text) <= _CAPTION_MAX and bool(_TRAILING_CREDIT_RE.search(text))))


def _caption_match(text: str, alt: str, captions: Collection[str]) -> bool:
    """Sicherer Hinweis auf eine Bildunterschrift: Text gleich dem Alt-Text des Bildes, oder der kürzere
    ist der Anfang des längeren und lang genug (GEA kürzt den Alt-Text; ein Alt-Text „Donald Trump“ macht
    „Donald Trump hat erneut gedroht.“ nicht zur Unterschrift), oder er steht in einer <figcaption> der
    Seite (derStandard)."""
    sq = _squash(text)
    if len(sq) < 10:
        return False
    alt_sq = _squash(alt)
    if alt_sq == sq:
        return True
    short, long_ = sorted((sq, alt_sq), key=len)
    if short and long_.startswith(short) and (len(short) >= _ALT_PREFIX_MIN or
                                              len(short) >= _ALT_PREFIX_SHARE * len(long_)):
        return True
    return any(sq in c and (len(sq) >= _ALT_PREFIX_MIN or len(sq) >= _ALT_PREFIX_SHARE * len(c))
               for c in captions)


def _plain_caption(text: str, clean: str, after: int) -> bool:
    """Kurzer Absatz ohne Satzzeichen am Ende als Bildunterschrift – nicht bei Zitat, Auslassung, Klammer,
    Ortsmarke, Doppelpunkt, und nicht, wenn der nächste Absatz genauso aussieht (Gedicht, Aufzählung)."""
    if len(text) > _CAPTION_MAX or text.endswith(":") or "+++" in text or _NOT_CAPTION_RE.search(text):
        return False   # „+++“: Liveblog-Anreißer (FAZ), keine Unterschrift
    if text.rstrip("\"“”„«»‘’'").endswith((".", "!", "?")):
        return False
    nxt = _NEXT_PARA_RE.match(clean, after)
    if nxt and len(nxt.group(1)) <= 4 * _CAPTION_MAX:
        following = html_to_text(nxt.group(1))
        if following and len(following) <= _CAPTION_MAX and not following.rstrip("\"“”„«»‘’'").endswith(
                (".", "!", "?")) and not _is_credit(following):
            return False
    return True


def _caption_parts(clean: str, pos: int, alt: str, captions: Collection[str],
                   probe_second: bool = True, avoid: str = "") -> tuple[list[str], int, tuple[int, int] | None]:
    """Bildunterschrift und Bildnachweis nach einem Bild bei `pos`: ([innere HTML-Teile], Ende, Stelle
    eines späteren Absatzes, der nach vorn gezogen wird). Erkannt werden
    - eine <figcaption> oder ein Absatz, der zum Alt-Text bzw. einer <figcaption> der Seite passt;
    - ein Bildnachweis („Foto: …“, „(Bild: …)“, „© …“), auch als loser Text ohne Absatz (SPIEGEL:
      „<p>Unterschrift</p>Foto: X“) oder als „Foto:“ und ein kurzer Absatz mit dem Namen; nach einem
      Bildnachweis endet die Unterschrift;
    - direkt nach dem Bild ein kurzer Absatz ohne Satzzeichen am Ende (_plain_caption), außer nur fett
      (Uhrzeit eines Liveblog-Eintrags); ein Satz mit Punkt nur, wenn direkt ein Bildnachweis folgt (heise).
    Passt der erste Absatz nicht, aber der zweite sicher (Alt-Text, GEA), kommt der zweite nach vorn
    (nur mit probe_second)."""
    parts: list[str] = []
    while len(parts) < _CAPTION_PARTS:
        fig = _FIGCAPTION_RE.match(clean, pos)
        if fig:
            parts.append(fig.group(1).strip())
            pos = fig.end()
            continue
        loose = _LOOSE_TEXT_RE.match(clean, pos)
        if loose and loose.group(0).strip():
            raw = loose.group(0)
            text = html_to_text(raw)
            if len(text) > _CAPTION_MAX or not (_is_credit(text) or _caption_match(text, alt, captions)):
                break
            end = pos + len(raw)
            nxt = _NEXT_PARA_RE.match(clean, end)
            if text.endswith(":") and nxt and len(html_to_text(nxt.group(1))) <= _CAPTION_MAX // 2:
                parts.append(f"{raw.strip()} {nxt.group(1).strip()}")   # „Foto:“ + Name
                pos = nxt.end()
            else:
                parts.append(raw.strip())
                pos = end
            if _is_credit(text):
                break
            continue
        para = _NEXT_PARA_RE.match(clean, pos)
        if not para or "<img" in para.group(1) or len(para.group(1)) > 4 * _CAPTION_MAX:
            break
        inner = para.group(1)
        text = html_to_text(inner)
        if not text or _STRONG_ONLY_RE.match(inner) or (avoid and _near(text, avoid)):
            break   # Liveblog-Uhrzeit oder der Vorspann der Seite (og:description): keine Unterschrift
        if _is_credit(text):
            nxt = _NEXT_PARA_RE.match(clean, para.end())
            if text.endswith(":") and nxt and len(html_to_text(nxt.group(1))) <= _CAPTION_MAX // 2:
                parts.append(f"{inner.strip()} {nxt.group(1).strip()}")
                pos = nxt.end()
            elif text.endswith(":"):
                break
            else:
                parts.append(inner.strip())
                pos = para.end()
            break   # nach dem Bildnachweis ist die Unterschrift zu Ende
        short = len(text) <= _CAPTION_MAX
        ok = short and (_caption_match(text, alt, captions)
                        or (not parts and _plain_caption(text, clean, para.end())))
        if not ok and short:
            nxt = _NEXT_PARA_RE.match(clean, para.end())
            ok = bool(nxt and len(nxt.group(1)) <= 4 * _CAPTION_MAX and _is_credit(html_to_text(nxt.group(1))))
        if not ok and probe_second and not parts:
            second = _NEXT_PARA_RE.match(clean, para.end())
            if (second and "<img" not in second.group(1) and len(second.group(1)) <= 4 * _CAPTION_MAX
                    and _caption_match(html_to_text(second.group(1)), alt, captions)):
                return [second.group(1).strip()], pos, (second.start(), second.end())
        if not ok:
            break
        parts.append(inner.strip())
        pos = para.end()
    return parts, pos, None


def _image_block(clean: str, pos: int, captions: Collection[str],
                 probe_second: bool = True, avoid: str = "") -> tuple[int, str, str] | None:
    """Bild bei `pos` samt Bildunterschrift als (Ende, neues HTML, Text dazwischen): das Bild mit einer
    einzigen <figcaption> (Teile mit <br> getrennt) in einer <figure> – trafilatura liefert nie <figure>,
    und nur so findet die Leseansicht die Unterschrift zum Bild. „Text dazwischen“ ist nicht leer, wenn
    eine spätere Unterschrift nach vorn gezogen wurde (GEA): Er gehört hinter das Bild. None ohne Bild."""
    m = _LEADING_IMAGE_RE.match(clean, pos)
    if not m:
        return None
    block = m.group(0).rstrip()
    if "<figcaption>" in block:
        return m.end(), block, ""   # schon als Abbildung mit Unterschrift gegliedert
    alt_m = _ALT_RE.search(block)
    parts, end, moved = _caption_parts(clean, m.end(), html.unescape(alt_m.group(1)) if alt_m else "", captions,
                                       probe_second, avoid)
    if not parts and alt_m:
        # Keine Unterschrift im Text, aber eine auf der Seite, die mit dem Alt-Text beginnt (FAZ-Kopf, den
        # trafilatura verwarf): die nehmen
        alt_sq = _squash(html.unescape(alt_m.group(1)))
        if len(alt_sq) >= _ALT_PREFIX_MIN and isinstance(captions, dict):
            found = next((t for c, t in captions.items() if c.startswith(alt_sq)), None)
            if found:
                parts = [html.escape(part, quote=False) for part in found.split(_CAPTION_SEP)]
    if len(parts) == 1 and isinstance(captions, dict):
        # Nur der Bildnachweis kam an (taz: trafilatura verwarf den Text der <figcaption>, behielt den
        # Nachweis-Absatz darin): die ganze Unterschrift der Seite nehmen, die diesen Nachweis enthält
        credit = html_to_text(parts[0])
        credit_sq = _squash(credit)
        if _is_credit(credit) and len(credit_sq) >= 5:
            full = next((t.replace(_CAPTION_SEP, " ") for c, t in captions.items()
                         if credit_sq in c and len(c) > len(credit_sq) + 10 and len(t) <= 2 * _CAPTION_MAX), None)
            if full and credit in full:
                rest = _normalize_ws(full.replace(credit, " "))
                if rest:
                    parts = [html.escape(rest, quote=False), parts[0]]
    if not parts:
        return m.end(), block, ""
    caption = "<figcaption>" + "<br>".join(parts) + "</figcaption>"
    if block.startswith("<figure>"):
        new = block[:-len("</figure>")] + caption + "</figure>"
    else:
        if block.startswith("<p>"):
            block = block[3:-4].strip()   # <p><img></p>: das Bild gehört in die <figure>
        new = f"<figure>{block}{caption}</figure>"
    gap = ""
    if moved:
        # nach vorn gezogene Unterschrift: der Text dazwischen bleibt, sie selbst fällt dort weg
        start, stop = moved
        gap, end = clean[m.end():start], stop
    return end, new, gap


def _lead_position(clean: str) -> int:
    """Stelle für den Vorspann: nach führenden Überschriften und einem führenden Bild samt <figcaption>."""
    pos = _LEADING_HEADINGS_RE.match(clean).end()
    m = _LEADING_IMAGE_RE.match(clean, pos)
    return m.end() if m else pos


_INTRO_MAX = 600    # Textlänge eines Vorspann-Absatzes vor dem Titelbild (heise, taz, Guardian)


def _arrange_top(clean: str, captions: Collection[str] = frozenset(), avoid: str = "") -> str:
    """Anfang des Textes ordnen: Steht vorn (nach Überschriften) ein kurzer Absatz (Vorspann) und direkt
    danach ein Bild, kommt das Bild nach vorn: Die Leseansicht erkennt nur ein Bild am Anfang als
    Titelbild, sonst zeigt sie das Feed-Bild und das Bild im Text doppelt (heise nennt jede Bildgröße mit
    anderer Adresse). Die Bildunterschrift des führenden Bildes kommt in eine <figcaption> (_image_block).
    Im Fall mit Vorspann wird keine spätere Unterschrift nach vorn gezogen: der Vorspann bliebe sonst
    hinter dem ersten Textabsatz."""
    pos = _LEADING_HEADINGS_RE.match(clean).end()
    block = _image_block(clean, pos, captions, avoid=avoid)
    if block:
        end, new, gap = block
        return clean[:pos] + new + gap + clean[end:]
    para = _NEXT_PARA_RE.match(clean, pos)
    if not para or "<img" in para.group(1) or len(para.group(1)) > 4 * _INTRO_MAX:
        return clean
    if not 0 < len(html_to_text(para.group(1))) <= _INTRO_MAX:
        return clean
    block = _image_block(clean, para.end(), captions, probe_second=False, avoid=avoid)
    if block is None:
        return clean
    end, new, gap = block
    return clean[:pos] + new + clean[pos:para.end()] + gap + clean[end:]


def _page_headlines(tree) -> list[str]:
    """Sichtbare Überschrift der Seite: die erste <h1> und die erste <h1>/<h2> im Kopf des Artikels (SPIEGEL:
    <h1> trägt den Suchmaschinen-Titel, die gezeigte Überschrift aus Dachzeile und Titel ist ein <h2> im
    <header>); je höchstens 300 Zeichen."""
    out: list[str] = []
    for xpath in ("//body//h1", "//article//header//h1 | //article//header//h2 | //main//header//h2"):
        for el in tree.xpath(xpath)[:1]:
            text = _short_text(el, 300)
            if text and len(text) >= 10 and text not in out:
                out.append(text)
    return out


def _page_description(tree) -> str:
    """og:description bzw. description der Seite (ein Vorspann ist keine Bildunterschrift)."""
    metas = (tree.xpath("//meta[@property='og:description']/@content")
             or tree.xpath("//meta[@name='description']/@content"))
    return _normalize_ws(metas[0])[:_LEAD_MAX] if metas else ""


_CAPTION_SEP = "\n"          # trennt in _page_captions Unterschrift und Nachweis (FAZ-Kopf, dort ein <br>)


def _page_captions(tree) -> dict[str, str]:
    """Bildunterschriften der Seite (<figcaption>, FAZ-Kopf; höchstens 200): zusammengedrückt (_squash) →
    Text. Beim FAZ-Kopf (aus _fix_tree) sind die Teile am <br> mit _CAPTION_SEP getrennt: Sie kommen
    wie die übrigen Teile einer Unterschrift mit <br> in die <figcaption>."""
    out: dict[str, str] = {}
    for el in tree.xpath("//figcaption | //p[@data-kiosk-caption]")[:200]:
        if len(el.text_content()) > 2000:
            continue
        if el.get("data-kiosk-caption"):
            pieces: list[list[str]] = [[]]
            for node in el.xpath(".//text() | .//br"):
                if isinstance(node, str):
                    pieces[-1].append(node)
                else:
                    pieces.append([])
            text = _CAPTION_SEP.join(p for p in (_normalize_ws(" ".join(x)) for x in pieces) if p)
        else:
            # Teile mit Leerzeichen verbinden: Unterschrift und Nachweis stehen oft in eigenen Elementen
            # („… EU-Verträge.“<span>Peter Schneider / Keystone</span>, NZZ)
            text = _normalize_ws(" ".join(el.itertext()))
        if text:
            out[_squash(text)] = text
    return out


# ZEIT-Liveblogs: Der Vorspann (auch og:description) endet mit dem Etikett „Das Liveblog“ („… Mehrere Tote nach
# Raketenangriffen. Das Liveblog“). Nur im ersten Absatz und nur nach einem Satzende.
_FIRST_PARA_RE = re.compile(r"<p>((?:(?!</p>).)*)</p>", re.S)
_LIVEBLOG_LABEL_RE = re.compile(r"(?<=[.!?…“”\"])\s+Das Liveblog\s*\Z")


def _drop_liveblog_label(clean: str) -> str:
    m = _FIRST_PARA_RE.search(clean)
    if not m:
        return clean
    inner = _LIVEBLOG_LABEL_RE.sub("", m.group(1))
    return clean if inner == m.group(1) else f"{clean[:m.start(1)]}{inner}{clean[m.end(1):]}"


def _with_lead(clean: str, text: str, tree, title: str | None = None) -> tuple[str, str]:
    try:
        lead = _find_lead(tree, text, title)
    except Exception:  # noqa: BLE001
        return clean, text
    if not lead:
        return clean, text
    piece = f"<p>{html.escape(lead, quote=False)}</p>"
    if len(clean) + len(piece) > MAX_BODY_HTML:
        return clean, text
    pos = _lead_position(clean)
    clean = clean[:pos] + piece + clean[pos:]
    return clean, html_to_text(clean)


# ---- JSON-LD ---------------------------------------------------------------------------------
_LD_MAX_CHARS = 2_000_000   # längere JSON-LD-Blöcke werden nicht gelesen


def _ld_objects(tree) -> list[dict]:
    """Alle Objekte aus den JSON-LD-Blöcken der Seite (auch verschachtelte, @graph)."""
    out: list[dict] = []

    def walk(o, depth=0):
        if depth > 20:
            return
        if isinstance(o, dict):
            out.append(o)
            for v in o.values():
                walk(v, depth + 1)
        elif isinstance(o, list):
            for v in o:
                walk(v, depth + 1)

    try:
        scripts = tree.xpath("//script[@type='application/ld+json']/text()")
    except Exception:  # noqa: BLE001
        return out
    for raw in scripts:
        if len(raw) > _LD_MAX_CHARS or "{" not in raw:
            continue
        try:
            walk(json.loads(raw, strict=False))   # Steuerzeichen in Zeichenketten (Zeilenumbrüche) zulassen
        except (ValueError, RecursionError):
            continue
    return out


# Fallback auf articleBody aus dem JSON-LD (des Artikels der Seite, _ld_article_body), wenn trafilatura
# (fast) nur Seitenrahmen erwischt hat (Times of India: „Sign In“, „Comments“, „Share“ …): articleBody ist
# mindestens _LD_RATIO-mal so lang wie der extrahierte Text oder kaum ein Satz daraus (unter
# _LD_COVERAGE) steht im Text. Nie für Bezahlartikel (SCMP liefert dort den ganzen Text, zeigt ihn aber
# nicht). Absätze aus Zeilenumbrüchen; ein langer Block ohne Umbruch wird an Satzgrenzen in Absätze von
# etwa _LD_PARA_CHARS Zeichen geteilt.
_LD_MIN = 200
_LD_RATIO = 1.3
_LD_COVERAGE = 0.5
_LD_PARA_CHARS = 600
_LD_CANDIDATES = 50         # höchstens so viele Objekte mit articleBody werden geprüft
_LD_RELEVANT = 0.6          # so viele Sätze des extrahierten Textes stehen im articleBody (ohne URL/Titel)
_SENTENCE_END_RE = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][\"“”’»«]))\s+(?=[\"„“«»(]?[A-ZÄÖÜ0-9])")


def _url_key(raw) -> str:
    """URL zum Vergleichen: ohne Schema, Query, Fragment und Schrägstrich am Ende, Host klein."""
    if not isinstance(raw, str):
        return ""
    raw = re.sub(r"[?#].*\Z", "", raw.strip(), flags=re.S)
    raw = re.sub(r"^[a-z][a-z0-9+.-]*://", "", raw, flags=re.I).rstrip("/")
    host, slash, path = raw.partition("/")
    return host.lower().removeprefix("www.") + slash + path


def _ld_urls(o: dict) -> set[str]:
    urls = set()
    for key in ("url", "@id", "mainEntityOfPage"):
        value = o.get(key)
        if isinstance(value, dict):
            value = value.get("@id") or value.get("url")
        if _url_key(value):
            urls.add(_url_key(value))
    return urls


def _clean_ld_body(raw: str) -> str:
    if "<" in raw and _TAG_RE.search(raw):
        raw = re.sub(r"(?i)<br\s*/?>|</p\s*>", "\n", raw)
        raw = _TAG_RE.sub(" ", raw)
    raw = html.unescape(raw)
    if "\n" not in raw:
        # Absätze ohne Umbruch aneinandergehängt (Times of India: „… top court.The collegium …“): Punkt direkt
        # vor einem großgeschriebenen Wort wird ein Absatzwechsel. Nicht nach Abkürzungen („Dr.Müller“),
        # Ziffern („3.Liga“), einem Großbuchstaben („U.S.Army“) und nicht in Adressen („www.Example.com“).
        raw = _RUN_TOGETHER_RE.sub(_run_together, raw)
    return raw


# Linear: Anfang nur am Wortanfang, der Blick nach vorn ist begrenzt (sonst „ab.Cd“ × N quadratisch).
_RUN_TOGETHER_RE = re.compile(r"(?<!\w)(\w+)([.!?])([\"”’]?)(?=([A-ZÄÖÜ][a-zäöüß]{1,40})(\S{0,40}))")
_ABBREVIATIONS = {"dr", "prof", "st", "nr", "mr", "mrs", "ms", "jr", "sr", "vs", "ca", "bzw", "usw", "etc",
                  "inc", "ltd", "co", "no", "www", "gen", "col", "gov", "sen", "rep", "hr", "fr", "str"}


def _run_together(m: re.Match) -> str:
    word, mark, quote, _, rest = m.groups()
    if (not word[-1:].islower() or word.casefold() in _ABBREVIATIONS or any(c.isdigit() for c in word)
            or (mark == "." and "." in rest)):
        return m.group(0)
    return f"{word}{mark}{quote}\n"


def _ld_article_body(ld: list[dict], text: str = "", title: str | None = None,
                     page_urls: frozenset[str] = frozenset()) -> str:
    """articleBody des Artikels der Seite: Das Objekt gehört zur Seite (URL wie Seite, og:url oder
    canonical; Überschrift wie der Titel) oder enthält ≥ _LD_RELEVANT der Sätze des extrahierten Textes.
    Von mehreren passenden das längste; ohne passendes "" (nicht einfach das längste irgendwo, etwa eines
    empfohlenen Artikels)."""
    best = ""
    keys = _sentence_keys(text)   # Sätze des Textes einmal, nicht je Objekt
    candidates = [o for o in ld if isinstance(o.get("articleBody"), str)][:_LD_CANDIDATES]
    for o in candidates:
        raw = o["articleBody"]
        if len(raw) <= len(best):
            continue
        headline = o.get("headline") or o.get("name")
        relevant = (bool(_ld_urls(o) & page_urls)
                    or (isinstance(headline, str) and bool(title) and _near(headline, title)))
        if not relevant and keys:
            have = _squash(raw)
            relevant = sum(1 for k in keys if k in have) / len(keys) >= _LD_RELEVANT
        if relevant:
            best = raw
    return _clean_ld_body(best) if best else ""


def _ld_paragraphs(raw: str) -> list[str]:
    paras = [_normalize_ws(p) for p in re.split(r"\n", raw)]
    out: list[str] = []
    for para in paras:
        if not para:
            continue
        if len(para) <= 2 * _LD_PARA_CHARS:
            out.append(para)
            continue
        chunk = ""
        for sentence in _SENTENCE_END_RE.split(para):
            chunk = f"{chunk} {sentence}".strip()
            if len(chunk) >= _LD_PARA_CHARS:
                out.append(chunk)
                chunk = ""
        if chunk:
            out.append(chunk)
    return out


def _sentence_keys(text: str) -> list[str]:
    """Anfänge (50 Zeichen, _squash) der Sätze ab 40 Zeichen, höchstens der ersten 100."""
    return [_squash(s)[:50] for s in _SENTENCE_END_RE.split(_normalize_ws(text)) if len(s) >= 40][:100]


def _coverage(raw: str, text: str) -> float:
    """Anteil der Sätze aus `raw` (_sentence_keys), deren Anfang im Text steht; 1.0 ohne solche Sätze."""
    keys = _sentence_keys(raw)
    if not keys:
        return 1.0
    have = _squash(text)
    return sum(1 for k in keys if k in have) / len(keys)


# Paywall aus dem JSON-LD: das Objekt des Artikels der Seite entscheidet. Reihenfolge: Artikel-Objekt
# (@type …Article, BlogPosting, LiveBlogPosting) mit URL der Seite oder passender Überschrift, sonst das
# erste Artikel-Objekt mit Angabe, sonst ein beliebiges Objekt mit URL der Seite. Werte: false/"False" →
# Paywall, true/"True" → frei, anderes (CSM: "http://schema.org/False" auf jeder Seite) → keine Angabe.
# Ohne verwertbares Objekt entscheidet is_paywalled nach dem ersten Vorkommen im Seitentext.
_LD_PAYWALL_MAX = 500       # höchstens so viele JSON-LD-Objekte werden geprüft


def _is_article(o: dict) -> bool:
    types = o.get("@type")
    types = types if isinstance(types, list) else [types]
    return any(isinstance(t, str) and (t.endswith("Article") or t.endswith("BlogPosting")) for t in types)


def _access_value(value) -> bool | None:
    """True = Paywall, False = frei, None = keine verwertbare Angabe."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, str) and value.strip().casefold() in ("true", "false"):
        return value.strip().casefold() == "false"
    return None


def _flag(o: dict) -> bool | None:
    """Angabe des Objekts und seiner Teile (hasPart: WebPageElement mit isAccessibleForFree, wie Google es für
    Bezahltexte vorschlägt): Irgendeine Paywall-Angabe gewinnt; sonst True/False wie angegeben; None ohne."""
    values = [_access_value(o.get("isAccessibleForFree"))]
    parts = o.get("hasPart")
    for part in parts if isinstance(parts, list) else [parts]:
        if isinstance(part, dict):
            values.append(_access_value(part.get("isAccessibleForFree")))
    if True in values:
        return True
    return False if False in values else None


def _is_page(o: dict) -> bool:
    types = o.get("@type")
    types = types if isinstance(types, list) else [types]
    return any(isinstance(t, str) and t.endswith("Page") for t in types)


def _ld_paywall(ld: list[dict], page_urls: frozenset[str], title: str | None) -> bool | None:
    """Reihenfolge: eigene Objekte – Artikel oder Seite (WebPage …) mit URL der Seite (url, @id,
    mainEntityOfPage) bzw. passender Überschrift, oder ein anderes Objekt mit url/mainEntityOfPage der Seite
    (ein Listeneintrag einer BreadcrumbList mit der Seite als @id zählt nicht). Eine Angabe eines eigenen
    Objekts entscheidet (Artikel vor anderen). Eigene Objekte ohne Angabe: None, is_paywalled sucht dann im
    Seitentext. Ohne eigenes Objekt: das erste Artikel-Objekt mit Angabe, sonst None."""
    objects = ld[:_LD_PAYWALL_MAX]

    def own(o: dict) -> bool:
        if _is_article(o) or _is_page(o):
            headline = o.get("headline")
            return bool(_ld_urls(o) & page_urls) or (isinstance(headline, str) and bool(title)
                                                     and _near(headline, title))
        explicit = {_url_key(o.get("url"))}
        entity = o.get("mainEntityOfPage")
        explicit.add(_url_key(entity.get("@id") or entity.get("url")) if isinstance(entity, dict) else _url_key(entity))
        return bool((explicit - {""}) & page_urls)

    mine = [o for o in objects if own(o)]
    if mine:
        for o in sorted(mine, key=lambda o: not _is_article(o)):
            value = _flag(o)
            if value is not None:
                return value
        return None
    for o in objects:
        if _is_article(o):
            value = _flag(o)
            if value is not None:
                return value
    return None


def _page_urls(tree, url: str) -> frozenset[str]:
    urls = [url] + tree.xpath("//meta[@property='og:url']/@content") + tree.xpath("//link[@rel='canonical']/@href")
    return frozenset(k for k in map(_url_key, urls) if k)


def _ld_fallback(ld: list[dict], text: str, url: str, title: str | None = None,
                 page_urls: frozenset[str] = frozenset()) -> tuple[str, str, bool]:
    raw = _ld_article_body(ld, text, title, page_urls)
    size = len(_normalize_ws(raw))
    if size < _LD_MIN:
        return "", "", False
    if size < _LD_RATIO * len(text) and (size < 0.5 * len(text) or _coverage(raw, text) >= _LD_COVERAGE):
        return "", "", False
    paras = _ld_paragraphs(raw)
    fragment = "".join(f"<p>{html.escape(p, quote=False)}</p>" for p in paras)
    return sanitize_limited(fragment, url)


# ---- Titelbild ------------------------------------------------------------------------------
# Guardian brennt ins og:image ein Logo ein (Parameter overlay-base64); das JSON-LD nennt dasselbe Bild
# ohne Logo (fit=max bevorzugt). Meduza liefert als Bild eine Texttafel mit der Überschrift
# (/imgly/share/), das zählt als kein Bild.


def is_text_card_image(url: str | None) -> bool:
    return bool(url) and "/imgly/share/" in url


def _ld_images(ld: list[dict]) -> list[str]:
    urls: list[str] = []
    for o in ld:
        img = o.get("image")
        for item in img if isinstance(img, list) else [img]:
            if isinstance(item, dict):
                item = item.get("url") or item.get("contentUrl")
            if isinstance(item, str):
                urls.append(item)
    return urls


def _preferred_image(image_url: str | None, ld: list[dict]) -> str | None:
    if is_text_card_image(image_url):
        return None
    if image_url and "overlay-base64" in image_url:
        path = image_url.split("?", 1)[0]
        same = [u for u in _ld_images(ld) if u.split("?", 1)[0] == path and "overlay" not in u]
        same.sort(key=lambda u: "fit=max" not in u)
        if same:
            return safe_http_url(same[0]) or image_url
    return image_url


# ---- Autor ----------------------------------------------------------------------------------
# trafilatura liest den Autor aus Metadaten und Autorenzeilen; dabei kommen Etiketten („Autor“,
# „Redaktor“ bei SRF, „No author“ beim Handelsblatt), statt einer Person der Name der Website
# („Tagesschau De“, „N-Tv Nachrichten“, „Heise Online“, „ORF at“, „DER SPIEGEL“; nur bekannte Namen,
# „Maria De“ bleibt) und beim Guardian die Funktion hinter dem Namen („Ian Sample Science editor“) mit.
# Mehrere Namen trennt trafilatura mit „; “, Feeds mit „und“/„and“; jeder Name wird einzeln geprüft. Lange
# Listen (SZ-Liveblog mit 14 Namen) werden nach _AUTHOR_MAX_NAMES Namen mit „u. a.“ gekürzt.
_AUTHOR_JUNK = {"no author", "autor", "autorin", "redaktor", "redaktorin"}
_AUTHOR_LABEL_RE = re.compile(r"^(?:autor(?:in)?|redaktor(?:in)?)\s+|\s+(?:autor(?:in)?|redaktor(?:in)?)$", re.I)
SITE_NAMES = {
    "tagesschau de", "tagesschau", "ndr de", "wdr de", "br de", "mdr de", "swr de", "hr de", "zdf de",
    "deutschlandfunk de", "deutschlandfunk", "deutschlandfunk kultur", "n-tv nachrichten", "n-tv de", "n-tv",
    "heise online", "heise autos", "heise security", "orf at", "orf", "der spiegel", "spiegel", "spiegel online",
    "zeit online", "die zeit", "welt", "die welt", "faz", "faz net", "süddeutsche zeitung", "sz de",
    "tagesspiegel", "der tagesspiegel", "handelsblatt", "focus online", "golem de", "der standard",
    "derstandard at", "srf", "srf news", "nzz", "taz", "moscow times", "the moscow times", "cna",
    "channel newsasia", "the straits times", "straits times", "le monde", "meduza",
    "the christian science monitor", "christian science monitor",
}
_AUTHOR_MAX_NAMES = 3
_AUTHOR_INPUT_MAX = 300
# Funktion am Ende (Guardian): „Ian Sample Science editor“, „Martin Chulov Middle East correspondent“,
# „Shaun Walker Central and eastern Europe correspondent“. Die Funktion beginnt beim ersten Wort (ab dem
# dritten), ab dem bis zur Funktion nur kleingeschriebene Wörter oder bekannte Zusätze (Ressorts, Regionen)
# stehen; so bleiben dreiteilige Namen („Mary Ann Smith political editor“ → „Mary Ann Smith“). Besteht der
# Teil nur aus Zusätzen und Funktion („Home Affairs Correspondent“, „The Editor“), fällt er weg; „Mark
# Reporter“ (Name mit zwei Wörtern) bleibt.
_ROLE_WORDS = {"editor", "correspondent", "reporter", "writer", "columnist", "critic", "commentator",
               "analyst", "journalist", "producer", "photographer", "presenter", "contributor"}
_ROLE_QUALIFIERS = {
    "the", "and", "&", "of", "for", "at", "large", "chief", "senior", "deputy", "associate", "assistant",
    "special", "staff", "contributing", "home", "foreign", "affairs", "political", "politics", "science",
    "economics", "economic", "economy", "business", "technology", "tech", "environment", "environmental",
    "climate", "health", "education", "sport", "sports", "arts", "culture", "media", "defence", "defense",
    "security", "diplomatic", "legal", "crime", "social", "community", "transport", "religious", "religion",
    "investigations", "investigative", "data", "consumer", "money", "travel", "food", "fashion", "music", "film",
    "books", "opinion", "digital", "global", "world", "international", "middle", "east", "west", "north",
    "south", "central", "eastern", "western", "northern", "southern", "europe", "european", "asia", "asian",
    "africa", "african", "america", "american", "latin", "us", "uk", "australia", "china", "india", "ireland",
    "scotland", "wales", "england", "washington", "moscow", "brussels", "paris", "berlin", "beijing", "london",
    "new", "york", "pacific", "nordic", "gulf",
}
# „Shrai Popat (now) and Tom Ambrose (earlier)“, „Angelique Chrisafis in Paris“
# Auf Text mit einfachen Leerzeichen (_normalize_ws) angewandt: kein \s* vor einer Klammer, das bei langem
# Leerraum quadratisch würde.
_AUTHOR_NOTE_RE = re.compile(r" ?\((?:now|earlier|later|updates?)\)", re.I)
_AUTHOR_PLACE_RE = re.compile(r" in [A-ZÄÖÜ][\w'-]+(?: [A-ZÄÖÜ][\w'-]+)?$")
_AUTHOR_AND_RE = re.compile(r" (und|and|&) (?=[A-ZÄÖÜ])")
# Sender und Studio hinter dem Namen: „Ilanit Spinner, ARD Berlin“, „Anna Muster, BR“
_BROADCASTER_RE = re.compile(
    r", ?(?:ARD|ZDF|NDR|WDR|BR|SWR|MDR|HR|RBB|SR|DLF|Deutschlandfunk|ORF|SRF|DW)(?:[- ][A-ZÄÖÜ][\w-]*){0,2}(?=;|$)")


def _strip_role(part: str) -> str:
    words = part.split()
    if len(words) < 2 or words[-1].casefold().rstrip("s") not in _ROLE_WORDS:
        return part
    role = len(words) - 1
    if all(w.casefold() in _ROLE_QUALIFIERS for w in words[:role]):
        return ""   # nur eine Funktion
    for start in range(2, role + 1):
        if all(w[:1].islower() or w.casefold() in _ROLE_QUALIFIERS for w in words[start:role]):
            return " ".join(words[:start])
    return part


def _tidy_name(piece: str) -> str:
    """Ort („in Paris“) und Funktion am Ende weg, Satzzeichen am Rand weg."""
    return _strip_role(_AUTHOR_PLACE_RE.sub("", piece.strip(" ,;:"))).strip(" ,;:")


def _author_pieces(raw: str) -> list[str]:
    """Namen einzeln: getrennt an „;“; an „und“/„and“/„&“ nur zwischen zwei Namen aus je mindestens zwei
    Wörtern („Johnson & Johnson“ bleibt); an Kommas, wenn jedes Stück ein Name aus mindestens zwei Wörtern
    ist („Julia Surkowa, Gaby Coldewey“; „Von der Leyen, Ursula“ bleibt ganz). Die Funktion wird zuerst am
    ganzen Teil entfernt (sie kann selbst „and“ enthalten: „Central and eastern Europe correspondent“),
    dann je Stück („Jane Doe, Senior correspondent“)."""
    pieces: list[str] = []
    for part in _AUTHOR_NOTE_RE.sub("", _normalize_ws(raw)).split(";"):
        part = _tidy_name(_BROADCASTER_RE.sub("", part))
        split = _AUTHOR_AND_RE.split(part)   # [Name, Trenner, Name, Trenner, …]
        chunks = [split[0]]
        for sep, chunk in zip(split[1::2], split[2::2]):
            if len(chunks[-1].split()) < 2 or len(chunk.split()) < 2:
                chunks[-1] = f"{chunks[-1]} {sep} {chunk}"   # kein Namenspaar: zusammen lassen
            else:
                chunks.append(chunk)
        for chunk in chunks:
            commas = [c for c in (_tidy_name(c) for c in chunk.split(",")) if c]
            if len(commas) > 1 and all(len(c.split()) >= 2 and c[:1].isupper() for c in commas):
                pieces.extend(commas)
            elif commas:
                pieces.append(", ".join(commas))
    return pieces


def clean_author(raw: str | None) -> str | None:
    """Autorenzeile aufräumen (siehe oben); mehrere Namen mit „, “ getrennt, nach _AUTHOR_MAX_NAMES
    Namen „u. a.“."""
    if is_placeholder(raw):
        return None
    if len(raw) > _AUTHOR_INPUT_MAX:
        # Autorenzeilen sind kurz; längere werden an einem Trenner gekürzt (die Regeln unten sind nicht linear)
        cut = raw[:_AUTHOR_INPUT_MAX]
        raw = cut[:max(cut.rfind(";"), cut.rfind(","))] if max(cut.rfind(";"), cut.rfind(",")) > 0 else cut
    parts: list[str] = []
    for part in _author_pieces(raw):
        part = _normalize_ws(part)
        for _ in range(2):   # vorn und hinten ein Etikett („Autor Tobias Müller Redaktor“)
            part = _AUTHOR_LABEL_RE.sub("", part).strip()
        part = _strip_role(_AUTHOR_PLACE_RE.sub("", part))
        if (not part or part.casefold() in _AUTHOR_JUNK or part.casefold() in SITE_NAMES
                or is_placeholder(part)):
            continue
        if part not in parts:
            parts.append(part)
    extra = len(parts) > _AUTHOR_MAX_NAMES
    names: list[str] = []
    for part in parts[:_AUTHOR_MAX_NAMES]:
        if len(", ".join(names + [part])) > 190:
            extra = True
            break
        names.append(part)
    return (", ".join(names) + (" u. a." if extra and names else "")) or None


_TAG_RE = re.compile(r"<[^>]+>")


PLACEHOLDERS = {"none", "null", "undefined"}


def is_placeholder(text: str | None, title: str | None = None) -> bool:
    """True für leere Werte und Platzhalter wie "None"/"null"/"undefined" (manche Feeds liefern
    die als Text) sowie für einen Wert, der nur den Titel wiederholt."""
    t = (text or "").strip().lower()
    if not t or t in PLACEHOLDERS:
        return True
    return title is not None and t == title.strip().lower()


def strip_html(raw: str | None, limit: int = 700) -> str:
    if not raw:
        return ""
    text = _normalize_ws(html.unescape(_TAG_RE.sub(" ", raw)))
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text
