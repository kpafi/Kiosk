"""Volltext-Extraktion aus Artikelseiten und Bereinigung des HTML."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import trafilatura

# Schema.org-Markierung, die viele Verlage (Spiegel, NYT, Zeit ...) für Bezahlartikel setzen,
# als JSON-LD ("isAccessibleForFree": false) oder als Attribut/Microdata.
PAYWALL_RE = re.compile(r"""isaccessibleforfree["']?\s*[:=]\s*["']?false""", re.I)
PAYWALL_SCAN_CHARS = 200_000
MIN_FULL_CHARS = 600

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
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.out: list[str] = []
        self.text: list[str] = []
        self._drop_depth = 0

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
            kept.append(' loading="lazy"')
        self.out.append(f"<{tag}{''.join(kept)}>")

    def handle_startendtag(self, tag, attrs):
        # <svg/> oder <iframe/> haben keinen Inhalt: nicht den Drop-Zähler erhöhen,
        # sonst verschwindet der restliche Artikel.
        if tag in DROP_WITH_CONTENT:
            return
        self.handle_starttag(tag, attrs)
        # Erlaubte Nicht-Void-Tags wie <a/> sofort schließen, sonst bleibt der Tag offen.
        if not self._drop_depth and tag in ALLOWED_TAGS and tag not in VOID_TAGS:
            self.out.append(f"</{tag}>")

    def handle_endtag(self, tag):
        self._break(tag)
        if tag in DROP_WITH_CONTENT:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if self._drop_depth or tag not in ALLOWED_TAGS or tag in VOID_TAGS:
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self._drop_depth:
            return
        self.out.append(html.escape(data, quote=False))
        self.text.append(data)


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def sanitize(fragment: str, base_url: str) -> tuple[str, str]:
    """Bereinigt HTML auf eine Whitelist und liefert (html, reiner Text)."""
    s = _Sanitizer(base_url)
    s.feed(fragment)
    s.close()
    body = "".join(s.out)
    body = re.sub(r"^\s*<h1>.*?</h1>\s*", "", body, count=1, flags=re.S)  # Titel steht separat
    body = re.sub(r"(<p>\s*</p>\s*)+", "", body)
    return body.strip(), _normalize_ws("".join(s.text))


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
            author = (meta.author or None) and meta.author[:200]
    except Exception:
        pass

    if not body:
        return Extraction("empty", None, None, 0, paywalled, image_url, author)

    clean, text = sanitize(body, url)
    if not text:
        return Extraction("empty", None, None, 0, paywalled, image_url, author)
    status = "partial" if (paywalled or len(text) < MIN_FULL_CHARS) else "full"
    return Extraction(status, clean, text, len(text), paywalled, image_url, author)


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(raw: str | None, limit: int = 700) -> str:
    if not raw:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", raw))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text
