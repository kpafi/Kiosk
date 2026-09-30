from __future__ import annotations

import re
import time

import lxml.html as lxml_html
import pytest

import app.extract as extract_mod
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


def test_paywall_marker_is_found_anywhere_in_the_page():
    """Le Monde setzt das JSON-LD erst nach rund 240 000 Zeichen; gesucht wird in der ganzen Seite."""
    assert is_paywalled("x" * 238_000 + '"isAccessibleForFree": "False"')
    assert not is_paywalled("x" * extract_mod.PAYWALL_SCAN_CHARS + '"isAccessibleForFree":false')
    # Die GEA-Klasse (Suche mit großem Faktor) nur im Anfang der Seite
    assert not is_paywalled("x" * extract_mod.BLUR_SCAN_CHARS + '<p class="nfy-text-wrapper-blur">x</p>')


@pytest.mark.parametrize("page,expected", [
    # CSM setzt den Wert als URL auf jede Seite, auch auf frei lesbare: zählt nicht
    ('"isAccessibleForFree":"http:\\/\\/schema.org\\/False"', False),
    ('"isAccessibleForFree": "https://schema.org/False"', False),
    # Nur das erste Vorkommen zählt (heise: empfohlene Artikel weiter unten mit eigener Angabe)
    ('{"isAccessibleForFree":true}' + "x" * 200_000 + '{"isAccessibleForFree":false}', False),
    ('{"isAccessibleForFree":"False"}' + "x" * 3_000 + '{"isAccessibleForFree":"False"}', True),   # Handelsblatt H+
    # Handelsblatt: <app-paywall> steht auch in freien, vollständigen Artikeln und zählt nicht.
    ('<app-paywall style="display: none;"><hmg-paywall></hmg-paywall></app-paywall>', False),
])
def test_paywall_regex_schema_url_values(page, expected):
    assert is_paywalled(page) is expected


GEA_BLUR = ('<p class=nfy-text-wrapper-blur aria-label="Unkenntlich gemachter Text" role=paragraph>'
            'Lorem ipsum dolor sit amet, consetetur sadipscing elitr, sed diam nonumy eirmod tempor invidunt ut '
            'labore et dolore magna aliquyam erat, sed diam voluptua.</p>')


@pytest.mark.parametrize("page,expected", [
    (GEA_BLUR, True),
    ('<p class="nfy-text nfy-text-wrapper-blur">x</p>', True),
    ("<p class='nfy-text-wrapper-blur'>x</p>", True),
    ('<p aria-label="Unkenntlich gemachter Text">x</p>', True),
    ("<p ARIA-LABEL = 'unkenntlich gemachter text'>x</p>", True),
    ("<style>.nfy-text-wrapper-blur{filter:blur(4px)}</style>", False),   # nur CSS, kein Absatz
    ("<p>Der Unkenntlich gemachter Text war lesbar.</p>", False),
    ('<p class="nfy-text-wrapper">x</p>', False),
])
def test_paywall_regex_gea(page, expected):
    assert is_paywalled(page) is expected


def test_paywall_regex_stays_fast_on_hostile_input():
    size = 5_000_000   # so viel liest der Abruf höchstens (fetcher.ARTICLE_MAX_BYTES)
    for page in ("class=" * (size // 6), "class=" + "x" * size, 'class="' + "a " * (size // 2),
                 "aria-label=" * (size // 11), "isaccessibleforfree:" + " " * size,
                 ("isaccessibleforfree:" + " " * 50) * (size // 70), "isaccessibleforfree=http://schema.org/" * (size // 40)):
        t0 = time.monotonic()
        assert not is_paywalled(page)
        assert time.monotonic() - t0 < 1.5


def test_gea_marker_lies_within_scan_window():
    """Echte GEA-Seiten sind rund 50 KB groß, der unscharfe Absatz steht nach etwa 48 000 Zeichen."""
    page = "<html>" + "x" * 48_000 + GEA_BLUR + "y" * 2_000 + "</html>"
    assert page.index("nfy-text-wrapper-blur") < extract_mod.PAYWALL_SCAN_CHARS
    assert is_paywalled(page)


GEA_ARTICLE = """<!doctype html><html><head><meta charset="utf-8"><title>Poetic Pop im franz.K</title></head>
<body><article class="nfy-ar-detail"><h1>Poetic Pop im franz.K</h1>
<div class="nfy-ar-detail-inner-wrapper nfy-ar-detail-inner-wrapper-payment">
<p class=nfy-teaser>Independent, Retro-Folk und wilder Rock: Beim ersten Poetic Pop Festival im franz.K begegneten
sich Stile und Zeiten. Drei Bands spielten an einem Abend in Reutlingen vor vollem Haus.</p>
""" + GEA_BLUR + """<div class=paywall-delimiter></div></div></article></body></html>"""


def test_gea_paywall_article_is_marked_and_has_no_lorem_ipsum():
    result = extract(GEA_ARTICLE.encode(), BASE)
    assert result.paywalled is True and result.status == "partial"
    assert "Poetic Pop Festival" in (result.body_text or "")
    assert "lorem" not in (result.body_html or "").lower() and "lorem" not in (result.body_text or "").lower()


def test_blurred_placeholder_elements_are_dropped_whole():
    """Mehrere Absätze Platzhalter ohne „Lorem ipsum“ am Anfang: das ganze Element fällt weg."""
    page = GEA_ARTICLE.replace(GEA_BLUR, '<div class="x nfy-text-wrapper-blur" role=paragraph>'
                               "<p>Consetetur sadipscing elitr, sed diam nonumy eirmod tempor.</p>"
                               "<p>At vero eos et accusam et justo duo dolores et ea rebum.</p></div>"
                               "<p>Schluss nach dem Platzhalter bleibt stehen.</p>")
    result = extract(page.encode(), BASE)
    assert result.paywalled is True
    assert "Poetic Pop Festival" in result.body_text and "Schluss nach dem Platzhalter" in result.body_text
    assert "sadipscing" not in result.body_text and "accusam" not in result.body_text


def test_placeholder_class_on_root_element_does_not_break_extraction():
    page = GEA_ARTICLE.replace("<html>", '<html class="nfy-text-wrapper-blur">')
    assert '<html class="nfy-text-wrapper-blur">' in page
    result = extract(page.encode(), BASE)
    assert result.paywalled is True and "Poetic Pop Festival" in (result.body_text or "")
    nested = GEA_ARTICLE.replace(GEA_BLUR, '<div class="nfy-text-wrapper-blur"><p class="nfy-text-wrapper-blur">'
                                 "Innen</p><p>Auch weg</p></div><p>Danach bleibt.</p>")
    result = extract(nested.encode(), BASE)
    assert "Innen" not in result.body_text and "Auch weg" not in result.body_text
    assert "Danach bleibt." in result.body_text


def test_lorem_ipsum_paragraphs_are_dropped():
    html, text = sanitize(
        "<p>Echter Text.</p><p>Lorem ipsum dolor sit amet, consetetur sadipscing elitr.</p>"
        "<p> <em>lorem IPSUM dolor sit amet</em> at vero eos</p>"
        "<p>Der Blindtext beginnt mit Lorem ipsum dolor sit amet und ist uralt.</p>"
        "<p>Lorem ipsum ist ein Blindtext.</p>", BASE)
    assert html == ("<p>Echter Text.</p><p>Der Blindtext beginnt mit Lorem ipsum dolor sit amet und ist uralt.</p>"
                    "<p>Lorem ipsum ist ein Blindtext.</p>")
    assert "consetetur" not in text and "vero" not in text


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


# ---- Beifang aus der Extraktion ---------------------------------------------------

def test_published_on_and_newsfeed_paragraphs_are_dropped():
    html, text = sanitize(
        '<p>Published On 12 Sep 2026</p><p><a href="/news/">NewsFeed</a></p>'
        '<p>Der eigentliche Text.</p><p>Published in 2020, the report said.</p>', BASE)
    assert "Published On" not in html and "NewsFeed" not in html and "NewsFeed" not in text
    assert "Der eigentliche Text." in html and "Published in 2020" in html
    assert text == "Der eigentliche Text. Published in 2020, the report said."


@pytest.mark.parametrize("para", [
    "Published On the same day, a second report followed.",
    "Published On 12 Sep 2026, the report found that most households saved less than before.",
])
def test_published_on_rule_keeps_real_sentences(para):
    html, _ = sanitize(f"<p>Published On 3 March 2026</p><p>{para}</p>", BASE)
    assert "3 March 2026" not in html and para in html


def test_newsfeed_inside_text_is_kept():
    html, _ = sanitize('<p>Mehr im <a href="/n">NewsFeed</a> von heute.</p>', BASE)
    assert "NewsFeed" in html


def test_label_and_empty_paragraphs_are_dropped():
    html, text = sanitize(
        '<p>hide caption</p><p> Toggle  Caption </p><p>Advertisement</p><p>WERBUNG</p><p>Anzeige</p>'
        '<p><strong></strong></p><p><em> </em><br></p><p>Die Anzeige wurde erstattet.</p>'
        '<p><img src="https://a.example/bild.jpg"></p>', BASE)
    assert html == ('<p>Die Anzeige wurde erstattet.</p>'
                    '<p><img src="https://a.example/bild.jpg" loading="lazy"></p>')
    assert text == "Die Anzeige wurde erstattet."


def test_junk_paragraph_does_not_swallow_the_only_copy_of_an_image():
    """Müll-Absätze fallen vor den doppelten Bildern weg: Steht das erste Vorkommen in einem
    Müll-Absatz, bleibt das zweite erhalten."""
    img = "https://a.example/bild.jpg"
    html, _ = sanitize(f'<p><img src="{img}">Advertisement</p><p>Text</p><figure><img src="{img}?w=640"></figure>',
                       BASE)
    assert html.count("<img") == 1 and f'{img}?w=640' in html and "Advertisement" not in html


def _extraction(body: str):
    from app.extract import Extraction
    text = html_to_text(body)
    return Extraction("full", body, text, len(text), False, None, None)


@pytest.mark.parametrize("tag", ["figcaption", "h2", "p"])
def test_leading_title_echo_is_dropped(tag):
    from app.extract import drop_title_echo
    res = drop_title_echo(_extraction(f"<{tag}>  Große  Nachricht </{tag}><p>Text folgt.</p>"), "große nachricht")
    assert res.body_html == "<p>Text folgt.</p>" and res.body_text == "Text folgt." and res.body_len == 11


def test_title_echo_only_at_start_and_only_exact():
    from app.extract import drop_title_echo
    later = _extraction("<p>Einleitung.</p><h2>Große Nachricht</h2>")
    assert drop_title_echo(later, "Große Nachricht") is later
    longer = _extraction("<p>Große Nachricht mit mehr Wörtern</p><p>x</p>")
    assert drop_title_echo(longer, "Große Nachricht") is longer
    only = _extraction("<p>Große Nachricht</p>")
    assert drop_title_echo(only, "Große Nachricht") is only   # sonst bliebe kein Text
    assert drop_title_echo(later, None) is later


# ---- Bild-Schlüssel und doppelte Bilder (Regeln wie imageKey in static/js/format.js) ----
from urllib.parse import quote  # noqa: E402

from app.extract import image_key, is_small_image_url, same_image  # noqa: E402

SPIEGEL = "https://cdn.prod.www.spiegel.de/images/c67a3ae2-2a68-4bc0-a149-fcb957f4ec3a"
FAZ = "https://media0.faz.net/image/w400"
NYT = "https://static01.nyt.com/images/2026/09/25/multimedia/25pol-foo-01-abcd/25pol-foo-01-abcd"
NEXT = "https://kyivindependent.com/_next/image?url=https%3A%2F%2Fassets.example%2Fimg%2F"
# (URL a, URL b, gleich laut Server same_image, gleich laut Browser sameImage).
# Der Server wertet nur gleiche Schlüssel (sicher dasselbe Bild); der Browser zusätzlich unscharf.
# image_key wird in tests/test_frontend_js.py für alle URLs gegen imageKey (JS) verglichen,
# sameImage dort gegen die vierte Spalte.
IMAGE_PAIRS = [
    (f"{SPIEGEL}_w960_r1.5_fpx43_fpy41.jpg", f"{SPIEGEL}_w520_r1.33_fpx42.6_fpy49.97.jpg", True, True),
    (f"{SPIEGEL}_w960_r1.5_fpx43_fpy41.jpg", "https://cdn.prod.www.spiegel.de/images/8556f02e-d502_w520_r2.08.jpg", False, False),
    ("https://x.example/wp/foto-1024x683.jpg", "http://x.example/wp/foto.jpg?w=300#top", True, True),
    ("https://x.example/wp/foto-scaled.jpg", "https://x.example/wp/foto-300x200.webp", True, True),
    ("https://img.example/w960/abc.jpg", "https://img.example/w320/abc.jpg", True, True),
    ("https://img.example/1200x800/abc.jpg", "https://img.example/abc@2x.jpg", True, True),
    ("https://a.example/1/image.jpg", "https://a.example/2/image.jpg", False, False),
    ("https://a.example/galerie-01.jpg", "https://a.example/galerie-02.jpg", False, False),
    ("https://a.example/bild.jpg?width=960", "https://a.example/bild.jpg?width=320", True, True),
    ("", "https://a.example/bild.jpg", False, False),
    # Query ohne Bild-Endung bleibt (ohne Größen-Parameter); url= eines Bild-Proxys ist der Schlüssel.
    (f"{NEXT}a.jpg&w=640", f"{NEXT}b.jpg&w=640", False, False),
    (f"{NEXT}a.jpg&w=640&q=75", f"{NEXT}a.jpg&w=1536", True, True),
    (f"{NEXT}a.jpg&w=640", "https://assets.example/img/a.jpg", True, True),
    ("https://a.example/img.php?id=1", "https://a.example/img.php?id=2", False, False),
    ("https://a.example/img.php?id=1&width=64", "https://a.example/img.php?id=1&width=640", True, True),
    # Gleicher Dateiname, anderes Bild: FAZ mit anderer Bild-ID, gleicher Name auf anderem Host.
    (f"{FAZ}/52da1bbe0fb6/w5463h3073x2729y0/202609/00a5d0a5/der-dauerstreit.webp",
     f"{FAZ}/004c994d93df/w5761h3241x0y0/202609/94ac381f/der-dauerstreit.webp", False, False),
    ("https://a.example/photos/thumbnail.jpg", "https://b.example/photos/thumbnail.jpg", False, False),
    ("https://a.example/bilder/sonnenaufgang_w800.jpg", "https://cdn.b.example/x/sonnenaufgang.jpg", False, False),
    # Nur im Browser gleich: abweichende Größen-Segmente bzw. Varianten-Endungen.
    ("https://ichef.bbci.co.uk/news/1024/cpsprodpb/live/c288fcf0-b8f2.jpg",
     "https://ichef.bbci.co.uk/news/480/cpsprodpb/live/c288fcf0-b8f2.jpg.webp", False, True),
    # El País: dasselbe Bild auf zwei Hosts derselben Website (auth= bleibt Teil des Schlüssels).
    ("https://images.english.elpais.com/resizer/v2/WV7WEPOUJNFONI4E3JACBY4UPY.JPG?auth=714c4b",
     "https://imagenes.elpais.com/resizer/v2/WV7WEPOUJNFONI4E3JACBY4UPY.JPG?auth=714c4b&width=414", False, True),
    ("https://images.english.elpais.com/resizer/v2/WV7WEPOUJNFONI4E3JACBY4UPY.JPG?auth=714c4b",
     "https://imagenes.elpais.com/resizer/v2/WV7WEPOUJNFONI4E3JACBY4UPY.JPG?auth=aaaa11", False, False),
    # n-tv: Zuschnitt- und Breiten-Ordner.
    ("https://www.n-tv.de/img/31348263/1790342429/Img_4_3/250/GettyImages-2296637644.jpg",
     "https://www.n-tv.de/img/31348263/1790342429/Img_16_9/1024/GettyImages-2296637644.webp", False, True),
    # Le Monde: Zuschnitt als Zahlen-Ordner.
    ("https://img.lemde.fr/2026/09/24/0/0/7704/3852/644/322/60/0/e2d7448_ftp-1-rft5zuvmaawu-5174699-01-06.jpg",
     "https://img.lemde.fr/2026/09/24/0/0/7704/5139/664/0/75/0/e2d7448_ftp-1-rft5zuvmaawu-5174699-01-06.jpg", False, True),
    # WordPress: allgemeiner Dateiname in anderen Jahres-/Monatsordnern ist ein anderes Bild.
    ("https://blog.example/wp-content/uploads/2025/10/screenshot.png",
     "https://blog.example/wp-content/uploads/2026/09/screenshot.png", False, False),
    # taz: gleiche Bild-ID im Pfad, Breiten-Ordner verschieden, Dateiname ohne Ziffern.
    ("https://taz.de/picture/8763231/948/Berlin-Wahl-Linke.jpeg", "https://taz.de/picture/8763231/14/Berlin-Wahl-Linke.webp",
     False, True),
    # Zuschnitt-/Größen-Parameter von Bilddiensten (Al Jazeera resize=, CBC im=) fallen weg.
    ("https://www.aljazeera.com/wp-content/uploads/2026/09/ap_6aa6da36d5b77-1789319734.jpg?resize=1920%2C1440",
     "https://www.aljazeera.com/wp-content/uploads/2026/09/ap_6aa6da36d5b77-1789319734.jpg?resize=770%2C513&quality=80",
     True, True),
    # Andere Website mit ähnlichem Namen: nicht dasselbe.
    ("https://img.a-example.co.uk/1024/c288fcf0-b8f2.jpg", "https://img.b-example.co.uk/480/c288fcf0-b8f2.jpg", False, False),
    # Query an Bild-Endungen: nur Größen-Parameter fallen weg.
    ("https://a.example/photo.jpg?id=1", "https://a.example/photo.jpg?id=2", False, False),
    ("https://images.tagesschau.de/image/x/AAABnSSvrFg/16x9/vw-1560.jpg?width=1280",
     "https://images.tagesschau.de/image/x/AAABnSSvrFg/16x9/vw-1560.jpg?width=640", True, True),
    # Spiegel-Zuschnitt-Token ohne _w-Token gehören zum Namen.
    ("https://a.example/foto_r1.jpg", "https://a.example/foto_r2.jpg", False, False),
    (f"{NYT}-superJumbo.jpg", f"{NYT}-articleLarge.jpg?quality=75", False, True),
    # Zuschnitt-Parameter: Server strikt (anderer Ausschnitt kann ein anderes Bild sein), Browser unscharf.
    ("https://i.cbc.ca/1.123/default.jpg?im=Crop,rect=(103,106,421,236)",
     "https://i.cbc.ca/1.123/default.jpg?im=Crop,rect=(0,0,588,457)", False, True),
    # url=/src= ohne Bild-Endung: übrige Parameter gehören zum Schlüssel.
    ("https://ex.com/thumb?src=/v.mp4&t=10", "https://ex.com/thumb?src=/v.mp4&t=50", False, False),
    ("https://ex.com/thumb?src=/v.mp4&t=10&w=300", "https://ex.com/thumb?w=640&src=/v.mp4&t=10", True, True),
    # Bewusst falsch-negativ: tagesschau-Varianten tragen verschiedene IDs im Pfad.
    ("https://images.tagesschau.de/image/x/AAABnSSvrFg/16x9-big/vw-1560.jpg",
     "https://images.tagesschau.de/image/x/AAABnSSvdEs/16x7/vw-1560.jpg", False, False),
]


@pytest.mark.parametrize("a,b,same,_client", IMAGE_PAIRS)
def test_same_image(a, b, same, _client):
    assert same_image(a, b) is same
    assert same_image(b, a) is same


def test_image_key_query_rules():
    assert image_key(f"{NEXT}a.jpg&w=640")[0] == "assets.example/img/a"
    assert image_key("https://a.example/_next/image?url=%2Fimg%2Fa.jpg&w=640")[0] == "a.example/img/a"
    assert image_key("https://a.example/img.php?id=1&w=64&fit=crop")[0] == "a.example/img?id=1"
    assert image_key("https://a.example/x.JPG?id=1&width=64")[0] == "a.example/x?id=1"
    assert image_key("HTTPS://A.Example/Bild/X.JPG")[0] == "a.example/Bild/X"   # nur der Host wird klein
    assert image_key("https://a.example/h1/w5/bild.jpg")[0] == "a.example/h1/w5/bild"   # /w5/ ist kein Größen-Ordner


def test_image_key_strict_keeps_crop_params():
    url = "https://i.cbc.ca/1.123/default.jpg?im=Crop,rect=(103,106,421,236)&w=780&id=7"
    assert image_key(url)[0] == "i.cbc.ca/1.123/default?im=Crop,rect=(103,106,421,236)&id=7"
    assert image_key(url, strict=False)[0] == "i.cbc.ca/1.123/default?id=7"
    assert image_key("https://a.example/p?url=%2Fx.mp4&t=1&w=5")[0] == "a.example/x?t=1"
    assert image_key("https://a.example/p?url=%2Fx.jpg&t=1&w=5")[0] == "a.example/x"   # Bild-Endung: nur die innere


def test_image_key_url_param_depth_is_limited():
    url = "https://a.example/bild.jpg"
    for _ in range(8):
        url = "https://p.example/proxy?url=" + quote(url, safe="")
    full, _ = image_key(url)
    assert full.startswith("p.example/proxy?url=")


@pytest.mark.parametrize("url,small", [
    ("https://a.example/autor_w120_r1.jpg", True),
    ("https://a.example/bild_w960_r1.jpg", False),
    ("https://a.example/img/80x80/autor.jpg", True),
    ("https://a.example/img.php?id=1&dimensions=65", True),
    ("https://a.example/bild?width=862", False),
    ("https://a.example/bild.jpg", False),
    ("https://cdn.tickaroo.com/mediagw/image/c_fill%2Cw_64%2Ch_64/f_webp/x/avatar.jpeg", True),
    ("https://res.example/image/upload/w_1200,c_fill/bild.jpg", False),
])
def test_is_small_image_url(url, small):
    assert is_small_image_url(url) is small


def test_image_key_spiegel():
    full, base = image_key(f"{SPIEGEL}_w960_r1.5_fpx43_fpy41.jpg?x=1")
    assert full == SPIEGEL.split("://", 1)[1] + "?x=1"
    assert base == "c67a3ae2-2a68-4bc0-a149-fcb957f4ec3a"


def test_sanitize_drops_duplicate_images_in_body():
    body = (f'<p>Text</p><figure><img src="{SPIEGEL}_w960_r1.5_fpx43_fpy41.jpg"><figcaption>Wahlkabine</figcaption></figure>'
            f'<p><a href="/x"><img src="{SPIEGEL}_w520_r1.33_fpx43_fpy41.jpg"></a></p>'
            f'<figure><img src="{SPIEGEL}_w300_r1_fpx43_fpy41.jpg"><figcaption>Wahlkabine</figcaption></figure>'
            '<p>Ende</p><img src="https://a.example/anderes-bild.jpg">')
    out, text = sanitize(body, BASE)
    assert out.count("<img") == 2
    assert f"{SPIEGEL}_w960_r1.5_fpx43_fpy41.jpg" in out and "anderes-bild" in out
    assert "<a " not in out and "<p></p>" not in out and out.count("<figure>") == 1
    assert text == "Text Wahlkabine Ende"


def test_sanitize_keeps_distinct_images_untouched():
    body = '<p>a</p><img src="https://a.example/1/image.jpg"><img src="https://a.example/2/image.jpg">'
    out, _ = sanitize(body, BASE)
    assert out.count("<img") == 2


def test_sanitize_keeps_repeated_small_images():
    """Gleiche Wappen in einer Ergebnisliste sind Absicht, keine Duplikate."""
    badge = "https://omo.example/image.php?dimensions=65&amp;id=1550"
    body = f'<ul><li><a href="/a"><img src="{badge}"> Knights</a></li><li><a href="/b"><img src="{badge}"> Knights</a></li></ul>'
    out, _ = sanitize(body, BASE)
    assert out.count("<img") == 2


def test_sanitize_keeps_same_name_different_image():
    body = (f'<p>a</p><img src="{FAZ}/52da1bbe0fb6/w1/202609/aa/der-dauerstreit.webp">'
            f'<img src="{FAZ}/004c994d93df/w1/202609/bb/der-dauerstreit.webp">')
    out, _ = sanitize(body, BASE)
    assert out.count("<img") == 2


def test_sanitize_drops_caption_of_dropped_duplicate():
    img = f"{SPIEGEL}_w960_r1.5_fpx43_fpy41.jpg"
    body = (f'<p>Anfang</p><p><img src="{img}"></p><p>Foto: dpa</p><p>Mitte</p>'
            f'<figure><img src="{img}"></figure><p>Foto: Reuters</p><p>Ende</p>'
            f'<img src="https://a.example/anderes-bild.jpg"><p>Quelle: AP</p>')
    out, text = sanitize(body, BASE)
    assert out.count("<img") == 2 and "<figure>" not in out
    assert "Reuters" not in out and "Foto: dpa" in out and "Quelle: AP" in out
    assert text == "Anfang Foto: dpa Mitte Ende Quelle: AP"
    assert len(text) == len(html_to_text(out))


def test_sanitize_keeps_caption_only_figures_of_other_images():
    """Nur die durch das Entfernen leer gewordene <figure> fällt weg, keine andere."""
    img = f"{SPIEGEL}_w960_r1.5_fpx43_fpy41.jpg"
    body = (f'<p>Anfang</p><figure><img src="{img}"><figcaption>Eins</figcaption></figure>'
            '<figure><figcaption>Grafik folgt</figcaption></figure>'
            f'<figure><img src="{img}"><figcaption>Zwei</figcaption></figure><p>Ende</p>')
    out, text = sanitize(body, BASE)
    assert out.count("<img") == 1 and out.count("<figure>") == 2
    assert "Grafik folgt" in out and "Zwei" not in out
    assert text == "Anfang Eins Grafik folgt Ende"


# ---- Obergrenzen für feindliche Seiten ------------------------------------------------

def test_long_body_is_cut_at_top_level_element(monkeypatch):
    import app.extract as extract_mod
    monkeypatch.setattr(extract_mod, "MAX_BODY_HTML", 2000)
    body = "".join(f"<p>Absatz {i} " + "x" * 80 + "</p>" for i in range(200))
    out, text, truncated = extract_mod.sanitize_limited(body, BASE)
    assert truncated and 2000 < len(out) < 2200
    assert out.count("<p>") == out.count("</p>") and out.endswith("</p>")
    assert "Absatz 199" not in text and text.startswith("Absatz 0")
    _, _, short = extract_mod.sanitize_limited("<p>kurz</p>", BASE)
    assert short is False


def test_single_huge_element_is_closed_at_hard_limit(monkeypatch):
    import app.extract as extract_mod
    monkeypatch.setattr(extract_mod, "MAX_BODY_HTML", 1000)
    body = "<table><tbody>" + ("<tr><td><p>" + "y" * 50 + "</p></td></tr>") * 500 + "</tbody></table><p>Ende</p>"
    out, _, truncated = extract_mod.sanitize_limited(body, BASE)
    assert truncated and len(out) <= 2200
    for tag in ("table", "tbody", "tr", "td", "p"):
        assert out.count(f"<{tag}>") == out.count(f"</{tag}>"), tag
    assert "Ende" not in out
    # Verwaiste End-Tags verlängern die Ausgabe über der Grenze nicht mehr.
    out, _, _ = extract_mod.sanitize_limited("<p>" + "z" * 1500 + "</p>" + "</td></tr>" * 5000, BASE)
    assert len(out) < 2200


def test_images_are_capped():
    from app.extract import MAX_IMAGES
    body = "".join(f'<p><img src="/bild-nummer-{i}.jpg"></p>' for i in range(MAX_IMAGES + 50))
    out, _ = sanitize(body, BASE)
    assert out.count("<img") == MAX_IMAGES


def test_truncated_extraction_is_partial(monkeypatch):
    import app.extract as extract_mod
    monkeypatch.setattr(extract_mod, "MAX_BODY_HTML", 3000)
    page = ("<html><head><title>T</title></head><body><article><h1>Titel</h1>"
            + "".join(f"<p>Absatz {i}: ein ganz normaler Satz mit genug Text für die Extraktion.</p>" for i in range(400))
            + "</article></body></html>").encode()
    res = extract_mod.extract(page, BASE)
    assert res.status == "partial" and len(res.body_html) < 3500 and res.body_len > 600


def test_many_unclosed_tags_stay_linear():
    import time
    from app.extract import sanitize_limited
    body = "<p>" + "<b>x" * 40_000 + "</i>" * 20_000 + "</p>"
    t0 = time.monotonic()
    out, _, _ = sanitize_limited(body, BASE)
    assert time.monotonic() - t0 < 3
    assert out.startswith("<p><b>x")


@pytest.mark.parametrize("filler", ["a", "wort ", "&<>"])
def test_single_huge_text_node_is_cut(filler):
    from app.extract import MAX_BODY_HTML, sanitize_limited
    body = "<p>" + filler * (4_000_000 // len(filler)) + "</p><p>danach</p>"
    out, text, truncated = sanitize_limited(body, BASE)
    # Ein Textknoten wird an der Grenze geschnitten; zerfällt der Text in viele Knoten ("<>" teilt
    # ihn), greift wie bei jedem langen Element die harte Grenze (doppelte Länge).
    assert truncated and len(out) <= (MAX_BODY_HTML if filler != "&<>" else 2 * MAX_BODY_HTML) + 20
    assert out.startswith("<p>") and out.endswith("</p>") and out.count("<p>") == out.count("</p>") == 1
    assert "danach" not in out and len(text) > MAX_BODY_HTML // 10
    import re
    assert not re.search(r"&[a-z]*</p>$", out)   # keine angeschnittene Entität


# ---- Lesezeit-Etiketten und Bedienelemente (SPIEGEL „3 Min“) ------------------------

def _spiegel_reading_time(minutes: int) -> str:
    """Lesezeit mit Symbol und Merken-Knopf unter einem Teaser, wie auf spiegel.de."""
    return ('<div class="mt-12 flex items-center sm:hidden justify-between">'
            '<span class="flex flex-wrap items-center font-sansUI" data-contains-auxiliary-icons="Text">\n'
            '<span data-icon-auxiliary="Text" class="flex items-center leading-none h-20">\n'
            '<span class="mr-6"><svg width="16" height="16" aria-hidden="true"><use href="#spon-text-m"/></svg>\n'
            f'</span>\n<span>{minutes} Min</span>\n</span>\n</span>\n'
            '<button class="flex" data-component="BookmarkButton" data-area="bookmark_button"'
            ' title="Zur Merkliste hinzufügen"><span class="sr-only">Zur Merkliste hinzufügen</span></button></div>')


def _spiegel_teaser(n: int, minutes: int) -> str:
    """„Mehr zum Thema“-Teaser mitten im Artikel: Überschrift als Link, Lesezeit (zweimal, für schmale
    und breite Anzeige), Bild. trafilatura verwarf Überschrift und Bild, die Lesezeit blieb als
    Listenpunkt stehen."""
    href = f"https://www.spiegel.de/wirtschaft/teaser-{n}-a-0000{n}"
    return (f'<li class="border-separator-secondary-b py-16" data-pos-inner="{n}"><div class="flex items-start">'
            f'<div class="flex-1 min-w-0"><a href="{href}" title="Teaser {n}"><span class="block font-bold">'
            f'<span class="align-middle">Verwandter Artikel {n}: Überschrift des Teasers</span></span></a>'
            + _spiegel_reading_time(minutes) + f'</div><a href="{href}"><div><picture><img data-src="'
            f'https://cdn.prod.www.spiegel.de/images/teaser-{n}_w240_r1.333.jpg" src="data:image/svg+xml,%3Csvg%3E'
            '%3C/svg%3E" width="240" height="180" alt></picture></div></a></div>'
            + _spiegel_reading_time(minutes) + '</li>')


def _spiegel_text(pos: int, html: str) -> str:
    return f'<div data-area="text" data-pos="{pos}"><div class="RichText">{html}</div></div>'


SPIEGEL_TOOLBAR = (
    '<ul class="flex items-center"><li><span x-data="{isLoading: false}">\n'
    '<button type="button" class="font-sansUI leading-normal text-s" data-area="play_button"'
    ' data-sara-click-el="button" title="Artikel anhören (3 Minuten)">\n'
    '<span class="flex"><svg width="28" height="28" aria-hidden="true"><use href="#spon-rounded-audio-play-xl"/></svg>\n'
    '</span>\n<span class="sr-only">Artikel anhören (3 Minuten)</span>\n'
    '<span class="ml-6 leading-none" aria-hidden="true">3 Min</span>\n</button>\n</span>\n</li>\n'
    '<li><button class="flex" data-component="BookmarkButton" data-area="bookmark_button" title="Zur Merkliste hinzufügen">'
    '<span class="sr-only">Zur Merkliste hinzufügen</span></button></li></ul>')

SPIEGEL_URL = "https://www.spiegel.de/wirtschaft/boeing-a-1"
SPIEGEL_ARTICLE = (
    '<!doctype html><html><head><meta charset="utf-8"><title>Boeing meldet Softwarefehler</title></head>'
    '<body><main id="Inhalt"><article><header><h1>Boeing meldet Softwarefehler bei Pannen-Flugzeug</h1></header>'
    '<div data-area="body">'
    + _spiegel_text(2, "<p>Der Flugzeugbauer Boeing hat einen Fehler in der Software für die automatische "
                       "Landenavigation gemeldet. Betroffen sind nach Angaben des Unternehmens mehrere Hundert "
                       "Maschinen.</p>")
    + _spiegel_text(4, "<p>Die Airlines wurden aufgefordert, bestimmte Anflugverfahren bis auf Weiteres zu "
                       "unterlassen. Ingenieure arbeiteten an einer dauerhaften Lösung, teilte das Unternehmen "
                       "mit.</p>")
    + '<section data-area="related_articles" data-pos="8"><div><ul class="flex flex-col">'
    + _spiegel_teaser(1, 3) + _spiegel_teaser(2, 6) + _spiegel_teaser(3, 3) + "</ul></div></section>"
    + _spiegel_text(10, "<p>»Dass sich solche Fehler wiederholen, ist nicht hinnehmbar«, sagte ein Sprecher der "
                        "Aufsichtsbehörde FAA. Boeing steht seit Jahren unter strenger Beobachtung.</p><p>Wie viele "
                        "Flugzeuge derzeit mit dem fehlerhaften Programm im Einsatz sind, blieb zunächst unklar. Das "
                        "Unternehmen kündigte ein Update innerhalb weniger Wochen an.</p>")
    + "</div></article></main></body></html>")

_MIN_LABEL_RE = re.compile(r"(?:^|\s)\d{1,3} Min(?:\s|$)")


def test_spiegel_reading_times_and_buttons_are_not_extracted():
    result = extract(SPIEGEL_ARTICLE.encode(), SPIEGEL_URL)
    assert result.status == "full"
    assert "Landenavigation" in result.body_text and "nicht hinnehmbar" in result.body_text
    assert "Update innerhalb weniger Wochen" in result.body_text
    assert not _MIN_LABEL_RE.search(result.body_text), result.body_text
    assert "anhören" not in result.body_text and "Merkliste" not in result.body_text
    assert "<li>" not in result.body_html and "<ul>" not in result.body_html


def test_reading_time_backstop_works_without_precleaning(monkeypatch):
    """Zweite Sicherung: Auch ohne Vorab-Bereinigung (etwa bei anderem Markup) bleibt keine Liste
    „3 Min / 6 Min / 3 Min“ stehen."""
    from trafilatura.utils import load_html
    monkeypatch.setattr(extract_mod, "_cleaned_tree", lambda page: (load_html(page), False))
    result = extract(SPIEGEL_ARTICLE.encode(), SPIEGEL_URL)
    assert "Landenavigation" in result.body_text and "Update innerhalb weniger Wochen" in result.body_text
    assert not _MIN_LABEL_RE.search(result.body_text) and "<li>" not in result.body_html


def test_toolbar_list_inside_text_leaves_no_labels():
    """Die Leiste mit „Artikel anhören (3 Minuten)“ und „3 Min“ direkt im Artikeltext."""
    page = SPIEGEL_ARTICLE.replace('<div class="RichText"><p>Der', '<div class="RichText">' + SPIEGEL_TOOLBAR
                                   + '<p>Der', 1)
    assert page != SPIEGEL_ARTICLE
    result = extract(page.encode(), SPIEGEL_URL)
    assert "Landenavigation" in result.body_text
    assert not _MIN_LABEL_RE.search(result.body_text) and "anhören" not in result.body_text


def test_preclean_keeps_buttons_with_images_and_aria_hidden_content():
    from app.extract import _precleaned
    page = (b'<html><body><article><p>Text</p><button class="zoom"><img src="https://a.example/b.jpg"></button>'
            b'<figure><img src="https://a.example/c.jpg"><figcaption aria-hidden="true">Echte Unterschrift'
            b'</figcaption></figure><BUTTON>Teilen</BUTTON><span data-icon-auxiliary="Text">1 Min</span>'
            b'<section data-area="related_articles"><a href="/x">Teaser</a></section></article></body></html>')
    import lxml.html
    out = lxml.html.tostring(_precleaned(page), encoding="unicode")
    assert "b.jpg" in out and "Echte Unterschrift" in out
    assert "Teilen" not in out and "1 Min" not in out and "Teaser" not in out
    assert _precleaned(b"<html><body><p>Nichts zu tun</p></body></html>") == b"<html><body><p>Nichts zu tun</p></body></html>"


def test_gea_placeholders_and_buttons_are_removed_together():
    page = GEA_ARTICLE.replace("<p class=nfy-teaser>", "<button>Vorlesen</button><p class=nfy-teaser>")
    result = extract(page.encode(), BASE)
    assert result.paywalled is True and "Poetic Pop Festival" in result.body_text
    assert "lorem" not in result.body_text.lower() and "Vorlesen" not in result.body_text


@pytest.mark.parametrize("label", ["3 Min", " 12  Min. ", "3 Minuten", "45 min", "3 Min 3 Min",
                                   "\n\n\n6 Min\n\n6 Min", "Artikel anhören", "Artikel anhören (3 Minuten)",
                                   "<span>6</span> Min",
                                   "<span>Artikel anhören (3 Minuten)</span>\n<span>3 Min</span>"])
def test_reading_time_items_are_dropped(label):
    html, text = sanitize(f"<p>Vorher.</p><ul><li>{label}</li><li>\n\n\n{label}</li></ul><p>Nachher.</p>", BASE)
    assert html == "<p>Vorher.</p><p>Nachher.</p>"
    assert text == "Vorher. Nachher."


@pytest.mark.parametrize("label", ["Artikel anhören", "Artikel anhören (3 Minuten)",
                                   "<span>Artikel anhören (3 Minuten)</span>\n<span>3 Min</span>"])
def test_listen_label_paragraphs_are_dropped(label):
    html, _ = sanitize(f"<p>Vorher.</p><p>{label}</p><p>Nachher.</p>", BASE)
    assert html == "<p>Vorher.</p><p>Nachher.</p>"


@pytest.mark.parametrize("para", ["90 Min", "3 Min", "45 min", "12 Minuten"])
def test_bare_minutes_paragraph_is_kept(para):
    """Ein Absatz „90 Min“ allein kann Inhalt sein (Liveticker); nur Listenpunkte fallen weg."""
    html, _ = sanitize(f"<p>Vorher.</p><p>{para}</p>", BASE)
    assert html == f"<p>Vorher.</p><p>{para}</p>"


@pytest.mark.parametrize("keep", [
    "<p>Nach 3 Min war alles vorbei.</p>",
    "<ul><li>3 Min Anfahrt</li><li>5 Min Fußweg</li></ul>",
    "<ul><li>Min</li><li>3 Minister</li><li>1000 Min</li></ul>",
    "<p>Artikel anhören? Lieber lesen.</p>",
    # Reine Linkliste ohne Lesezeit, Link mit weiterem Text, zwei Links mit Lesezeit
    '<ul><li><a href="https://a.example/1">Erster Link</a></li><li><a href="https://a.example/2">Zweiter</a></li></ul>',
    '<ul><li><a href="https://a.example/1">Fahrplan</a> 3 Min Fußweg</li></ul>',
    '<ul><li><a href="https://a.example/1">Eins</a> <a href="https://a.example/2">Zwei</a> 3 Min</li></ul>',
    '<ul><li>Vorwort <a href="https://a.example/1">Link</a> 3 Min</li></ul>',
])
def test_reading_time_rule_keeps_real_content(keep):
    html, _ = sanitize(f"<p>Vorher.</p>{keep}", BASE)
    expected = re.sub(r'(<a href="[^"]*")', r'\1 target="_blank" rel="noopener noreferrer"', keep)
    assert html == f"<p>Vorher.</p>{expected}"


def test_reading_time_items_leave_other_items_and_nested_lists_intact():
    html, _ = sanitize("<ul><li>Echter Punkt</li><li>3 Min</li><li>Außen<ul><li>2 Min</li></ul></li></ul>", BASE)
    assert html == "<ul><li>Echter Punkt</li><li>Außen</li></ul>"
    html, _ = sanitize("<ol><li><p>3 Min</p></li></ol><p>Text.</p>", BASE)
    assert html == "<p>Text.</p>"


@pytest.mark.parametrize("nested", [
    "<ul><li><ul><li>3 Min</li></ul></li></ul>",
    "<ul><li><ol><li><ul><li>3 Min</li><li>6 Min</li></ul></li></ol></li></ul>",
])
def test_nested_reading_time_lists_vanish_in_one_call(nested):
    from app.extract import strip_reading_times
    html, _ = sanitize(f"<p>Text.</p>{nested}", BASE)
    assert html == "<p>Text.</p>"
    assert strip_reading_times(f"<p>Text.</p>{nested}") == "<p>Text.</p>"


STORED_SPIEGEL = ("<p>Die Airlines sollen die Verfahren zu unterlassen.</p><ul><li>\n\n\n3 Min</li><li>\n\n\n6 Min</li>"
                  "<li>\n\n\n3 Min</li></ul><p>»Dass sich solche Fehler wiederholen, ist nicht hinnehmbar«.</p>")


def test_strip_reading_times_cleans_stored_bodies():
    from app.extract import strip_reading_times
    assert strip_reading_times(STORED_SPIEGEL) == (
        "<p>Die Airlines sollen die Verfahren zu unterlassen.</p>"
        "<p>»Dass sich solche Fehler wiederholen, ist nicht hinnehmbar«.</p>")
    for unchanged in (None, "", "<p>Ohne Etikett.</p><ul><li></li></ul>", "<p>Nach 3 Min war alles vorbei.</p>",
                      "<p>90 Min</p>"):
        assert strip_reading_times(unchanged) == unchanged
    assert strip_reading_times("<p>Text.</p><ul><li>3 MIN</li></ul>") == "<p>Text.</p>"


# So speicherte die alte Extraktion Teaser aus „Mehr zum Thema“: Überschrift als Link, danach die
# Lesezeit doppelt (schmale und breite Anzeige); daneben ein zweiter Teaser.
STORED_SPIEGEL_TEASERS = (
    '<p>Die Gewerkschaften lehnen die Pläne ab.</p><ul><li>\n<a href="https://www.spiegel.de/wirtschaft/service/'
    'rente-krankenkasse-pflege-was-die-reformen-der-bundesregierung-fuer-sie-bedeuten-a-3d9698a3-583f-4046-8599-'
    '4956fd936ed2" target="_blank" rel="noopener noreferrer">\nRente, Krankenkasse, Pflege: Was die Reformen der '
    'Regierung für Sie bedeuten\nEine Kolumne von Hermann-Josef Tenhagen\n\n</a>\n\n\n6 Min\n\n6 Min</li><li>\n'
    '<a href="https://www.spiegel.de/wirtschaft/aufschwung-a-1" target="_blank" rel="noopener noreferrer">\n'
    '<strong>Wirtschaft in Deutschland:</strong> Ist der Aufschwung endlich da?</a>\n13 Min\n\n13 Min</li></ul>'
    '<p>Deutschlands Arbeitgeber warfen dem DGB »rituelle Reformverweigerung« vor.</p>')


def test_strip_reading_times_drops_stored_teaser_items():
    from app.extract import strip_reading_times
    assert strip_reading_times(STORED_SPIEGEL_TEASERS) == (
        "<p>Die Gewerkschaften lehnen die Pläne ab.</p>"
        "<p>Deutschlands Arbeitgeber warfen dem DGB »rituelle Reformverweigerung« vor.</p>")


FOCUS_URL = "https://www.focus.de/finanzen/news/finanzamt-krypto_1234.html"
FOCUS_ARTICLE = (
    '<!doctype html><html><head><meta charset="utf-8"><title>Finanzamt und Krypto</title></head><body><main>'
    '<article><h1>Finanzamt bekommt mehr Einblick in Ihre Bitcoin-Geschäfte</h1>'
    '<div class="Content-Author-List Content-Author-Single"><div class="Content-Author-Images">'
    '<span class="Content-Author"><img src="https://quadro.example/marvin-haas.jpg" alt="Marvin Haas"></span></div>'
    '<span class="Content-Author-Text"><a href="https://www.focus.de/autoren/marvin-haas_1.html">Marvin Haas</a>'
    '</span></div>'
    '<div class="Html-Block" data-qa-article-content-text><p>Seit 2026 müssen bestimmte Krypto-Dienstleister '
    'Informationen über ihre Kunden und deren Transaktionen an die zuständigen Steuerbehörden melden. '
    'Deutschland setzt damit eine europäische Richtlinie um.</p><h2>Krypto-Mining kann gewerblich sein</h2>'
    '<p>Wer Kryptowährungen in größerem Umfang und mit Gewinnerzielungsabsicht schürft, kann damit eine '
    'gewerbliche Tätigkeit ausüben. Dann werden auch Gewerbe- und Umsatzsteuer relevant.</p></div>'
    '<div class="Article-ReusableText Html-Block"><div><h2>So richten Sie FOCUS online als bevorzugte Quelle '
    'bei Google ein</h2><p>Sie wollen FOCUS online als bevorzugte Quelle bei Google hinterlegen? Voraussetzung '
    'ist ein Google-Konto. <a href="https://www.google.com/preferences/source?q=https://www.focus.de">Über '
    'diesen Link</a> geht es direkt zur Einstellung.</p></div></div>'
    '<div class="Article-ReusableText Html-Block"><p>Stefan Petzold ist Landschaftsarchitekt und Teil unseres '
    'Expertenkreises.</p></div>'
    '<section class="Newsletter-Container Html-Block" data-element-tracking-name="newsletter-widget">'
    '<h2>Hier für den Auto-Newsletter anmelden!</h2><p>Sie wollen keinen Artikel unserer Auto-Experten '
    'verpassen? Dann melden Sie sich jetzt kostenlos an.</p><button>Anmelden</button></section>'
    '</article></main></body></html>')


def test_focus_promo_boxes_and_byline_are_dropped_but_author_box_stays():
    result = extract(FOCUS_ARTICLE.encode(), FOCUS_URL)
    assert "Krypto-Dienstleister" in result.body_text and "Gewerbe- und Umsatzsteuer" in result.body_text
    assert "bevorzugte Quelle" not in result.body_text and "google.com" not in result.body_html
    assert "Expertenkreises" in result.body_text
    assert "Newsletter" not in result.body_text and "Marvin Haas" not in result.body_text
    assert "marvin-haas.jpg" not in result.body_html


# ---- Paket A: Volltext (Vorspann, Paywall, Liveblogs, Beifang, JSON-LD, Bilder, Autoren) ----------

def _page(body: str, head: str = "") -> bytes:
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>T</title>{head}</head>'
            f"<body>{body}</body></html>").encode()


def _paras(*texts: str) -> str:
    return "".join(f"<p>{t}</p>" for t in texts)


ARTICLE_PARAS = _paras(
    "Der Flugzeugbauer Boeing hat einen Fehler in der Software für die automatische Landenavigation gemeldet. "
    "Betroffen sind nach Angaben des Unternehmens mehrere Hundert Maschinen weltweit.",
    "Die Airlines wurden aufgefordert, bestimmte Anflugverfahren bis auf Weiteres zu unterlassen. Ingenieure "
    "arbeiteten an einer dauerhaften Lösung, teilte das Unternehmen am Montag mit.",
    "»Dass sich solche Fehler wiederholen, ist nicht hinnehmbar«, sagte ein Sprecher der Aufsichtsbehörde FAA. "
    "Boeing steht seit Jahren unter strenger Beobachtung der Behörden.")
LEAD = ("Der Hersteller meldet einen Softwarefehler bei Hunderten Maschinen. Die Aufsicht reagiert "
        "ungewöhnlich scharf.")


def _og(desc: str) -> str:
    return f'<meta property="og:description" content="{desc}">'


def test_lost_lead_from_page_header_is_prepended():
    """SPIEGEL: Der Vorspann steht im Kopf (header[data-area=intro]) und fiel bei trafilatura weg."""
    page = _page('<main><article><header><h2>Softwarefehler bei Boeing</h2><div data-area="intro">'
                 f'<div class="RichText">{LEAD}</div></div></header>'
                 f'<div data-area="body">{ARTICLE_PARAS}</div></article></main>', _og(LEAD))
    result = extract(page, BASE)
    assert result.body_text.count("Aufsicht reagiert ungewöhnlich scharf") == 1
    assert result.body_text.index("Hunderten Maschinen") < result.body_text.index("Landenavigation")
    assert f"<p>{LEAD}</p>" in result.body_html


def test_truncated_description_finds_the_whole_lead():
    """Daily Maverick kürzt og:description („…“); der Vorspann in div.top-summary beginnt damit."""
    page = _page(f'<header class="titles"><h1>Kaffee</h1></header><div class="top-summary"><p>{LEAD}</p></div>'
                 f"<article>{ARTICLE_PARAS}</article>", _og(LEAD[:70] + "…"))
    result = extract(page, BASE)
    assert result.body_text.startswith(LEAD)


def test_lead_is_not_taken_from_links_lists_or_captions():
    teaser = f'<ul><li><a href="/x">{LEAD}</a></li></ul><figure><figcaption>{LEAD}</figcaption></figure>'
    page = _page(f"<nav>{teaser}</nav><article>{ARTICLE_PARAS}</article>", _og(LEAD))
    assert "Aufsicht reagiert" not in extract(page, BASE).body_text
    # Beschreibung schon im Text: nichts doppelt
    page = _page(f"<article><p>{LEAD}</p>{ARTICLE_PARAS}</article>", _og(LEAD))
    assert extract(page, BASE).body_text.count("Aufsicht reagiert") == 1


def test_sz_teaser_text_is_the_lead():
    """SZ: p[data-manual=teaserText], auch wenn die Beschreibung anders lautet."""
    page = _page(f'<header><p data-manual="teaserText">{LEAD}</p></header><article>{ARTICLE_PARAS}</article>',
                 _og("Eine ganz andere, kürzere Beschreibung der Seite für Suchmaschinen und soziale Netze."))
    assert extract(page, BASE).body_text.startswith(LEAD)


def test_left_to_read_alone_does_not_mark_paywall():
    page = _page(f"<article>{ARTICLE_PARAS}<p>Half of this article left to read, he joked, is about "
                 "cats and the weather in Paris during the summer.</p></article>")
    assert extract(page, BASE).paywalled is False


def test_le_monde_subscriber_note_marks_paywall_and_is_dropped():
    page = _page(f"<article>{ARTICLE_PARAS}<p>You have 81.65% of this article left to read. The rest is for "
                 "subscribers only.</p></article>")
    result = extract(page, BASE)
    assert result.paywalled is True and result.status == "partial"
    assert "subscribers" not in result.body_text and "Landenavigation" in result.body_text


def _guardian_block(n: int, title: str, text: str, avatar: bool = False) -> str:
    img = ('<div><img src="https://i.guim.co.uk/img/uploads/2023/09/18/Rajeev_Syal.png?width=300" alt="Rajeev Syal">'
           '</div><span>Rajeev Syal</span>') if avatar else ""
    return (f'<article id="block-{n}"><header><div><a href="?page=with%3Ablock-{n}#block-{n}"><span>'
            f'<gu-island name="RelativeTime"><time datetime="2026-09-28T15:{n}:21.000Z">{n}m ago</time></gu-island>'
            f'</span><span>17.{n} CEST</span></a></div><h2>{title}</h2>{img}</header>'
            f'<p><strong>{text}</strong></p><p>{text} Weitere Einzelheiten folgen im Laufe des Tages.</p>'
            f'<blockquote><p>Zitat aus Eintrag {n}: Wir werden das nicht hinnehmen.</p></blockquote>'
            f'<footer><gu-island name="ShareButton"><a href="mailto:?subject=x">Share</a></gu-island></footer>'
            '</article>')


def _guardian_pinned(body: str) -> str:
    return ('<div id="pinned-post" data-component="pinned-post"><input type="checkbox" id="pinned-post-checkbox">'
            '<div><div>Angeheftet <gu-island name="RelativeTime"><time datetime="2026-09-28T10:23:40.000Z">5h ago'
            '</time></gu-island></div></div><div id="collapsible-body"><article><header><div><a href="#b"><span>'
            f"12.23 CEST</span></a></div><h2>Zusammenfassung</h2></header>{body}</article></div></div>")


def test_guardian_closing_post_is_kept_on_top(monkeypatch):
    """Der angeheftete „Closing post“ steht in keinem Eintrag der Liste: er bleibt, die Einträge auch."""
    monkeypatch.delenv("KIOSK_TZ", raising=False)
    blocks = "".join(_guardian_block(n, f"Überschrift Eintrag {n}",
                                     f"Eintrag {n} berichtet ausführlich über die Lage am Parteitag in Liverpool.")
                     for n in range(10, 16))
    closing = "".join(f"<p>Schlussbeitrag Punkt {n}: Die Konferenz endet heute Abend mit einer Rede der "
                      f"Parteichefin, danach beginnt die Aussprache.</p>" for n in range(8))
    page = _page(f'<main><div id="liveblog-body">{_guardian_pinned(closing)}{blocks}</div></main>')
    result = extract(page, "https://www.theguardian.com/politics/live/2026/sep/28/x")
    for n in range(8):
        assert f"Schlussbeitrag Punkt {n}:" in result.body_text, n
    for n in range(10, 16):
        assert f"Überschrift Eintrag {n}" in result.body_text, n
    assert result.body_text.index("Schlussbeitrag Punkt 0") < result.body_text.index("Überschrift Eintrag 10")
    assert "Angeheftet" not in result.body_text and "5h ago" not in result.body_text


def test_guardian_live_blog_keeps_all_blocks_with_headings_and_quotes(monkeypatch):
    monkeypatch.delenv("KIOSK_TZ", raising=False)
    blocks = "".join(_guardian_block(n, f"Überschrift Eintrag {n}",
                                     f"Eintrag {n} berichtet ausführlich über die Lage am Parteitag in Liverpool.",
                                     avatar=n == 12) for n in range(10, 16))
    pinned = _guardian_pinned("<p>Eintrag 15 berichtet ausführlich über die Lage am Parteitag in Liverpool.</p>"
                              "<p>Eintrag 15 berichtet ausführlich über die Lage am Parteitag in Liverpool. Weitere "
                              "Einzelheiten folgen im Laufe des Tages.</p>")   # Kopie eines Eintrags
    page = _page(f'<main><div id="liveblog-body">{pinned}{blocks}</div></main>')
    result = extract(page, "https://www.theguardian.com/politics/live/2026/sep/28/x")
    for n in range(10, 16):
        assert f"Überschrift Eintrag {n}" in result.body_text, n
        assert f"Zitat aus Eintrag {n}" in result.body_text, n
    assert "<h2>" in result.body_html and "<blockquote>" in result.body_html
    assert "Angeheftet" not in result.body_text and "m ago" not in result.body_text
    assert result.body_text.count("Eintrag 15 berichtet") == 2   # nur der Eintrag, nicht die Kopie
    assert "Rajeev_Syal.png" not in result.body_html and "Share" not in result.body_text
    assert "Rajeev Syal" not in result.body_text   # Autorenname im Kopf des Eintrags
    for n in range(10, 16):   # feste Zeit (datetime, Zeitzone der App) vor jedem Eintrag
        assert re.search(rf"<p>\s*<strong>28\.09\. 17:{n}</strong>\s*</p>\s*<h2>Überschrift Eintrag {n}</h2>",
                         result.body_html), n
    # ohne datetime: die angezeigte feste Zeit
    page = _page(f'<main><div id="liveblog-body">{blocks.replace(" datetime=", " data-x=")}</div></main>')
    result = extract(page, "https://www.theguardian.com/politics/live/2026/sep/28/x")
    assert re.search(r"<strong>17\.10 CEST</strong>", result.body_html)


def test_live_blog_times_use_the_app_time_zone(monkeypatch):
    from app.extract import _local_time
    monkeypatch.setenv("KIOSK_TZ", "UTC")
    assert _local_time("2026-09-28T12:43:55.000Z") == "28.09. 12:43"
    monkeypatch.setenv("KIOSK_TZ", "Nicht/Da")
    assert _local_time("2026-09-28T12:43:55.000Z") == "28.09. 14:43"   # Rückfall: Europe/Berlin
    assert _local_time("gestern") is None
    assert _local_time("2026-09-28") is None   # nur ein Datum: keine Uhrzeit erfinden
    monkeypatch.setenv("KIOSK_TZ", "../../etc/passwd")
    assert _local_time("2026-09-28T12:43:55Z") == "28.09. 14:43"


def test_preclean_drops_screen_reader_text_and_teaser_boxes_but_keeps_srf_offscreen():
    import lxml.html
    from app.extract import _precleaned
    junk = [
        '<span class="visually-hidden">, external</span>', '<a class="screen-reader-text" href="#m">Skip links</a>',
        '<span class="x sr-only">Zur Merkliste</span>', '<div data-testid="promo"><a href="/p">Promo BBC</a></div>',
        '<div data-testid="topic-list">Related topics Fairford</div>',
        '<div data-testid="metadata"><ul><li>Published 27 September</li></ul></div>',
        '<div data-block="uploaderEmbed"><h2>Get in touch</h2><p>Share your experiences.</p></div>',
        '<section class="more-on"><h2>Recommended Stories</h2></section>',
        '<div class="bucketwrap internallink insettwocolumn">NPR intern</div>', '<b class="hide-caption">hide caption</b>',
        '<b class="toggle-caption">toggle caption</b>', '<dl class="tspBz debug-piano"><dt>showPaywall:</dt><dd>false</dd></dl>',
        '<div id="summy-content">Artikelzusammenfassung KI</div>', '<div class="tzi-bottom-container">Die taz schreibt</div>',
        '<div class="c-inline-alert c-article-page__preferred-sources">WELT bevorzugt</div>',
        '<div class="c-inline-teaser-list">Lesen Sie auch WELT</div>', '<figure class="c-inline-video">WELT Video</figure>',
        '<div class="teaser-absatz columns">tagesschau Teaser</div>', '<li class="teaser-xs columns">tagesschau klein</li>',
        '<div class="teaser">NDR Teaser</div>', '<div class="TeaserInline_wrapper__x1">ntv Teaser</div>',
        '<div class="VideoInline_wrapper__y2">ntv Video</div>', '<app-storyline-related-topics>Verwandte Themen</app-storyline-related-topics>',
        '<app-default-teaser>HB Teaser</app-default-teaser>', '<app-guidelines-reference>Richtlinien</app-guidelines-reference>',
        '<div class="tik4-sharing">Teilen FAZ</div>', '<a-opt-in>heise Opt-in</a-opt-in>',
        '<div class="ad--inread-header">Videos by heise</div>', '<div data-component="RecommendationBox">heise Empfehlung</div>',
        '<figure class="branding"><img src="https://heise.example/newsletter.png" alt="KI-Update"></figure>',
        '<a class="id-Story-googleFollowButton" href="https://profile.google.com/cp/x">Uns auf Google folgen</a>',
        '<img src="/pu_gea/image/logo.png" alt="gea.de">', '<div class="nfy-teaser nfy-teaser-small nfy-ar-teaser">GEA Teaser</div>',
        '<div class="nfy-detail-footer-inner nfy-ar-teaser-box nfy-teaser-box">GEA Kasten</div>', '<div class="bookmark-experience">Independent merken</div>',
        '<div class="show-comments">Join our commenting forum</div>', '<div class="sc-1 js-comment">Go to comments</div>',
        '<div class="contribute-article p-3">A Message from The Moscow Times</div>',
        '<section class="catcher catcher--favoris">Read more Le Monde</section>', '<a class="js-article-read-also" href="/r">Le Monde auch</a>',
        '<aside class="injection trinity-skip-it">CSM Recommended</aside>', '<div class="in-article-games">CNA Games</div>',
        '<div class="js-tcf-notloaded tcf-notloaded" hidden><p>Sie entscheiden darüber</p></div>',
        '<details data-gu-name="dateline"><summary>Mon 28 Sep 2026 17.31 CEST</summary></details>',
        '<section data-area="embed"><p>Ich bin damit einverstanden, dass mir externe Inhalte angezeigt werden.</p>'
        '</section>',
        '<span>Sie nutzen Google? <a href="https://google.com/preferences/source?q=x">direkt aktivieren.</a></span>',
    ]
    keep = ['<span class="h-offscreen">Autor Tobias Müller</span>', "<p>Echter Artikeltext bleibt stehen.</p>",
            '<section class="catcher-free">bleibt</section>']
    page = _page("<article>" + "".join(junk) + "".join(keep) + "</article>").decode()
    out = lxml.html.tostring(_precleaned(page.encode()), encoding="unicode")
    for piece in junk:
        el = lxml.html.fromstring(piece)
        marker = el.text_content().strip() or el.xpath("descendant-or-self::img/@src")[0]
        assert marker not in out, piece
    assert "Autor Tobias Müller" in out and "Echter Artikeltext" in out and "bleibt" in out


@pytest.mark.parametrize("para", [
    "Klicken Sie hier, um sich Artikel von WELT in der Google-Suche bevorzugt anzeigen zu lassen.",
    "Uns auf Google folgen", "Folgen Sie uns auf Google News, um nichts zu verpassen.",
    "Sie nutzen Google? Richten Sie uns als bevorzugte Quelle ein.",
    'Google: <a href="https://www.google.com/preferences/source?q=x">Jetzt einrichten</a>',
    "If you rely on our team for trusted reporting, please select us as your Preferred Source on Google",
    "Lesen Sie auch", "Lesen Sie mehr zum Thema", "Stand:", "Stand: 28.09.2026 18:07 Uhr", "Lesezeit: 3 Min.",
    "Stand: 28. September 2026, 18:07 Uhr", "Stand: heute, 17:45 Uhr",
    "Videos by heise", "Link kopieren", "Auf Facebook teilen", "Auf X teilen", "Per E-Mail teilen",
    "At Meduza, we are committed to transparency about our use of artificial intelligence. " + "x " * 200,
    "If you find any errors in this translation, please contact us at [email protected].",
    "To read Meduza’s exclusive content in English, please subscribe to our newsletter.",
    "Sign up for our weekly newsletter to get more English-language news coverage from EL PAÍS USA Edition",
    "Sign up now: Get ST's newsletters delivered to your inbox",
    "Listen to the best of BBC Radio Merseyside on Sounds and follow BBC Merseyside on Facebook",
    "Get in touch",
    "Get the latest India News and Live updates. Download the TOI app.",
    "Ich bin damit einverstanden, dass mir externe Inhalte angezeigt werden. Damit können personenbezogene Daten "
    "an Drittplattformen übermittelt werden.",
    "Empfohlener externer Inhalt", "Wir würden Ihnen hier gerne einen externen Inhalt zeigen. Sie entscheiden, ob "
    "Sie dies auch wollen.",
])
def test_promo_and_label_paragraphs_are_dropped(para):
    html, text = sanitize(f"<p>Vorher.</p><p>{para}</p><p>Nachher.</p>", BASE)
    assert html == "<p>Vorher.</p><p>Nachher.</p>" and text == "Vorher. Nachher."


@pytest.mark.parametrize("para", [
    "Apple und Google folgen dem Beispiel des Konkurrenten.",
    "Die EU wirft Google vor, eigene Angebote bevorzugt anzuzeigen.",
    "Laut Gericht hat Google eigene Dienste bevorzugt.",
    "Sie werfen Google vor, den Vorgaben nicht zu folgen.",
    "Stand der Dinge: Die Verhandlungen dauern an.",
    "Stand: 2:1 für Leverkusen nach 70 Minuten.",
    "Lesezeit: eine Stunde, sagt der Verlag, sei ideal.",
    "Der Konzern bevorzugt nach eigenen Angaben heimische Zulieferer.",
    "Google " + "bevorzugt " * 40,   # lang: Artikeltext
    "Sign up sheets were posted at the town hall on Monday.",
])
def test_promo_rules_keep_real_sentences(para):
    html, _ = sanitize(f"<p>{para}</p>", BASE)
    assert para.strip() in html


def test_label_headings_are_dropped():
    html, _ = sanitize('<h2><a href="/news/">NewsFeed</a></h2><h3>Lesen Sie auch</h3><p>Text.</p>'
                       "<h2>Lesen Sie auch, was Experten sagen</h2>", BASE)
    assert html == '<p>Text.</p><h2>Lesen Sie auch, was Experten sagen</h2>'


@pytest.mark.parametrize("raw", ["Bundes­regierung", "Bundes​regierung", "Bundes‌regierung",
                                 "Bundes⁠regierung", "﻿Bundesregierung", "Bundes&shy;regierung"])
def test_invisible_characters_are_removed(raw):
    html, text = sanitize(f"<p>Die {raw} tagt.</p>", BASE)
    assert html == "<p>Die Bundesregierung tagt.</p>" and text == "Die Bundesregierung tagt."
    assert html_to_text(f"<p>{raw}</p>") == "Bundesregierung"
    assert strip_html(f"Die {raw}") == "Die Bundesregierung"


def test_json_ld_article_body_replaces_page_chrome():
    """Times of India: trafilatura fand nur Bedienelemente, der Text steht im JSON-LD."""
    body = ("NEW DELHI: The Supreme Court on Monday recommended the elevation of three high court chief justices. "
            "The collegium met in the afternoon and approved the names unanimously.\\n"
            "The government is expected to notify the appointments within weeks. Officials said the process "
            "would be completed before the end of the year.")
    ld = f'<script type="application/ld+json">{{"@type":"NewsArticle","articleBody":"{body}"}}</script>'
    chrome = _paras("Sign In", "Comments", "Share", "AaText Size", "Gujarat, Delhi, Telangana high court CJs "
                    "recommended for SC elevation after a long wait", "End of Article", "Follow Us On Social Media")
    result = extract(_page(f"<main>{chrome}</main>", ld), BASE)
    assert result.body_html.startswith("<p>NEW DELHI: The Supreme Court") and "Sign In" not in result.body_text
    assert "before the end of the year" in result.body_text
    # Die Regel selbst: Absätze aus den Zeilenumbrüchen; nichts zu tun, wenn der Text schon da ist.
    from app.extract import _ld_fallback
    ld_objects = [{"@type": "NewsArticle", "articleBody": body.replace("\\n", "\n"), "url": BASE}]
    urls = frozenset({"news.example/artikel/1"})
    clean, text, _ = _ld_fallback(ld_objects, html_to_text(chrome), BASE, None, urls)
    assert clean.count("<p>") == 2 and clean.startswith("<p>NEW DELHI:") and "Sign In" not in text
    assert _ld_fallback(ld_objects, text, BASE, None, urls) == ("", "", False)
    assert _ld_fallback([{"articleBody": "Zu kurz.", "url": BASE}], "", BASE, None, urls) == ("", "", False)


def test_json_ld_body_must_belong_to_the_page():
    """Nur das articleBody des eigenen Artikels (URL oder Überschrift), nicht das längste irgendwo."""
    from app.extract import _ld_fallback, _ld_objects
    from trafilatura.utils import load_html
    own = "The court met on Monday and recommended three judges for elevation to the top court of the country."
    other = "A completely different story about football results and transfer rumours in the league. " * 10
    ld = ('<script type="application/ld+json">[{"@type":"NewsArticle","url":"https://news.example/artikel/1",'
          f'"headline":"Judges recommended","articleBody":"{own * 3}"}},{{"@type":"NewsArticle",'
          f'"url":"https://news.example/other","headline":"Football","articleBody":"{other}"}}]</script>')
    objects = _ld_objects(load_html(_page("<p>Sign In</p>", ld)))
    clean, text, _ = _ld_fallback(objects, "Sign In", BASE, "Judges recommended",
                                  frozenset({"news.example/artikel/1"}))
    assert "court met on Monday" in text and "football" not in text
    clean, text, _ = _ld_fallback(objects, "Sign In", BASE, "Etwas anderes", frozenset({"news.example/x"}))
    assert text == ""   # keines gehört zur Seite


def test_json_ld_sentences_run_together_become_paragraphs():
    """Times of India hängt Absätze ohne Leerzeichen aneinander: „… top court.The collegium …“."""
    from app.extract import _ld_article_body
    raw = ("NEW DELHI: The Supreme Court recommended three chief justices as judges of the top court.The collegium "
           "met on Monday.The U.S.Army was not involved, officials said.")
    body = _ld_article_body([{"articleBody": raw, "headline": "T"}], title="T")
    assert body.split("\n") == ["NEW DELHI: The Supreme Court recommended three chief justices as judges of the "
                                "top court.", "The collegium met on Monday.",
                                "The U.S.Army was not involved, officials said."]


def test_json_ld_body_is_ignored_when_the_text_already_has_it():
    body = "Der Flugzeugbauer Boeing hat einen Fehler in der Software für die automatische Landenavigation gemeldet."
    ld = f'<script type="application/ld+json">{{"@type":"NewsArticle","articleBody":"{body} {body}"}}</script>'
    result = extract(_page(f"<article>{ARTICLE_PARAS}</article>", ld), BASE)
    assert "Aufsichtsbehörde FAA" in result.body_text


def test_long_json_ld_body_is_split_into_paragraphs():
    from app.extract import _ld_paragraphs
    sentence = "Das ist ein ziemlich langer Satz über die Lage, der sich immer wiederholt."
    paras = _ld_paragraphs(" ".join([sentence] * 40))
    assert len(paras) > 3 and all(p.endswith(".") for p in paras)
    assert all(len(p) < 800 for p in paras) and " ".join(paras) == " ".join([sentence] * 40)


def test_golem_consent_page_is_stored_without_text():
    page = _page('<div id="screen"><h1>Willkommen auf Golem.de!</h1><section class="c2_left"><h2>Cookies zustimmen</h2>'
                 '<p class="intro">Besuchen Sie Golem.de wie gewohnt mit Werbung und Tracking, indem Sie der Nutzung '
                 "aller Cookies zustimmen. Details zum Tracking finden Sie im Privacy Center.</p>"
                 + _paras(*[f"Für die Ihnen angezeigten Verarbeitungszwecke können Cookies verwendet werden ({n})."
                            for n in range(12)]) + "</section></div>")
    result = extract(page, "https://www.golem.de/news/x.html")
    assert result.status == "empty" and result.body_html is None
    # Ein langer Artikel, der den Satz zitiert, bleibt
    quote = ("<p>Golem zeigt Besuchern ohne Einwilligung den Satz: „Besuchen Sie Golem.de wie gewohnt mit Werbung "
             "und Tracking“, erklärte der Verlag.</p>")
    article = _page(f"<article>{ARTICLE_PARAS * 8}{quote}</article>")
    result = extract(article, "https://www.golem.de/news/y.html")
    assert result.status == "full" and "erklärte der Verlag" in result.body_text


def test_derstandard_consent_dialog_is_not_extracted():
    dialog = ('<div class="js-tcf-notloaded tcf-notloaded" hidden><h1>Willkommen bei DER STANDARD</h1><p><strong>Sie '
              "entscheiden darüber, wie Sie unsere Inhalte nutzen wollen. Ihr Gerät erlaubt uns derzeit leider nicht, "
              "die entsprechenden Optionen anzuzeigen.</strong></p><p><strong>Bitte deaktivieren</strong> Sie sämtliche "
              "Hard- und Software-Komponenten, die in der Lage sind Teile unserer Website zu blockieren.</p></div>")
    result = extract(_page(f'<article><div class="article-text">{ARTICLE_PARAS}</div></article>{dialog}'), BASE)
    assert "Landenavigation" in result.body_text and "entscheiden" not in result.body_text
    html, _ = sanitize("<p>Text.</p><p>Sie entscheiden darüber, wie Sie unsere Inhalte nutzen wollen.</p>", BASE)
    assert html == "<p>Text.</p>"


def test_headline_echo_with_kicker_is_dropped():
    from app.extract import drop_title_echo
    res = drop_title_echo(_extraction("<h2>Enteignung per Dekret Putin stellt deutschen Großhändler Metro unter "
                                      "Zwangsverwaltung</h2><img src=\"https://a.example/b.jpg\"><p>Text folgt.</p>"),
                          "Russland stellt deutschen Großhändler Metro unter Zwangsverwaltung")
    assert res.body_html == '<img src="https://a.example/b.jpg"><p>Text folgt.</p>'
    img_first = drop_title_echo(_extraction('<img src="https://a.example/b.jpg"><h1>Fluggastrechte Verbraucherschützer '
                                            'mahnen Airlines ab</h1><p>Text.</p>'),
                                "Europäische Verbraucherschützer mahnen Airlines ab")
    assert "<h1>" not in img_first.body_html


def test_headline_with_long_kicker_is_dropped():
    from app.extract import drop_title_echo
    res = drop_title_echo(_extraction("<h2>Verhandlungen in der Chemiebranche Evonik schlägt BASF-Übernahmeangebot "
                                      "aus</h2><p>Text folgt.</p>"), "Chemie: Evonik schlägt BASF-Übernahmeangebot aus")
    assert res.body_html == "<p>Text folgt.</p>"
    # Eine Zwischenüberschrift mit wenigen Titelwörtern bleibt
    other = _extraction("<h2>Was die Übernahme für Beschäftigte bedeutet</h2><p>Text.</p>")
    assert drop_title_echo(other, "Chemie: Evonik schlägt BASF-Übernahmeangebot aus") is other


def test_headline_rule_keeps_headings_inside_the_article():
    from app.extract import drop_title_echo
    later = _extraction("<p>Einleitung mit Text.</p><h2>Putin stellt Metro unter Zwangsverwaltung</h2><p>Mehr.</p>")
    assert drop_title_echo(later, "Putin stellt Metro unter Zwangsverwaltung") is later
    other = _extraction("<h2>Was jetzt passiert</h2><p>Text.</p>")
    assert drop_title_echo(other, "Putin stellt Metro unter Zwangsverwaltung") is other


def test_h1_equal_to_title_is_dropped_anywhere():
    from app.extract import drop_title_echo
    body = ("<p>REUTLINGEN. Zwei Kindergärten wurden durchwühlt.</p><h1>Einbrecher durchwühlen zwei Kindergärten</h1>"
            "<p>Die Polizei ermittelt.</p>")
    res = drop_title_echo(_extraction(body), "Einbrecher durchwühlen zwei Kindergärten")
    assert "<h1>" not in res.body_html and "Polizei ermittelt" in res.body_text
    kicker = _extraction("<p>© dpa</p><h1>Fußball-Bundesliga BVB verlängert</h1><p>Text.</p>")
    assert "<h1>" not in drop_title_echo(kicker, "Fußball-Bundesliga: BVB verlängert").body_html


def test_author_contained_in_title_is_dropped():
    from app.extract import Extraction, drop_title_echo
    res = Extraction("full", "<p>Text.</p>", "Text.", 5, False, None, "Auffahrunfall")
    assert drop_title_echo(res, "Auffahrunfall in Zwiefalten: Motorradfahrerin leicht verletzt").author is None
    res = Extraction("full", "<p>Text.</p>", "Text.", 5, False, None, "Red")
    assert drop_title_echo(res, "Gerede um den Haushalt").author == "Red"
    res = Extraction("full", "<p>Text.</p>", "Text.", 5, False, None, "Einbrecher durchwühlen zwei Kindergärten")
    assert drop_title_echo(res, "Einbrecher durchwühlen zwei Kindergärten in Gönningen").author is None
    res = Extraction("full", "<p>Text.</p>", "Text.", 5, False, None, "Friedrich Merz")
    assert drop_title_echo(res, "Gastbeitrag von Friedrich Merz: Europa muss handeln").author == "Friedrich Merz"


@pytest.mark.parametrize("raw,expected", [
    ("Tagesschau De", None), ("N-Tv Nachrichten", None), ("Deutschlandfunk De", None), ("Ndr De", None),
    ("No author; Andreas Neuhaus", "Andreas Neuhaus"), ("Autor", None), ("Heise Online; Stefan Krempl", "Stefan Krempl"),
    ("Autor Tobias Müller Redaktor", "Tobias Müller"), ("Autorin Livia Middendorp", "Livia Middendorp"),
    ("Red; ORF at; Agenturen", "Red, Agenturen"), ("Anna Muster; Anna Muster", "Anna Muster"),
    ("Julia Bergmann; Juri Auel", "Julia Bergmann, Juri Auel"), (None, None), ("null", None),
    ("DER SPIEGEL", None), ("Alina Schadwinkel; DER SPIEGEL", "Alina Schadwinkel"), ("Maria De", "Maria De"),
    ("Anna Online", "Anna Online"),
    ("Ian Sample Science editor", "Ian Sample"), ("Ashifa Kassam European community affairs correspondent",
                                                  "Ashifa Kassam"),
    ("Ramon Antonio Vargas Global affairs reporter", "Ramon Antonio Vargas"), ("Jane Doe reporter", "Jane Doe"),
    ("Andrew Sparrow", "Andrew Sparrow"), ("The Editor", None),
    ("David Maddox; Home Affairs Correspondent", "David Maddox"), ("Moscow Times Reporter", None),
    ("Martin Chulov Middle East correspondent", "Martin Chulov"),
    ("Shaun Walker Central and eastern Europe correspondent", "Shaun Walker"),
    ("Mary Ann Smith political editor", "Mary Ann Smith"), ("Mark Reporter", "Mark Reporter"),
    ("Shrai Popat (now) and Tom Ambrose (earlier)", "Shrai Popat, Tom Ambrose"),
    ("Angelique Chrisafis in Paris", "Angelique Chrisafis"), ("Julia Surkowa, Gaby Coldewey und Barbara Oertel",
                                                             "Julia Surkowa, Gaby Coldewey, Barbara Oertel"),
    ("Von der Leyen, Ursula", "Von der Leyen, Ursula"), ("CNA", None), ("The Straits Times", None),
    ("Le Monde", None), ("Meduza", None), ("The Christian Science Monitor; Pan Pylas", "Pan Pylas"),
    ("Jane Doe, Senior correspondent", "Jane Doe"), ("Ilanit Spinner, ARD Berlin", "Ilanit Spinner"),
    ("Anna Muster, BR; Paul Schulz", "Anna Muster, Paul Schulz"), ("A" * 5000 + "; " + "B " * 5000, None), ("Chris Brown, political reporter", "Chris Brown"),
    ("Smith, John, and Doe, Jane", "Smith, John, Doe, Jane"), ("Johnson & Johnson", "Johnson & Johnson"),
    ("Anna Meier & Paul Schulz", "Anna Meier, Paul Schulz"), ("Anna" + " " * 50_000 + "(now)", "Anna"),
    ("A Eins; B Zwei; C Drei; D Vier; E Fünf", "A Eins, B Zwei, C Drei u. a."),
    ("; ".join(f"Vorname{n} Nachname{n}" for n in range(14)), "Vorname0 Nachname0, Vorname1 Nachname1, "
     "Vorname2 Nachname2 u. a."),
])
def test_clean_author(raw, expected):
    from app.extract import clean_author
    assert clean_author(raw) == expected


HEISE_FIGURE = (
    '<figure><div class="article-image__gallery-container"><a-img width="8103" height="4553" layout="responsive" '
    'src="/imgs/18/5/1/shutterstock_2705357347-12b280dba25485df.jpeg" alt="Beleuchtetes Nvidia-Logo" quality="85">'
    '<img src="data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\'%3E%3C/svg%3E" alt="Beleuchtetes Nvidia-Logo">'
    '</a-img><noscript><img src="https://heise.cloudimg.io/v7/_www-heise-de_/imgs/18/5/1/shutterstock_2705357347'
    '-12b280dba25485df.jpeg?org_if_sml=1&amp;q=50&amp;width=696" alt="Beleuchtetes Nvidia-Logo"></noscript></div>'
    '<figcaption><p class="source">(Bild: Robert Way/Shutterstock.com)</p></figcaption></figure>'
    '<a-img src="${image}" alt="${title}"><img src="data:image/svg+xml,x"></a-img>')


def test_heise_a_img_becomes_a_real_image():
    page = _page(f"<article><p>Nvidia baut seine Sicherheitsarchitektur für KI-Agenten aus.</p>{HEISE_FIGURE}"
                 f"{ARTICLE_PARAS}</article>")
    result = extract(page, "https://www.heise.de/news/x.html")
    assert "https://heise.cloudimg.io/v7/_www-heise-de_/imgs/18/5/1/shutterstock_2705357347" in result.body_html
    assert "${" not in result.body_html and "data:image" not in result.body_html
    assert "(Bild: Robert Way/Shutterstock.com)" in result.body_text   # Bildnachweis mit Bild bleibt


def test_image_url_without_extension_is_kept():
    """WELT: …/dpa-com-260928-935-1641004-jpg hat keine Dateiendung; trafilatura verwarf das Bild."""
    src = "https://images.welt.de/6aba/eb01/ci23x11-w1200/dpa-com-260928-935-1641004-jpg"
    page = _page(f'<article><figure><img src="{src}" alt="Metro-Logo"></figure>{ARTICLE_PARAS}</article>')
    result = extract(page, BASE)
    assert f'<img src="{src}"' in result.body_html and "#.jpg" not in result.body_html


def test_orphan_image_credits_are_dropped():
    html, _ = sanitize("<p>Vorspann des Artikels.</p><p>(Bild: Robert Way/Shutterstock.com)</p><p>Text.</p>", BASE)
    assert html == "<p>Vorspann des Artikels.</p><p>Text.</p>"
    html, _ = sanitize('<p>Text.</p><img src="https://a.example/b.jpg"><p>Foto: dpa</p>', BASE)
    assert "Foto: dpa" in html
    far = "<p>" + "Langer Absatz. " * 30 + "</p>"
    html, _ = sanitize(f'<img src="https://a.example/b.jpg">{far}<p>(Quelle: Reuters)</p><p>Foto: dpa</p>', BASE)
    assert "Quelle: Reuters" not in html and "Foto: dpa" not in html
    # „Quelle:“ ohne Klammern bleibt (Statistik, Text), auch fern von jedem Bild; unter einer Tabelle ohnehin
    html, _ = sanitize(f'<img src="https://a.example/b.jpg">{far}<p>Quelle: Reuters</p>', BASE)
    assert "Quelle: Reuters" in html
    html, _ = sanitize(f"{far}<table><tr><td>2025</td><td>3,1 %</td></tr></table><p>(Quelle: Statistisches "
                       "Bundesamt)</p>", BASE)
    assert "Statistisches Bundesamt" in html


def test_orphan_caption_scan_is_linear():
    """Viele Bildnachweise ohne Bild in einem langen Text: früher quadratisch (64 KB: 60 s)."""
    body = ("<p>" + "Absatz mit Text. " * 20 + "</p><p>(Bild: dpa)</p>") * 2_000
    t0 = time.monotonic()
    html, _ = sanitize(body, BASE)
    assert time.monotonic() - t0 < 5
    assert "(Bild: dpa)" not in html
    from app.extract import _drop_orphan_captions
    raw = ('<p><img src="https://a.example/b.jpg"></p>' + "<p>(Bild: dpa)</p>" * 20_000)
    t0 = time.monotonic()
    _drop_orphan_captions(raw)
    assert time.monotonic() - t0 < 5
    html, _ = sanitize("<p>Text.</p><p>Quelle: Das Statistische Bundesamt hat die Zahlen (vorläufig) veröffentlicht, "
                       "sie gelten als verlässlich.</p>", BASE)
    assert "Statistische Bundesamt" in html


def test_guardian_image_without_logo_overlay_is_preferred():
    base = "https://i.guim.co.uk/img/media/7043ac/507_0_4762_3810/master/4762.jpg"
    og = f"{base}?width=1200&height=630&quality=85&auto=format&fit=crop&overlay-align=bottom%2Cleft&overlay-base64=L2lt&s=1"
    ld = ('<script type="application/ld+json">{"@type":"NewsArticle","image":["' + og + '","' + base
          + '?width=1200&height=900&quality=85&auto=format&fit=crop&s=2","' + base
          + '?width=1200&quality=85&auto=format&fit=max&s=3"]}</script>')
    result = extract(_page(f"<article>{ARTICLE_PARAS}</article>", f'<meta property="og:image" content="{og}">' + ld),
                     BASE)
    assert result.image_url == f"{base}?width=1200&quality=85&auto=format&fit=max&s=3"


def test_meduza_text_card_is_no_image():
    card = "https://meduza.io/imgly/share/1790580532/en/news/2026/09/28/russian-strike"
    result = extract(_page(f"<article>{ARTICLE_PARAS}</article>", f'<meta property="og:image" content="{card}">'), BASE)
    assert result.image_url is None


def test_tickaroo_timestamps_become_paragraphs_and_share_buttons_go(monkeypatch):
    monkeypatch.delenv("KIOSK_TZ", raising=False)
    import lxml.html
    from app.extract import _precleaned
    event = ('<div class="tik4-event"><div class="tik4-meta"><time datetime="2026-09-28T12:43:55.000Z" '
             'class="tik4-meta__ts"><span class="tik4-meta__ts__date">Heute</span> <span class="tik4-meta__ts__time">'
             '12:43 Uhr</span></time></div><div class="tik4-author__thumb"><img src="https://cdn.tickaroo.com/a.webp">'
             '</div><div class="tik4-sharing"><a href="#">Teilen</a></div><div class="tik4-content-block"><h2>Eintrag'
             "</h2><p>Meldung.</p></div></div>")
    out = lxml.html.tostring(_precleaned(_page(event)), encoding="unicode")
    # feste Zeit aus datetime (deutsche Sommerzeit: UTC+2), nicht das relative „Heute 12:43 Uhr“
    assert "<p><strong>28.09. 14:43</strong></p>" in out and "Heute" not in out
    assert "Teilen" not in out and "tickaroo.com/a.webp" not in out and "Meldung." in out
    relative = event.replace(' datetime="2026-09-28T12:43:55.000Z"', "").replace("Heute", "Vor 44 Minuten")
    out = lxml.html.tostring(_precleaned(_page(relative)), encoding="unicode")
    assert "Minuten" not in out and "<strong>" not in out   # ohne feste Zeit: weg
    named = event.replace('<div class="tik4-author__thumb">', '<div class="tik4-author"><span>Anna Schimpp</span>'
                          '</div><div class="tik4-author__thumb">')
    assert "Anna Schimpp" not in lxml.html.tostring(_precleaned(_page(named)), encoding="unicode")


# ---- Nachbesserung nach den Reviews -------------------------------------------------------------

def test_caption_regex_matches_bild_and_parentheses():
    from app.extract import CAPTION_RE
    for caption in ("Bild: dpa", "(Bild: Daimler Truck)", "Bilder: AP", "Foto: X", "Fotos: Y", "(Quelle: Z)",
                    "Photo: Reuters", "Image: AFP"):
        assert CAPTION_RE.match(caption), caption
    assert not CAPTION_RE.match("Bildung: Die Schulen öffnen wieder")


def test_duplicate_image_drops_its_parenthesised_credit():
    img = "https://a.example/bild.jpg"
    html, _ = sanitize(f'<p><img src="{img}"></p><p>Text.</p><p><img src="{img}?w=640"></p><p>(Bild: Daimler Truck)</p>',
                       BASE)
    assert html.count("<img") == 1 and "Daimler" not in html


def test_lead_search_is_bounded_for_hostile_descriptions():
    word = "x" * 100_000
    body = "<div>" + f"<p>{word[:50]} Text</p>" * 5_000 + "</div>"
    t0 = time.monotonic()
    result = extract(_page(f"<article>{ARTICLE_PARAS}</article>{body}", _og(word)), BASE)
    assert time.monotonic() - t0 < 10 and result.body_text
    long_desc = ("Ein langer Satz der Beschreibung. " * 60).strip()
    page = _page(f"<header><div>{long_desc}</div></header><article>{ARTICLE_PARAS}</article>", _og(long_desc))
    assert "Ein langer Satz" not in extract(page, BASE).body_text   # über _LEAD_MAX: kein Vorspann


def test_lead_is_not_the_headline():
    """Beschreibung = Titel (oder fast): nicht als Absatz einfügen; Überschriften sind keine Kandidaten."""
    title = "Boeing meldet Softwarefehler bei Hunderten Maschinen weltweit, die Aufsicht reagiert scharf"
    page = _page(f"<article><h2>{title}</h2>{ARTICLE_PARAS}</article>",
                 f"<title>{title}</title><meta property='og:title' content='{title}'>" + _og(title + "."))
    result = extract(page, BASE)
    assert f"<p>{title}" not in result.body_html
    page = _page(f"<article><h2>{LEAD}</h2>{ARTICLE_PARAS}</article>", _og(LEAD))
    assert f"<p>{LEAD}</p>" not in extract(page, BASE).body_html


def test_lead_does_not_push_the_body_over_the_limit(monkeypatch):
    from app.extract import _with_lead
    from trafilatura.utils import load_html
    tree = load_html(_page(f"<header><div>{LEAD}</div></header>", _og(LEAD)))
    clean = "<p>" + "x" * 100 + "</p>"
    monkeypatch.setattr(extract_mod, "MAX_BODY_HTML", len(clean) + 20)
    assert _with_lead(clean, "x" * 100, tree) == (clean, "x" * 100)


IMG = '<img src="https://a.example/b.jpg">'


@pytest.mark.parametrize("body,expected", [
    # heise: Vorspann, dann Titelbild samt Bildnachweis → Bild nach vorn, Nachweis in <figcaption>
    (f"<p>Nvidia baut seine Sicherheitsarchitektur aus.</p>{IMG}<p>(Bild: Robert Way/Shutterstock.com)</p>"
     "<p>Nvidia reagiert auf jüngste Vorfälle.</p>",
     f"<figure>{IMG}<figcaption>(Bild: Robert Way/Shutterstock.com)</figcaption></figure><p>Nvidia baut seine "
     "Sicherheitsarchitektur aus.</p><p>Nvidia reagiert auf jüngste Vorfälle.</p>"),
    # heise: Bildunterschrift mit Punkt, weil direkt ein Bildnachweis folgt
    (f"<p>Lead.</p>{IMG}<p>Bose Wired Earbuds.</p><p>(Bild: Bose)</p><p>Text.</p>",
     f"<figure>{IMG}<figcaption>Bose Wired Earbuds.<br>(Bild: Bose)</figcaption></figure><p>Lead.</p><p>Text.</p>"),
    # SPIEGEL: Bildnachweis als loser Text nach der Unterschrift …
    ('<img src="https://a.example/b.jpg" alt="Metro-Schild in Deutschland im Jahr 2021"><p>Metro-Schild in '
     "Deutschland im Jahr 2021</p>Foto: Beautiful Sports / IMAGO<p>Der russische Machthaber hat entschieden.</p>",
     '<figure><img src="https://a.example/b.jpg" alt="Metro-Schild in Deutschland im Jahr 2021"><figcaption>'
     "Metro-Schild in Deutschland im Jahr 2021<br>Foto: Beautiful Sports / IMAGO</figcaption></figure><p>Der "
     "russische Machthaber hat entschieden.</p>"),
    # … oder „Foto:“ lose und der Name als eigener Absatz (Unterschrift = Alt-Text, endet mit »…«)
    ('<img src="https://a.example/b.jpg" alt="Horst Lichter (2024): »Einer der großartigsten Tage«">'
     "<p>Horst Lichter (2024): »Einer der großartigsten Tage«</p>Foto:<p>Oliver Berg / dpa</p><p>Text.</p>",
     '<figure><img src="https://a.example/b.jpg" alt="Horst Lichter (2024): »Einer der großartigsten Tage«">'
     "<figcaption>Horst Lichter (2024): »Einer der großartigsten Tage«<br>Foto: Oliver Berg / dpa</figcaption>"
     "</figure><p>Text.</p>"),
    # schon als <figure>: Unterschrift kommt hinein
    (f"<figure>{IMG}</figure><p>Metro-Schild in Deutschland im Jahr 2021</p><p>Der russische Machthaber.</p>",
     f"<figure>{IMG}<figcaption>Metro-Schild in Deutschland im Jahr 2021</figcaption></figure>"
     "<p>Der russische Machthaber.</p>"),
    # <p><img></p>: das Bild kommt ohne Absatz in die <figure>
    (f"<p>{IMG}</p><p>(Bild: dpa)</p><p>Text.</p>", f"<figure>{IMG}<figcaption>(Bild: dpa)</figcaption></figure>"
     "<p>Text.</p>"),
    # GEA: die Unterschrift (Anfang = Alt-Text) steht als zweiter Absatz, sie kommt ans Bild
    ('<img src="https://a.example/b.jpg" alt="Zieldurchfahrt in Trochtelfingen: Der Münsinger Uwe Hardter gewinnt '
     'die Langdist"><p>Bei der 30. Auflage setzt sich Uwe Hardter durch.</p><p>Zieldurchfahrt in Trochtelfingen: '
     "Der Münsinger Uwe Hardter gewinnt die Langdistanz.</p><p>Rest.</p>",
     '<figure><img src="https://a.example/b.jpg" alt="Zieldurchfahrt in Trochtelfingen: Der Münsinger Uwe Hardter '
     'gewinnt die Langdist"><figcaption>Zieldurchfahrt in Trochtelfingen: Der Münsinger Uwe Hardter gewinnt die '
     "Langdistanz.</figcaption></figure><p>Bei der 30. Auflage setzt sich Uwe Hardter durch.</p><p>Rest.</p>"),
])
def test_arrange_top_puts_image_first_with_one_figcaption(body, expected):
    from app.extract import _arrange_top
    assert _arrange_top(body) == expected


@pytest.mark.parametrize("body", [
    "<p>" + "Langer erster Absatz. " * 40 + f"</p>{IMG}<p>Text.</p>",       # langer Absatz: kein Vorspann
    f"{IMG}<p><strong>17.31 CEST</strong></p><h2>Eintrag</h2><p>Text.</p>",  # Liveblog-Uhrzeit: keine Unterschrift
    f"{IMG}<p>Kurzer erster Satz.</p><p>Text.</p>",                          # Satz mit Punkt: Text
    f"{IMG}<p>Das sagte der Minister dazu:</p><p>Text.</p>",                 # Doppelpunkt am Ende
])
def test_arrange_top_leaves_text_paragraphs_alone(body):
    from app.extract import _arrange_top
    assert _arrange_top(body) == body


def test_page_figcaption_marks_a_caption_with_full_stop():
    """derStandard: Unterschrift mit Punkt, die auf der Seite in einer <figcaption> steht."""
    from app.extract import _arrange_top, _squash
    body = (f'<img src="https://a.example/b.jpg" alt="AFP/ANDREJ ISAKOVIC"><p>McLaren-Fahrer Lando Norris muss '
            "zurückrudern.</p><p>Baku - Weltmeister Lando Norris hat seine Forderung zurückgenommen.</p>")
    captions = frozenset({_squash("McLaren-Fahrer Lando Norris muss zurückrudern. Foto: AFP")})
    assert "<figcaption>McLaren-Fahrer Lando Norris muss zurückrudern.</figcaption></figure><p>Baku" in \
        _arrange_top(body, captions)
    assert _arrange_top(body) == body


def test_lead_position_is_after_image_and_figcaption():
    from app.extract import _lead_position
    body = f"<h2>T</h2>{IMG}<figcaption>Unterschrift</figcaption><p>Text.</p>"
    assert body[_lead_position(body):] == "<p>Text.</p>"
    live = f"{IMG}<p><strong>17.31 CEST</strong></p><h2>Eintrag</h2>"
    assert live[_lead_position(live):].startswith("<p><strong>17.31")


def test_lead_goes_after_the_leading_image_in_extract():
    page = _page(f'<main><article><header><h2>Softwarefehler</h2><div data-area="intro"><div class="RichText">{LEAD}'
                 '</div></div></header><figure><img src="https://a.example/hero.jpg" alt="Boeing 737"><figcaption>'
                 f'Eine Boeing 737 in Seattle</figcaption></figure><div data-area="body">{ARTICLE_PARAS}</div>'
                 "</article></main>", _og(LEAD))
    result = extract(page, BASE)
    # die Überschrift im <header> ist die der Seite (fällt weg, _page_headlines), das Bild steht vorn
    assert re.match(r"\s*(?:<figure>\s*)?<img", result.body_html), result.body_html[:200]
    assert result.body_html.index("hero.jpg") < result.body_html.index("Aufsicht reagiert")


def test_preclean_xpath_stays_linear_on_big_pages():
    from app.extract import _cleaned_tree
    page = _page("<div>" + '<div class="a b c"><span class="x">t</span><a href="/y">l</a></div>' * 60_000 + "</div>")
    t0 = time.monotonic()
    tree, changed = _cleaned_tree(page)
    assert tree is not None and not changed
    assert time.monotonic() - t0 < 10


def test_meta_line_regex_has_no_catastrophic_backtracking():
    from app.extract import _META_LINE_RE
    for n in (21, 30, 36):
        t0 = time.monotonic()
        assert not _META_LINE_RE.match("Stand: " + "12.12.12" * n + "x")
        assert time.monotonic() - t0 < 0.5
    t0 = time.monotonic()
    html, _ = sanitize("<p>Stand: " + "12.12.12" * 30 + "x</p><p>Text.</p>", BASE)
    assert time.monotonic() - t0 < 1 and "Text." in html
    for line in ("Stand: 28.09.2026 18:07 Uhr", "Lesezeit: 3 Minuten", "Lesezeit: 3 Min.", "Stand: 28. September 2026"):
        assert _META_LINE_RE.match(line), line


def test_tickaroo_text_with_br_is_split_into_paragraphs_without_duplicates():
    rich = ('<div class="tik4-rich-text tik4-rich-text--de"><h2>Putin stellt Metro unter Verwaltung</h2><div>Der '
            "russische Präsident hat die Vermögenswerte unter Verwaltung gestellt.<br>Das Dekret nannte keine Gründe "
            "für den Schritt, Metro hat noch 91 Märkte.<br><br>In diesem Monat hatte Putin bereits Nestlé und Auchan "
            "unter externe Verwaltung gestellt.</div></div>")
    event = f'<div class="tik4-event"><div class="tik4-content-block">{rich}</div></div>'
    import lxml.html
    from app.extract import _precleaned
    out = lxml.html.tostring(_precleaned(_page(event)), encoding="unicode")
    assert out.count("<p>") == 3 and "<br>" not in out
    result = extract(_page(f'<main><div class="tik4-live">{event * 4}</div></main>'), BASE)
    for sentence in ("Das Dekret nannte keine Gründe", "In diesem Monat hatte Putin"):
        assert result.body_text.count(sentence) <= 4, sentence   # je Eintrag höchstens einmal


def test_title_echo_behind_the_moved_image_is_dropped():
    from app.extract import Extraction, drop_title_echo
    title = "Putin stellt Metro unter Zwangsverwaltung"
    body = f'{IMG}<figcaption>Metro-Schild</figcaption><p>{title}</p><p>Text folgt hier.</p>'
    res = drop_title_echo(_extraction(body), title)
    assert res.body_html == f"{IMG}<figcaption>Metro-Schild</figcaption><p>Text folgt hier.</p>"
    # Seitenüberschrift mit Dachzeile passt nur zum Titel der Seite (og:title), nicht zum Feed-Titel
    body = ("<h2>Partei von Marine Le Pen Antisemitismusvorwürfe bringen Bardella in Bedrängnis</h2>"
            "<p>Text folgt hier.</p>")
    res = Extraction("full", body, html_to_text(body), 10, False, None, None,
                     "Antisemitismusvorwürfe bringen Bardella in Bedrängnis")
    assert drop_title_echo(res, "Frankreich: Antisemitismusaffäre um Jordan Bardella").body_html == \
        "<p>Text folgt hier.</p>"


def test_extract_drops_page_title_paragraph_before_moving_the_image():
    title = "Nvidia stellt Sicherheitsnetz für autonome KI-Agenten vor"
    page = _page(f"<article><p>{title}</p><figure><img src=\"https://a.example/hero.jpg\" alt=\"Logo\"></figure>"
                 f"{ARTICLE_PARAS}</article>", f'<meta property="og:title" content="{title}">')
    result = extract(page, BASE)
    assert title not in result.body_text and result.page_title == title


@pytest.mark.parametrize("ld,expected", [
    ('[{"@type":"WebPage","isAccessibleForFree":true},{"@type":"NewsArticle","isAccessibleForFree":false}]', True),
    ('{"@graph":[{"@type":"WebPage","isAccessibleForFree":"True"},'
     '{"@type":"NewsArticle","url":"https://news.example/artikel/1","isAccessibleForFree":"False"}]}', True),
    # eigener Artikel frei, empfohlene Artikel (andere URL) mit Paywall: frei (heise)
    ('[{"@type":"NewsArticle","url":"https://news.example/artikel/1","isAccessibleForFree":true},'
     '{"@type":"NewsArticle","url":"https://news.example/plus","isAccessibleForFree":false}]', False),
    # CSM: Wert als URL ist keine Angabe, das erste Vorkommen im Text auch nicht
    ('{"@type":"NewsArticle","isAccessibleForFree":"http:\\\\/\\\\/schema.org\\\\/False"}', False),
    # eigenes Objekt ohne Angabe: unentschieden, der Seitentext entscheidet (hier: die Paywall-Angabe des
    # verwandten Artikels ist das erste Vorkommen)
    ('[{"@type":"WebPage","url":"https://news.example/artikel/1"},'
     '{"@type":"NewsArticle","url":"https://news.example/plus","isAccessibleForFree":false}]', True),
    # eigener Artikel ohne Angabe, dazu eine WebPage ohne URL mit Paywall-Angabe: Paywall (Seitentext)
    ('[{"@type":"NewsArticle","url":"https://news.example/artikel/1","headline":"H"},'
     '{"@type":"WebPage","isAccessibleForFree":false}]', True),
    # BreadcrumbList-Eintrag mit der Seite als @id ist kein eigenes Objekt; der Artikel (andere Überschrift)
    # entscheidet
    ('[{"@type":"BreadcrumbList","itemListElement":[{"@type":"ListItem","@id":"https://news.example/artikel/1",'
     '"isAccessibleForFree":true}]},{"@type":"NewsArticle","headline":"Ganz anders","isAccessibleForFree":false}]',
     True),
    # eigener Artikel frei, aber ein Teil (hasPart) mit Paywall: Paywall
    ('{"@type":"NewsArticle","url":"https://news.example/artikel/1","isAccessibleForFree":true,'
     '"hasPart":[{"@type":"WebPageElement","isAccessibleForFree":true},'
     '{"@type":"WebPageElement","isAccessibleForFree":false}]}', True),
    # eigener Artikel ausdrücklich frei überstimmt eine Paywall-Angabe davor im Seitentext
    ('[{"@type":"WebPage","isAccessibleForFree":false},'
     '{"@type":"NewsArticle","url":"https://news.example/artikel/1","isAccessibleForFree":true}]', False),
    # eigene WebPage mit Angabe, verwandter Artikel frei: die eigene entscheidet
    ('[{"@type":"WebPage","url":"https://news.example/artikel/1","isAccessibleForFree":false},'
     '{"@type":"NewsArticle","url":"https://news.example/frei","isAccessibleForFree":true}]', True),
    # Bezahlteil als hasPart des eigenen Artikels (Google-Vorschlag)
    ('{"@type":"NewsArticle","url":"https://news.example/artikel/1","hasPart":{"@type":"WebPageElement",'
     '"isAccessibleForFree":false,"cssSelector":".paywall"}}', True),
    # Zeilenumbruch in einer Zeichenkette (ungültiges JSON, aber verbreitet): trotzdem gelesen
    ('{"@type":"NewsArticle","headline":"Zeile\neins","url":"https://news.example/artikel/1",'
     '"isAccessibleForFree":"False"}' + "x" * 0, True),
])
def test_paywall_decided_by_the_page_article_object(ld, expected):
    page = _page(f"<article>{ARTICLE_PARAS}</article>", f'<script type="application/ld+json">{ld}</script>')
    assert extract(page, BASE).paywalled is expected


def test_json_ld_relevance_check_stays_fast_with_many_blocks():
    blocks = "".join(f'<script type="application/ld+json">{{"@type":"NewsArticle","url":"https://x.example/{n}",'
                     f'"articleBody":"Ein anderer Artikel Nummer {n} mit genug Text für einen ganzen Satz hier."}}'
                     "</script>" for n in range(5_000))
    t0 = time.monotonic()
    result = extract(_page(f"<article>{ARTICLE_PARAS * 20}</article>", blocks), BASE)
    assert time.monotonic() - t0 < 10 and "Landenavigation" in result.body_text


@pytest.mark.parametrize("credit,dropped", [
    ("Sven Hoppe/dpa", True), ("Zwei Männer, ein Wagen. Sven Hoppe/dpa", True),
    ("Beautiful Sports / KJPeters / IMAGO", True), ("Oliver Berg / dpa", True),
    ("Das berichteten dpa/AFP", False), ("Die Polizei ermittelt weiter gegen Unbekannt.", False),
])
def test_agency_credits_of_lost_images_are_dropped(credit, dropped):
    far = "<p>" + "Langer Absatz. " * 30 + "</p>"
    html, _ = sanitize(f"{far}<p>{credit}</p><p>Weiter.</p>", BASE)
    assert (credit not in html) is dropped
    html, _ = sanitize(f'<img src="https://a.example/b.jpg"><p>{credit}</p><p>Weiter.</p>', BASE)
    assert credit in html   # mit Bild davor: Bildnachweis bleibt


def test_heise_editor_initials_at_the_end_are_dropped():
    page = _page(f"<article>{ARTICLE_PARAS}<p>(<a href=\"mailto:x@heise.de\">fpi</a>)</p></article>")
    assert "(fpi)" not in extract(page, "https://www.heise.de/news/x.html").body_text
    assert "(fpi)" in extract(page, BASE).body_text   # nur bei heise
    from app.extract import _drop_trailing_initials
    assert _drop_trailing_initials("<p>Text.</p><p>(mki, tobe)</p>") == "<p>Text.</p>"
    assert _drop_trailing_initials("<p>Text.</p><p>(dpa)</p><p>Schluss.</p>") == "<p>Text.</p><p>(dpa)</p><p>Schluss.</p>"


def test_run_together_split_respects_newlines_abbreviations_and_domains():
    from app.extract import _clean_ld_body
    assert _clean_ld_body("A court.The collegium met.") == "A court.\nThe collegium met."
    for keep in ("Laut Dr.Müller geht es voran.", "Aufstieg in die 3.Liga steht bevor.",
                 "Mehr auf www.Example.com lesen.", "Die U.S.Army war nicht beteiligt.", "Erst Absatz.\nDann court.The"):
        assert _clean_ld_body(keep) == keep, keep
    t0 = time.monotonic()
    _clean_ld_body("a" * 200_000 + " " + "Wort.Wort " * 50_000)
    _clean_ld_body("ab.Cd" * 32_000)            # ohne Leerzeichen: früher 5,5 s
    _clean_ld_body("x" + "Ab.Cd" * 400_000)     # 2 MB
    assert time.monotonic() - t0 < 5


@pytest.mark.parametrize("junk", [
    '<button class="sr-only">Zum Inhalt</button>',
    '<button class="show-comments">Kommentare</button>',
    '<div class="c-article-page__preferred-sources"><a href="https://www.google.com/preferences/source?q=welt.de">'
    "Google</a></div>",
])
def test_element_matched_twice_is_dropped_once_and_the_rest_still_works(junk, caplog):
    from app.extract import _cleaned_tree
    import lxml.html
    page = _page(f"<article>{junk}{HEISE_FIGURE}{ARTICLE_PARAS}</article>")
    tree, changed = _cleaned_tree(page)
    assert tree is not None and changed
    out = lxml.html.tostring(tree, encoding="unicode")
    assert "Zum Inhalt" not in out and "Kommentare" not in out and "preferences/source" not in out
    assert "<a-img" not in out and "heise.cloudimg.io" in out   # Umbauten laufen weiter
    assert "gescheitert" not in caplog.text


@pytest.mark.parametrize("body", [
    # Alt-Text „Donald Trump“ macht einen Satz, der so beginnt, nicht zur Unterschrift
    '<img src="https://a.example/b.jpg" alt="Donald Trump"><p>Donald Trump hat erneut mit Zöllen gedroht.</p>'
    "<p>Text.</p>",
    # … und zieht keinen späteren Absatz nach vorn
    '<img src="https://a.example/b.jpg" alt="Olaf Scholz"><p>Erster Absatz mit einem ganzen Satz.</p>'
    "<p>Olaf Scholz sagte am Montag in Berlin, er sehe keinen Grund für Neuwahlen.</p>",
    # Zitat, Auslassung, Klammer, Ortsmarke, Gedicht: keine Bildunterschrift
    f"{IMG}<p>„Wir schaffen das“</p><p>Text.</p>",
    f"{IMG}<p>Und dann kam alles anders …</p><p>Text.</p>",
    f"{IMG}<p>Die Lage in der Stadt (Archiv)</p><p>Text.</p>",
    f"{IMG}<p>Berlin (dpa) – Die Bundesregierung</p><p>Text.</p>",
    f"{IMG}<p>Der Mond ist aufgegangen</p><p>Die goldnen Sternlein prangen</p><p>Am Himmel hell und klar</p>",
])
def test_arrange_top_does_not_take_text_as_caption(body):
    from app.extract import _arrange_top
    assert _arrange_top(body) == body


def test_caption_ends_after_the_credit():
    from app.extract import _arrange_top
    body = f"{IMG}<p>Merz im Bundestag</p><p>Foto: dpa</p><p>Eine kurze Einleitung ohne Punkt</p><p>Text.</p>"
    assert _arrange_top(body) == (f"<figure>{IMG}<figcaption>Merz im Bundestag<br>Foto: dpa</figcaption></figure>"
                                  "<p>Eine kurze Einleitung ohne Punkt</p><p>Text.</p>")


def test_intro_stays_before_the_body_when_the_image_follows_it():
    """GEA: kurzer Vorspann, Bild, langer Text, Unterschrift: der Vorspann bleibt vor dem Text."""
    from app.extract import _arrange_top
    alt = "Zieldurchfahrt in Trochtelfingen: Der Münsinger Uwe Hardter gewinnt die Langdist"
    body = (f'<p>Kurzer Vorspann.</p><img src="https://a.example/b.jpg" alt="{alt}"><p>{"Langer Text. " * 30}</p>'
            "<p>Zieldurchfahrt in Trochtelfingen: Der Münsinger Uwe Hardter gewinnt die Langdistanz.</p>")
    out = _arrange_top(body)
    assert out.startswith('<img src="https://a.example/b.jpg"') or out.startswith("<figure>")
    assert out.index("Kurzer Vorspann.") < out.index("Langer Text.")


def test_split_at_br_is_linear_and_keeps_the_tail():
    from app.extract import _split_at_br
    import lxml.html
    root = lxml.html.fromstring("<div><section>" + "<div>a<br>b</div>" * 20_000 + "</section></div>")
    divs = root.findall(".//section/div")
    t0 = time.monotonic()
    for div in divs:
        _split_at_br(div)
    assert time.monotonic() - t0 < 5 and len(root.findall(".//p")) == 40_000
    root = lxml.html.fromstring("<div><p>vorher</p><div><br><br></div>danach</div>")
    _split_at_br(root.find("div"))
    assert lxml.html.tostring(root, encoding="unicode") == "<div><p>vorher</p>danach</div>"
    root = lxml.html.fromstring("<div><div><br></div>danach</div>")
    _split_at_br(root.find("div"))
    assert lxml.html.tostring(root, encoding="unicode") == "<div>danach</div>"


def test_json_ld_with_raw_newline_is_parsed():
    """Ungültiges JSON mit Zeilenumbruch in einer Zeichenkette wird trotzdem gelesen (strict=False): Hier
    entscheidet nur das JSON-LD, das erste Vorkommen im Seitentext wäre „true“."""
    ld = ('<script type="application/ld+json">{"@type":"NewsArticle","headline":"Zeile\neins",'
          '"url":"https://news.example/artikel/1","isAccessibleForFree":false}</script>')
    page = _page(f"<article>{ARTICLE_PARAS}</article>", '<meta name="x" content=\'"isAccessibleForFree":true\'>' + ld)
    assert extract(page, BASE).paywalled is True


def test_screen_reader_separator_stays_as_text():
    """FAZ: <span class="sr-only">: </span> zwischen Dachzeile und Titel bleibt als „: “ stehen."""
    import lxml.html
    from app.extract import _precleaned
    page = _page('<article><h2><span>Liveblog Ukrainekrieg <span class="sr-only">: </span></span><span>Selenskyj '
                 'spricht</span></h2><span class="sr-only">Zum Inhalt springen</span></article>')
    out = lxml.html.fromstring(lxml.html.tostring(_precleaned(page))).text_content()
    assert "Liveblog Ukrainekrieg : Selenskyj" in out and "Zum Inhalt" not in out


def test_trailing_credit_in_parentheses_makes_a_caption():
    from app.extract import _arrange_top
    body = (f"{IMG}<p>Soldiers train near Kyiv on Sept. 27, 2026. (Nikoletta Stoyanova/Getty Images)</p>"
            "<p>Text.</p>")
    assert _arrange_top(body) == (f"<figure>{IMG}<figcaption>Soldiers train near Kyiv on Sept. 27, 2026. "
                                  "(Nikoletta Stoyanova/Getty Images)</figcaption></figure><p>Text.</p>")


@pytest.mark.parametrize("line", [
    "Bild: Die Bundesregierung muss jetzt endlich handeln, statt weiter zu zögern.",
    "Bild: Merz hat die Wahl gewonnen, und zwar deutlich, meint das Boulevardblatt heute in seinem Kommentar",
])
def test_press_review_line_quoting_bild_is_kept(line):
    far = "<p>" + "Langer Absatz. " * 30 + "</p>"
    html, _ = sanitize(f"{far}<p>{line}</p><p>Weiter.</p>", BASE)
    assert line in html


@pytest.mark.parametrize("para", [
    "Klicken Sie hier, um mehr über Google und seine Suchmaschine zu erfahren.",
    "Please contact us at the front desk if you have questions.",
])
def test_promo_phrases_need_their_context(para):
    html, _ = sanitize(f"<p>{para}</p>", BASE)
    assert para in html


def test_faz_header_caption_is_kept_and_teaser_is_not_the_caption():
    head = _og("Seoul: Kiew hat gegen vertrauliche Vereinbarung verstoßen +++ alle Neuigkeiten im Liveblog")
    alt = ("Werbung für den Armeedienst in Tambov: Umgerechnet 56.000 Euro pro Jahr bietet die Russische Armee "
           "für Drohnenoperatoren.")
    page = _page(f'<main><article><header><div data-external-selector="header-teaser-image"><picture><img alt="{alt}" '
                 'src="https://media0.faz.net/image/w1240/x/werbung.webp"></picture></div><div class="meta2 block">'
                 f'<span class="meta2">{alt}</span><small class="meta2-italic">AFP</small></div><p '
                 'data-external-selector="header-teaser">Seoul: Kiew hat gegen vertrauliche Vereinbarung verstoßen '
                 f"+++ alle Neuigkeiten im Liveblog</p></header>{ARTICLE_PARAS}</article></main>", head)
    result = extract(page, "https://www.faz.net/aktuell/politik/liveblog.html")
    caption = re.search(r"<figcaption>(.*?)</figcaption>", result.body_html, re.S)
    assert caption and "Tambov" in caption.group(1) and "AFP" in caption.group(1) and "+++" not in caption.group(1)
    from app.extract import _arrange_top
    teaser = f"{IMG}<p>Kiew meldet Angriffe +++ Kreml reagiert +++ alle Neuigkeiten im Liveblog</p><p>Text.</p>"
    assert _arrange_top(teaser) == teaser


def test_taz_placeholder_image_is_replaced_by_the_noscript_image():
    figure = ('<figure><a href="https://taz.de/picture/1/1200/b.webp"><picture><noscript><img '
              'src=" https://taz.de/picture/8766912/835/42191297.webp" alt="Wüst und Erdoğan"></noscript>'
              '<img src="https://taz.de/picture/8766912/14/42191297.webp" alt="Wüst und Erdoğan"></picture></a>'
              '<figcaption>Alles glänzt: Wüst bei Erdoğan <p class="credit-name">Foto: Marcel Kusch/dpa</p>'
              "</figcaption></figure>")
    import lxml.html
    from app.extract import _precleaned
    out = lxml.html.tostring(_precleaned(_page(f"<article>{figure}{ARTICLE_PARAS}</article>")), encoding="unicode")
    assert "/14/" not in out.split("</noscript>", 1)[1] and "/835/42191297.webp" in out.split("</noscript>", 1)[1]
    no_noscript = figure.replace("<noscript>", "<!--").replace("</noscript>", "-->")
    out = lxml.html.tostring(_precleaned(_page(f"<article>{no_noscript}{ARTICLE_PARAS}</article>")), encoding="unicode")
    assert '/14/42191297.webp"' not in out.replace("<!--", "").split("-->")[-1]


def test_only_a_credit_arrived_takes_the_full_page_caption():
    """taz: trafilatura verwarf den Text der <figcaption>, nur der Nachweis kam an."""
    from app.extract import _arrange_top, _squash
    full = "Glaubt, dass Regeln nicht für ihn gelten: Jens Spahn im Münsterland Foto: Lars Berg/dpa"
    body = f"{IMG}<p>Foto: Lars Berg/dpa</p><p>Text.</p>"
    out = _arrange_top(body, {_squash(full): full})
    assert out == (f"<figure>{IMG}<figcaption>Glaubt, dass Regeln nicht für ihn gelten: Jens Spahn im Münsterland"
                   "<br>Foto: Lars Berg/dpa</figcaption></figure><p>Text.</p>")


def test_heading_equal_to_the_visible_page_headline_is_dropped():
    """SPIEGEL: og:title ist ein Suchmaschinen-Titel; die gezeigte Überschrift (Dachzeile + Titel) steht als
    <h2> im Kopf und kam als erste Überschrift in den Text."""
    headline = ("<h2><span>Sendungsaus nach sechs Jahren</span> <span>Aus für »ZDF Magazin Royale« meine eigene "
                "Entscheidung, sagt Böhmermann</span></h2>")
    page = _page(f"<main><article><header>{headline}</header><div>{ARTICLE_PARAS}</div></article></main>",
                 '<meta property="og:title" content="Jan Böhmermann: Ende von »ZDF Magazin Royale« sei seine eigene '
                 'Entscheidung gewesen">')
    result = extract(page, "https://www.spiegel.de/kultur/tv/x")
    assert "Sendungsaus" not in result.body_text and "Landenavigation" in result.body_text


# ---- Nacharbeit: Werbung, Boilerplate, Bildunterschriften, Laufzeit ------------------------------

GUARDIAN_NEWSLETTER = (
    # gekürzt aus einem echten Guardian-Artikel (30.09.2026)
    '<figure data-spacefinder-role="inline" data-spacefinder-type="model.dotcomrendering.pageElements.'
    'NewsletterSignupBlockElement" class="dcr-d9bay7"><gu-island name="EmailSignUpWrapper" priority="feature" '
    'deferUntil="visible" props="{}"><a data-ignore="global-link-styling" href="#EmailSignup-skip-link-18" '
    'class="dcr-76akua">skip past newsletter promotion</a><div class="dcr-1tr4stg"><div class="dcr-12qa5gp">'
    '<hr class="dcr-1cjdlyj"/><aside aria-label="newsletter promotion" class="dcr-12f8e11"><div class="dcr-ey8bdt">'
    '<span>Headlines Europe</span><p>A digest of the morning\'s main headlines from the Europe edition emailed '
    'direct to you every week day</p></div><form id="newsletter-signup-headlines-europe"><label>Enter your email'
    '</label><input type="email" name="email"/><button type="submit">Sign up</button></form></aside></div></div>'
    '<p id="EmailSignup-skip-link-18" tabindex="0" aria-label="after newsletter promotion" role="note" '
    'class="dcr-76akua">after newsletter promotion</p></gu-island></figure>')


def test_guardian_newsletter_promotion_is_dropped():
    paras = ARTICLE_PARAS.split("</p>", 1)
    page = _page(f"<main><article><div>{paras[0]}</p>{GUARDIAN_NEWSLETTER}{paras[1]}</div></article></main>")
    result = extract(page, "https://www.theguardian.com/world/2026/sep/30/x")
    assert "Landenavigation" in result.body_text and "Aufsichtsbehörde FAA" in result.body_text
    assert "newsletter" not in result.body_text.lower() and "Headlines Europe" not in result.body_text
    # zweite Sicherung: die Sprungmarken als eigene Absätze
    html, _ = sanitize('<p>Text.</p><p><a href="https://www.theguardian.com#EmailSignup-skip-link-9">skip past '
                       "newsletter promotion</a></p><p>after newsletter promotion</p><p>Weiter.</p>", BASE)
    assert html == "<p>Text.</p><p>Weiter.</p>"


def _bbc_image(src: str, alt: str, width: str, height: str) -> str:
    # Aufbau wie auf bbc.co.uk (gekürzt: ohne <source> und srcSet)
    return ('<div data-block="image" class="ssrcss-l7emlr-ComponentWrapper e1aon0op1"><div class="ssrcss-126h8e-'
            'Spacer e1aon0op0"><div data-testid="image" class="ssrcss-1uy5kw5-FullWidthWrapper ep2nwvo1"><figure '
            'class="ssrcss-hc6arm-StyledFigure e34k3c22"><div class="ssrcss-ab5fd8-StyledFigureContainer e34k3c21">'
            f'<span class="ssrcss-xm7t37-Placeholder etlorgc0"><picture><img alt="{alt}" loading="lazy" src="{src}" '
            f'width="{width}" height="{height}" class="ssrcss-egie1y-Image edrdn950"/></picture></span></div>'
            "</figure></div></div></div>")


def _bbc_text(inner: str) -> str:
    return ('<div data-block="text" class="ssrcss-vxwoax-ComponentWrapper e1aon0op1"><div data-testid="rich-text">'
            f'<p class="ssrcss-1q0x1qg-Paragraph e1jhz7w10">{inner}</p></div></div>')


BBC = "https://ichef.bbci.co.uk/ace/standard/1920/cpsprodpb/"


@pytest.mark.parametrize("banner", [
    _bbc_image(BBC + "1288/live/05d56230-bc97-11f1-bd21-bdf910f2cec6.png", "A thin, grey banner promoting the News "
               "Daily newsletter. The banner reads: “The latest news in your inbox first thing.”", "1920", "316"),
    _bbc_image(BBC + "936b/live/838cd6f0-bb4c-11f1-bd21-bdf910f2cec6.png", "Your Voice banner", "", ""),
    _bbc_image(BBC + "6fad/live/133a6af0-dc98-11ef-bc01-8f2c83dad217.png", "BBC News Africa", "1600", "263"),
])
def test_bbc_promo_banners_and_newsletter_line_are_dropped(banner):
    photo = _bbc_image(BBC + "7e10/live/2ac04aa0-bc1b-11f1-b84f-b5367b64d530.jpg", "Aaron Richards stands outside "
                       "a shop.", "1536", "864")
    chart = _bbc_image(BBC + "e974/live/b5992fd0-bc10-11f1-bd21-bdf910f2cec6.png", "A bar chart showing the energy "
                       "price cap", "1600", "1600")
    paras = "".join(_bbc_text(p[3:]) for p in ARTICLE_PARAS.split("</p>") if p)
    newsletter = _bbc_text('Get our flagship newsletter with all the headlines you need to start the day. <a href='
                           '"/newsletters/zhp28xs" class="ssrcss-1e0jzsh-InlineLink e1kn3p7n0">Sign up here.</a>')
    page = _page(f"<main><article>{photo}{paras}{chart}{banner}{newsletter}</article></main>")
    result = extract(page, "https://www.bbc.co.uk/news/articles/cqn8m342rgk9o")
    assert "2ac04aa0" in result.body_html and "b5992fd0" in result.body_html   # Foto und Grafik bleiben
    assert "Landenavigation" in result.body_text and "Aufsichtsbehörde FAA" in result.body_text
    assert "banner" not in result.body_html.lower() and "133a6af0" not in result.body_html
    assert "flagship newsletter" not in result.body_text and "Sign up" not in result.body_text


def test_bbc_banner_check_stays_linear_with_nested_blocks():
    nested = '<div data-block="image">' * 200 + f'<img src="{BBC}x/a.png" alt="Your Voice banner">' + "</div>" * 200
    imgs = "".join(f'<div data-block="image"><img src="{BBC}{n}/a.png" width="1920" height="316"></div>'
                   for n in range(3000))
    t0 = time.monotonic()
    tree, _ = extract_mod._cleaned_tree(_page(f"<article>{nested}{imgs}{ARTICLE_PARAS}</article>"))
    assert time.monotonic() - t0 < 5 and ".png" not in lxml_html.tostring(tree, encoding="unicode")


@pytest.mark.parametrize("line", [
    "© Axel Springer Deutschland GmbH. Alle Rechte vorbehalten.",
    "© Axel Springer SE. Alle Rechte vorbehalten",
])
def test_welt_rights_line_is_dropped(line):
    html, _ = sanitize(f"<p>Text zum Video.</p><p>{line}</p>", BASE)
    assert html == "<p>Text zum Video.</p>"


def test_welt_rights_line_inside_text_stays():
    para = "Der Verlag © Axel Springer Deutschland GmbH. Alle Rechte vorbehalten, sagt der Sprecher, gelte weiter."
    html, _ = sanitize(f"<p>{para}</p>", BASE)
    assert para in html


ZEIT_SUMMARY = (
    # gekürzt aus dem ZEIT-Liveblog zum Krieg gegen die Ukraine (30.09.2026)
    '<div class="liveblog-subheader"><div class="article__item"><div class="summary">Um die Verteidigung im '
    "kommenden Jahr zu finanzieren, fehlen der Ukraine nach Angaben aus Kyjiw knapp 24 Milliarden Euro. Mehrere "
    "Tote nach Raketenangriffen. Das Liveblog</div></div></div>")
ZEIT_OVERVIEW = (
    '<section class="z-accordion liveblog-preceding-content article__item js-accordion js-accordion__wrapper '
    'is-active restore-state"><h2 class="z-accordion__title liveblog-preceding-content__title"><button class='
    '"z-accordion__button" aria-expanded="true">Überblick</button></h2><div role="region" class="z-accordion__'
    'content liveblog-preceding-content__content js-accordion__panel"><ul class="list article__item"><li> Alle '
    'Artikel zum Thema finden Sie auf unserer <a href="https://www.zeit.de/thema/krieg-in-ukraine" target="_blank">'
    "Themenseite zum Krieg gegen die Ukraine</a>. Eine Übersichtskarte mit den aktuellen Entwicklungen "
    'aktualisieren wir täglich <a href="https://www.zeit.de/politik/ausland/karte">hier</a>.<br></li><li>Russische '
    "oder ukrainische Angaben zu Details einzelner Angriffe und zur Zahl Verletzter und Getöteter lassen sich "
    "derzeit nicht unabhängig verifizieren.</li><li>Neben eigenen Recherchen verwenden wir Material der "
    "Nachrichtenagenturen AFP, AP, dpa, epd, KNA und Reuters.</li><li>Wie wir bei der ZEIT Nachrichten zum "
    'Ukrainekrieg machen, finden Sie <a href="https://www.zeit.de/administratives/x">hier</a>.<br></li>{extra}'
    "</ul></div></section>")


def test_zeit_liveblog_notes_and_label_are_dropped():
    desc = ("Um die Verteidigung im kommenden Jahr zu finanzieren, fehlen der Ukraine nach Angaben aus Kyjiw knapp "
            "24 Milliarden Euro. Mehrere Tote nach Raketenangriffen. Das Liveblog")
    drones = ("Russland hat nach eigenen Angaben in der Nacht 120 ukrainische Drohnen abgeschossen. Die Angaben "
              "lassen sich nicht unabhängig überprüfen.")
    extra = (f"<li>Selenskyj trifft am Mittwoch Macron in Paris, es geht um weitere Luftabwehrsysteme.</li>"
             f"<li>{drones}</li><li>Israelische Angaben zu Opferzahlen lassen sich nicht unabhängig überprüfen, "
             "sagt die UN.</li>")
    page = _page(f'<main><article><header>{ZEIT_SUMMARY}</header><div class="article-body"><div class="article-'
                 f'page">{ZEIT_OVERVIEW.format(extra=extra)}{ARTICLE_PARAS}</div></div></article></main>', _og(desc))
    result = extract(page, "https://www.zeit.de/politik/ausland/ukraine-krieg-news-liveblog")
    text = result.body_text
    assert "Landenavigation" in text and "Selenskyj trifft am Mittwoch Macron" in text   # andere Punkte bleiben
    assert drones in text and "sagt die UN." in text   # Agenturformel in einer Meldung ist kein Hinweis
    for note in ("Alle Artikel zum Thema", "Neben eigenen Recherchen", "Wie wir bei der ZEIT",
                 "Details einzelner Angriffe", "Das Liveblog"):
        assert note not in text, note
    assert "Mehrere Tote nach Raketenangriffen." in text   # der Vorspann bleibt, nur ohne Etikett


@pytest.mark.parametrize("body", [
    "<p>Hier berichten wir fortlaufend. Das Liveblog wird laufend aktualisiert.</p><p>Text.</p>",
    "<p>Erster Absatz.</p><p>Mehr dazu. Das Liveblog</p>",          # nur im ersten Absatz
    "<p>Alles zum Thema im Das Liveblog</p>",                        # ohne Satzende davor
])
def test_liveblog_label_rule_is_narrow(body):
    assert extract_mod._drop_liveblog_label(body) == body


def test_faz_header_caption_joins_caption_and_credit_with_br():
    alt = "Hendrik Wüst mit dem türkischen Präsidenten Recep Tayyip Erdogan am Montag in Ankara"
    page = _page('<main><article><header><div data-external-selector="header-teaser-image"><picture><img alt='
                 f'"{alt}" src="https://media0.faz.net/image/w1240/x/wuest.webp"></picture></div><div class="meta2 '
                 f'block"><span class="meta2 pr-[10px]">{alt}</span><small class="meta2-italic">dpa</small></div>'
                 f"</header>{ARTICLE_PARAS}</article></main>")
    result = extract(page, "https://www.faz.net/aktuell/politik/inland/x-201269563.html")
    assert f"<figcaption>{alt}<br>dpa</figcaption>" in result.body_html
    tree, _ = extract_mod._cleaned_tree(page)
    assert extract_mod._page_captions(tree) == {extract_mod._squash(alt + " dpa"): f"{alt}\ndpa"}


def test_many_pinned_posts_do_not_make_the_live_blog_quadratic():
    """Nur der erste angeheftete Eintrag zählt; die Eintragsliste wird einmal gelesen (vorher 500 × 2,9 MB)."""
    pinned = "".join('<div data-component="pinned-post"><article><p>Angeheftet {0} mit etwas Text, der lang genug '
                     "ist.</p></article></div>".format(n) for n in range(500))
    words = "Wort " * 580
    blocks = "".join(f'<article id="block-{n}"><header><h2>Eintrag {n}</h2></header><p>{words}</p></article>'
                     for n in range(1000))
    page = _page(f'<main><div id="liveblog-body">{pinned}{blocks}</div></main>')
    assert len(page) > 2_900_000
    t0 = time.monotonic()
    tree, _ = extract_mod._cleaned_tree(page)
    assert time.monotonic() - t0 < 5
    out = lxml_html.tostring(tree, encoding="unicode")
    assert "Angeheftet 0 mit" in out and "Angeheftet 1 mit" not in out and "Eintrag 999" in out


def test_metadata_skips_the_extensive_date_search():
    """htmldate durchsuchte jeden kurzen Text mit einer Zahl nach einem Datum (12 000 Absätze: 29 s)."""
    paras = "".join(f"<p>Tag {n} am {n % 28 + 1}.{n % 12 + 1}. um {n % 24}:{n % 60:02d}</p>" for n in range(12_000))
    page = _page(f"<article>{paras}</article>", '<meta property="og:title" content="Titel der Seite">'
                 '<meta name="author" content="Anna Muster">'
                 '<meta property="og:image" content="https://news.example/bild.jpg">')
    t0 = time.monotonic()
    result = extract(page, BASE)
    assert time.monotonic() - t0 < 10 and result.body_text
    assert (result.page_title, result.author, result.image_url) == (
        "Titel der Seite", "Anna Muster", "https://news.example/bild.jpg")


def test_taz_placeholder_rule_only_applies_to_taz_images():
    figure = ('<figure><picture><noscript><img src="https://img.example/picture/1/835/b.webp"></noscript>'
              '<img src="https://img.example/picture/8766912/14/42191297.webp" alt="Echtes Bild"></picture>'
              "</figure>")
    tree, _ = extract_mod._cleaned_tree(_page(f"<article>{figure}{ARTICLE_PARAS}</article>"))
    out = lxml_html.tostring(tree, encoding="unicode")
    assert 'src="https://img.example/picture/8766912/14/42191297.webp"' in out.split("</noscript>", 1)[1]
    taz = figure.replace("img.example", "taz.de")
    tree, _ = extract_mod._cleaned_tree(_page(f"<article>{taz}{ARTICLE_PARAS}</article>"))
    out = lxml_html.tostring(tree, encoding="unicode")
    assert "/14/" not in out.split("</noscript>", 1)[1] and "/835/b.webp" in out.split("</noscript>", 1)[1]


def test_zeit_note_check_stays_linear_with_nested_list_items():
    """Vorher las jeder der 100 verschachtelten Listenpunkte alle 100 000 Tags darunter (ca. 6 s)."""
    nested = "<li><ul>" * 100 + "<li>" + "<i></i>" * 100_000 + "</li>" + "</ul></li>" * 100
    page = _page(f'<main><article><section class="liveblog-preceding-content"><ul>{nested}<li>Neben eigenen '
                 f"Recherchen verwenden wir Material der Nachrichtenagenturen.</li></ul></section>{ARTICLE_PARAS}"
                 "</article></main>")
    tree = extract_mod.load_html(page)
    assert len(tree.xpath("//li")) == 102
    t0 = time.monotonic()
    found = extract_mod._chrome_elements(tree)
    assert time.monotonic() - t0 < 2
    assert [el.text for el in found if el.tag == "li"] == ["Neben eigenen Recherchen verwenden wir Material der "
                                                           "Nachrichtenagenturen."]


@pytest.mark.parametrize("alt, width, height", [
    ("The Daily Mirror front page carries the banner headline: Deal done", "1600", "1600"),
    ("Protesters hold a banner reading: Stop the war", "", ""),
    ("A screenshot of the website", "inf", "316"),
    ("A screenshot of the website", "nan", "316"),
])
def test_bbc_png_article_images_with_banner_words_stay(alt, width, height):
    image = _bbc_image(BBC + "8935/live/2ce03770-bcaa-11f1-a3e9-bbda6f9b9e25.png", alt, width, height)
    tree, _ = extract_mod._cleaned_tree(_page(f"<main><article>{image}{ARTICLE_PARAS}</article></main>"))
    assert "2ce03770" in lxml_html.tostring(tree, encoding="unicode")


GUARDIAN_NEWSLETTER_BUTTON = (
    # aus theguardian.com/australia-news/2026/sep/30/hunter-valley-coal-project-…
    '<figure id="78e982bc-490f-4947-959a-90267ca0a097" data-spacefinder-role="inline" data-spacefinder-type="model.'
    'dotcomrendering.pageElements.LinkBlockElement" class="dcr-d9bay7"><a data-link-name="standard link button '
    'Primary" data-spacefinder-role="inline" data-ignore="global-link-styling" href="https://www.theguardian.com/'
    'email-newsletters?CMP=copyembed&amp;CMP=emailbutton" class="dcr-rh233e"><span class="dcr-1vp4tz8">Sign up for '
    "the Breaking News Australia email</span></a></figure>")


def test_guardian_newsletter_button_is_dropped_but_other_link_buttons_stay():
    other = GUARDIAN_NEWSLETTER_BUTTON.replace("/email-newsletters?CMP=copyembed&amp;CMP=emailbutton",
                                               "/world/series/the-long-read").replace(
        "Sign up for the Breaking News Australia email", "Read the whole series on the housing crisis")
    head, rest = ARTICLE_PARAS.split("</p>", 1)
    page = _page(f"<main><article><div>{head}</p>{GUARDIAN_NEWSLETTER_BUTTON}{rest}{other}</div></article></main>")
    tree, _ = extract_mod._cleaned_tree(page)
    out = lxml_html.tostring(tree, encoding="unicode")
    assert "Breaking News Australia" not in out and "the-long-read" in out
    # zweite Sicherung: der Knopf als eigener Absatz
    html, _ = sanitize("<p>Text.</p><p><a href=\"/email-newsletters\">Sign up for the Breaking News Australia email"
                       "</a></p><p>Sign up for a gym, the doctor said, and then write me an email about it every "
                       "week so I know how it goes.</p>", BASE)
    assert "Breaking News" not in html and "Sign up for a gym" in html


def test_guardian_comments_note_in_live_blogs_is_dropped():
    note = ('I’m afraid we are <a href="https://www.theguardian.com/politics/live/2026/jul/17/x">no longer able to '
            "have comments open below the line every day on this blog</a>. If you want to contact me, please email "
            "politics.live@theguardian.com. You can use this email to flag up corrections, comments for publication, "
            "questions or complaints.")
    html, _ = sanitize(f"<p>Good morning. Andy Burnham speaks in Liverpool today.</p><p>{note}</p>", BASE)
    assert html == "<p>Good morning. Andy Burnham speaks in Liverpool today.</p>"
    kept = "<p>The minister said: I’m afraid we are no longer able to have comments open below the line.</p>"
    assert sanitize(kept, BASE)[0] == kept


def test_zeit_liveblog_back_to_top_and_dangling_read_more_are_dropped():
    entry = ("<p><strong>30.09. 09:29</strong></p><h2>Ukrainische Soldaten befreien offenbar Dorf</h2><p>Der Ort "
             "Hrafske nördlich von Charkiw-Stadt ist offenbar wieder unter ukrainischer Kontrolle.</p>"
             "<p><i>Lesen Sie hier mehr zum Frontverlauf:</i></p>")
    body = (f"{entry}{entry.replace('09:29', '08:31')}"
            '<p><a href="https://www.zeit.de#lhe-trigger">Nach oben</a></p>')
    html, text = sanitize(body, BASE)
    assert "Lesen Sie hier mehr" not in text and "Nach oben" not in text
    assert text.count("Hrafske") == 2 and text.count("Ukrainische Soldaten") == 2
    for kept in ("<p>Lesen Sie hier mehr zum Frontverlauf:</p><p><a href=\"https://www.zeit.de/karte\">Karte</a></p>",
                 "<p>Lesen Sie hier mehr zum Frontverlauf: Die Lage bleibt angespannt.</p>"):
        assert "Lesen Sie hier mehr" in sanitize(kept, BASE)[1]
    page = _page('<main><article><div>' + ARTICLE_PARAS + '</div><a class="z-text-button liveblog__chapter-button--'
                 'back-to-top" href="#lhe-trigger">Nach oben</a></article></main>')
    tree, _ = extract_mod._cleaned_tree(page)
    assert "Nach oben" not in lxml_html.tostring(tree, encoding="unicode")
