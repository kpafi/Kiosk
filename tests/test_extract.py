from __future__ import annotations

import pytest

from app.extract import extract, html_to_text, is_paywalled, safe_http_url, sanitize, strip_html

BASE = "https://news.example/artikel/1"


def test_self_closing_drop_tag_does_not_swallow_rest():
    html, text = sanitize("<p>a<svg/>b</p><p>rest</p>", BASE)
    assert "rest" in html and "rest" in text
    assert "svg" not in html


def test_self_closing_iframe_does_not_swallow_rest():
    html, _ = sanitize('<p>vor<iframe src="https://evil.example/"/>nach</p><p>Ende</p>', BASE)
    assert "nach" in html and "Ende" in html and "iframe" not in html


def test_self_closing_anchor_is_closed():
    html, text = sanitize('<p><a href="x"/>text danach</p>', BASE)
    assert "text danach" in text
    # Das <a/> wird sofort geschlossen, der Text danach ist kein Linktext.
    assert "</a>text danach</p>" in html


def test_unquoted_anchor_does_not_swallow_text():
    # href=x/ ist ohne Anführungszeichen ein Attributwert "x/", also ein normaler Start-Tag.
    html, text = sanitize("<p><a href=x/>text</p><p>weiter</p>", BASE)
    assert "text" in text and "weiter" in text and "weiter" in html


def test_void_self_closing_tags():
    html, _ = sanitize('<p>a<br/>b<img src="/bild.jpg"/></p>', BASE)
    assert "<br>" in html and "</br>" not in html
    assert '<img src="https://news.example/bild.jpg" loading="lazy">' in html
    assert "</img>" not in html


def test_drop_with_content_still_drops_content():
    html, text = sanitize("<p>ok</p><script>alert(1)</script><style>p{}</style><p>weiter</p>", BASE)
    assert "alert" not in html and "alert" not in text and "p{}" not in text
    assert "weiter" in html


@pytest.mark.parametrize("href", ["javascript:alert(1)", " JavaScript:alert(1)", "data:text/html,x", "vbscript:x"])
def test_dangerous_link_schemes_removed(href):
    html, _ = sanitize(f'<p><a href="{href}">klick</a></p>', BASE)
    assert "href" not in html
    assert "klick" in html


def test_attributes_whitelisted_and_escaped():
    html, _ = sanitize('<p onclick="x()" style="color:red"><a href="/a?b=1&c=&quot;2" title="t">l</a></p>', BASE)
    assert "onclick" not in html and "style" not in html and "title" not in html
    assert 'href="https://news.example/a?b=1&amp;c=&quot;2"' in html
    assert 'rel="noopener noreferrer"' in html


def test_image_without_safe_src_is_dropped():
    html, _ = sanitize('<p><img src="javascript:x"></p><p>t</p>', BASE)
    assert "<img" not in html


def test_text_is_escaped_in_html():
    html, text = sanitize("<p>&lt;script&gt;x&lt;/script&gt;</p>", BASE)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert text == "<script>x</script>"


def test_block_tags_separate_words_in_text():
    _, text = sanitize("<p>eins</p><p>zwei</p><ul><li>drei</li></ul> <strong>vi</strong>er", BASE)
    assert text == "eins zwei drei vier"


def test_html_to_text():
    assert html_to_text("<p>Die <em>Energie</em>wende &amp; mehr</p><p>Ende</p>") == "Die Energiewende & mehr Ende"
    assert html_to_text(None) == ""


@pytest.mark.parametrize("raw,expected", [
    ("https://img.example/a.jpg", "https://img.example/a.jpg"),
    ("  http://img.example/a.jpg ", "http://img.example/a.jpg"),
    ("javascript:alert(1)", None),
    ("data:image/png;base64,AAAA", None),
    ("//img.example/a.jpg", None),
    ("/relative.jpg", None),
    ("ftp://img.example/a.jpg", None),
    ("", None),
    (None, None),
])
def test_safe_http_url(raw, expected):
    assert safe_http_url(raw) == expected


@pytest.mark.parametrize("page,expected", [
    ('{"@type":"NewsArticle","isAccessibleForFree":false}', True),
    ('{"isAccessibleForFree": "False"}', True),
    ("<meta itemprop=\"isAccessibleForFree\" content='false'>", False),  # Microdata mit content= wird nicht erkannt
    ('"isAccessibleForFree" : "false"', True),
    ('isaccessibleforfree=false', True),
    ('{"isAccessibleForFree": true}', False),
    ('nichts', False),
])
def test_paywall_regex(page, expected):
    assert is_paywalled(page) is expected


def test_paywall_regex_only_scans_prefix():
    assert not is_paywalled("x" * 200_001 + '"isAccessibleForFree":false')


def test_strip_html():
    assert strip_html("<b>Fett</b> &amp; <i>kursiv</i>") == "Fett & kursiv"
    assert strip_html("wort " * 300, limit=20).endswith("…")


ARTICLE = """<!doctype html><html><head><meta charset="windows-1252">
<title>Ein Testartikel</title>
<meta property="og:image" content="javascript:alert(1)">
<script type="application/ld+json">{"@type":"NewsArticle","isAccessibleForFree":false}</script>
</head><body><article><h1>Ein Testartikel</h1>
<p>Gr\xfc\xdfe aus M\xfcnchen. Dies ist der erste Absatz eines l\xe4ngeren Artikels, der genug Text enth\xe4lt.</p>
<p>Der zweite Absatz erz\xe4hlt weiter von Dingen, die f\xfcr den Test wichtig sind, und noch mehr.</p>
<p>Ein dritter Absatz, damit trafilatura den Hauptinhalt sicher erkennt und nicht verwirft.</p>
</article></body></html>"""


def test_extract_from_bytes_with_legacy_encoding():
    page = ARTICLE.encode("cp1252")
    result = extract(page, BASE)
    assert result.paywalled is True
    assert result.status == "partial"
    assert result.body_text and "Grüße aus München" in result.body_text
    assert result.body_len == len(result.body_text)
    assert "<p>" in (result.body_html or "")
    assert result.image_url is None  # javascript:-Bild aus den Metadaten verworfen
