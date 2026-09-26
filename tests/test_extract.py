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
