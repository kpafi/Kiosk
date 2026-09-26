"""Volltext-Extraktion aus Artikelseiten und Bereinigung des HTML."""
from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlparse

import trafilatura

# Schema.org-Markierung, die viele Verlage (Spiegel, NYT, Zeit ...) für Bezahlartikel setzen,
# als JSON-LD ("isAccessibleForFree": false) oder als Attribut/Microdata.
PAYWALL_RE = re.compile(r"""isaccessibleforfree["']?\s*[:=]\s*["']?false""", re.I)
PAYWALL_SCAN_CHARS = 200_000
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


def is_paywalled(page_text: str) -> bool:
    return bool(PAYWALL_RE.search(page_text[:PAYWALL_SCAN_CHARS]))


class _Sanitizer(HTMLParser):
    """Whitelist-Bereinigung. Mit `max_chars` endet die Ausgabe am ersten Element der obersten
    Ebene nach dieser Länge (spätestens bei der doppelten Länge, dann mit geschlossenen Tags);
    `truncated` sagt, ob gekürzt wurde. Mit `max_images` fallen weitere <img> weg."""

    def __init__(self, base_url: str, max_chars: int | None = None, max_images: int | None = None):
        super().__init__(convert_charrefs=True)
        self.base = base_url
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
        for name, value in attrs:
            if name not in allowed:
                continue
            if name in ("href", "src"):
                value = self._safe_url(value)
                if not value:
                    if tag == "img":
                        return  # Bild ohne brauchbare Quelle weglassen
                    continue
            kept.append(f' {name}="{html.escape(value or "", quote=True)}"')
        if tag == "a":
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


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def sanitize(fragment: str, base_url: str) -> tuple[str, str]:
    """Bereinigt HTML auf eine Whitelist und liefert (html, reiner Text); Obergrenzen wie
    sanitize_limited."""
    clean, text, _ = sanitize_limited(fragment, base_url)
    return clean, text


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
CAPTION_RE = re.compile(r"^\s*(?:Fotos?|Bilder?|Quelle|Photo|Image)\s*:", re.I)


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
# und Absätze ohne Text und ohne Bild (<p><strong></strong></p>).
_PARA_RE = re.compile(r"<p>(.*?)</p>\s*", re.S)
_PUBLISHED_RE = re.compile(r"^\s*Published On\s+\d{1,2}\s+\w+\s+\d{4}")
_PUBLISHED_MAX_LEN = 60     # längere Absätze mit „Published On …“ sind Artikeltext
_NEWSFEED_RE = re.compile(r"^\s*<a\b[^>]*>\s*NewsFeed\s*</a>\s*$")
_JUNK_LABELS = {"hide caption", "toggle caption", "advertisement", "werbung", "anzeige"}


def _drop_junk_paragraphs(body: str) -> str:
    def repl(m: re.Match) -> str:
        inner = m.group(1)
        if _NEWSFEED_RE.match(inner):
            return ""
        text = html_to_text(inner)
        if text.casefold() in _JUNK_LABELS or (not text and "<img" not in inner):
            return ""
        if len(text) < _PUBLISHED_MAX_LEN and _PUBLISHED_RE.match(text):
            return ""
        return m.group(0)
    return _PARA_RE.sub(repl, body)


_LEAD_RE = re.compile(r"^\s*<(figcaption|h2|p)>(.*?)</\1>\s*", re.S)


def _norm_title(text: str) -> str:
    return _normalize_ws(text).casefold()


def drop_title_echo(result: Extraction, title: str | None) -> Extraction:
    """Entfernt ein führendes <figcaption>, <h2> oder <p>, das nur den Titel wiederholt
    (Groß-/Kleinschreibung und Leerraum egal). Der Titel steht in der Leseansicht separat."""
    if not result.body_html or not title or not title.strip():
        return result
    m = _LEAD_RE.match(result.body_html)
    if not m or _norm_title(html_to_text(m.group(2))) != _norm_title(title):
        return result
    body = result.body_html[m.end():].strip()
    text = html_to_text(body)
    if not text:
        return result  # nur der Titel da: lieber so lassen als leeren Text speichern
    return replace(result, body_html=body, body_text=text, body_len=len(text))


def html_to_text(fragment: str | None) -> str:
    """Reiner Text aus (bereits bereinigtem) HTML, z. B. für die Migration bestehender Artikel."""
    if not fragment:
        return ""
    s = _Sanitizer("http://localhost/")
    s.feed(fragment)
    s.close()
    return _normalize_ws("".join(s.text))


def extract(page: bytes, url: str) -> Extraction:
    """Extrahiert den Artikel aus den rohen Bytes; trafilatura erkennt die Kodierung selbst."""
    paywalled = is_paywalled(page[:PAYWALL_SCAN_CHARS].decode("utf-8", errors="ignore"))

    body = trafilatura.extract(
        page,
        url=url,
        output_format="html",
        include_images=True,
        include_links=True,
        include_formatting=True,
        include_tables=True,
        include_comments=False,
        favor_recall=True,
    )
    image_url = author = None
    try:
        meta = trafilatura.extract_metadata(page, default_url=url)
        if meta:
            image_url = safe_http_url(meta.image)
            author = None if is_placeholder(meta.author) else meta.author[:200]
    except Exception:
        pass

    if not body:
        return Extraction("empty", None, None, 0, paywalled, image_url, author)

    clean, text, truncated = sanitize_limited(body, url)
    if not text:
        return Extraction("empty", None, None, 0, paywalled, image_url, author)
    status = "partial" if (paywalled or truncated or len(text) < MIN_FULL_CHARS) else "full"
    return Extraction(status, clean, text, len(text), paywalled, image_url, author)


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
    text = html.unescape(_TAG_RE.sub(" ", raw))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text
