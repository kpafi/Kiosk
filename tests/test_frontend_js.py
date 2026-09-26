"""Kleine Unit-Tests für reine Frontend-Funktionen (format.js, route.js), ausgeführt mit node."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

JS = Path(__file__).resolve().parent.parent / "static" / "js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node nicht installiert")

HARNESS = """
const fmt = await import(process.argv[1] + '/format.js');
const { parseHash } = await import(process.argv[1] + '/route.js');
const now = new Date(2026, 8, 26, 12, 0);           // 26. Sep 2026, 12:00 Ortszeit
const at = (d, h, m = 0, s = 0) => new Date(2026, 8, d, h, m, s).toISOString();
const route = (h) => { const r = parseHash(h); return { name: r.name, id: r.id ?? null, key: r.key ?? null,
  hash: r.hash, frag: r.frag, p: r.params.get('p'), tab: r.params.get('tab') }; };
console.log(JSON.stringify({
  listTime: [fmt.listTime(at(26, 11, 48), now), fmt.listTime(at(26, 9), now), fmt.listTime(at(25, 10, 10), now),
             fmt.listTime(at(24, 12), now), fmt.listTime('kaputt', now)],
  dayLabel: [fmt.dayLabel(at(26, 1), now), fmt.dayLabel(at(25, 23), now), fmt.dayLabel(at(24, 12), now),
             fmt.dayLabel(new Date(2025, 11, 31, 12).toISOString(), now), fmt.dayLabel(null, now)],
  articleTime: [fmt.articleTime(at(26, 8, 5), now), fmt.articleTime(at(25, 7, 10), now)],
  ago: [fmt.ago(at(26, 11, 59, 30), now), fmt.ago(at(26, 11, 48), now), fmt.ago(at(26, 9), now), fmt.ago(at(24, 12), now)],
  cleanText: [fmt.cleanText(' None '), fmt.cleanText('null'), fmt.cleanText('Titel', ' titel '),
              fmt.cleanText('  Echter Text '), fmt.cleanText(undefined)],
  routes: ['', '#/', '#/?p=4', '#/a/12', '#/a/x', '#/s/taz', '#/gemerkt?tab=favoriten',
           '#/profile', '#/profile/neu', '#/profile#zeitungen', '#/quatsch'].map(route),
}));
"""


@pytest.fixture(scope="module")
def out():
    res = subprocess.run([NODE, "--input-type=module", "-e", HARNESS, str(JS.as_uri())],
                         capture_output=True, text=True, timeout=30, env={"TZ": "Europe/Berlin", "PATH": ""})
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_list_time(out):
    assert out["listTime"] == ["12 MIN", "3 STD", "GESTERN 10:10", "24. SEP", ""]


def test_day_label(out):
    assert out["dayLabel"] == ["HEUTE", "GESTERN", "24. SEP", "31. DEZ 2025", "OHNE DATUM"]


def test_article_time_and_ago(out):
    assert out["articleTime"] == ["Heute 08:05", "Gestern 07:10"]
    assert out["ago"] == ["gerade eben", "vor 12 Min", "vor 3 Std", "vor 2 Tagen"]


def test_clean_text(out):
    assert out["cleanText"] == ["", "", "", "Echter Text", ""]


def test_parse_hash(out):
    r = {h: v for h, v in zip(['', '#/', '#/?p=4', '#/a/12', '#/a/x', '#/s/taz', '#/gemerkt?tab=favoriten',
                               '#/profile', '#/profile/neu', '#/profile#zeitungen', '#/quatsch'], out["routes"])}
    assert r[""]["name"] == "list" and r[""]["key"] == "list:"
    assert r["#/?p=4"]["key"] == "list:4" and r["#/?p=4"]["p"] == "4"
    assert r["#/a/12"]["name"] == "article" and r["#/a/12"]["id"] == "12"
    assert r["#/a/x"]["name"] == "list"
    assert r["#/s/taz"]["key"] == "source:taz"
    assert r["#/gemerkt?tab=favoriten"]["key"] == "saved:favoriten"
    assert r["#/profile"]["name"] == "profiles" and r["#/profile#zeitungen"]["frag"] == "zeitungen"
    assert r["#/profile/neu"]["name"] == "editor" and r["#/profile/neu"]["id"] == "neu"
    assert r["#/quatsch"]["name"] == "list" and r["#/quatsch"]["hash"] == "#/"


def test_image_key_matches_python():
    """imageKey/isSmallImageUrl in format.js folgen denselben Regeln wie image_key/is_small_image_url in
    app/extract.py; sameImage (Browser, zusätzlich unscharf) wird gegen die Client-Spalte geprüft."""
    from app.extract import image_key, is_small_image_url
    from tests.test_extract import IMAGE_PAIRS

    urls = sorted({u for a, b, *_ in IMAGE_PAIRS for u in (a, b)} | {
        "//cdn.example/w100/bild_w200.png", "kaputt", "https://a.example/x?url=%E2%82%ZZ&w=1",
        "https://a.example/p?src=//cdn.example/b.jpg", "https://a.example/p?url=kein-link&w=5",
        "https://a.example/p?t=1&src=%20/v.mp4%0A&rect=(1,2)&w=5", "https://a.example/p?src=/a&src=/b&im=x",
        "HTTPS://A.Example/Bild/X_W9_r1.JPG?Width=5&ID=2", "https://a.example/foto_w960_r1.5_fpx4.jpg#x",
        "https://a.example/h1/w55/foto_r2_fpy4.jpg.webp?s=abc&auth=Q",
        "\ufeffhttps://a.example/bild.jpg", "\x1chttps://a.example/bild.jpg "})
    deep = "https://a.example/bild.jpg"
    for _ in range(7):   # url= wird höchstens 5 Ebenen tief ausgepackt
        deep = "https://p.example/proxy?url=" + quote(deep, safe="")
    urls.append(deep)
    script = """
const fmt = await import(process.argv[1] + '/format.js');
const input = JSON.parse(process.argv[2]);
console.log(JSON.stringify({ keys: input.urls.map((u) => fmt.imageKey(u, true)),
  loose: input.urls.map((u) => fmt.imageKey(u)),
  small: input.urls.map((u) => fmt.isSmallImageUrl(u)),
  same: input.pairs.map(([a, b]) => [fmt.sameImage(a, b), fmt.sameImage(b, a)]) }));
"""
    payload = json.dumps({"urls": urls, "pairs": [[a, b] for a, b, *_ in IMAGE_PAIRS]})
    res = subprocess.run([NODE, "--input-type=module", "-e", script, str(JS.as_uri()), payload],
                         capture_output=True, text=True, timeout=30, env={"PATH": ""})
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)
    # strict (Server-Standard) und unscharf (Browser-Standard, ohne Zuschnitt-Parameter) je gleich.
    assert [(k["full"], k["base"]) for k in got["keys"]] == [image_key(u) for u in urls]
    assert [(k["full"], k["base"]) for k in got["loose"]] == [image_key(u, strict=False) for u in urls]
    assert got["small"] == [is_small_image_url(u) for u in urls]
    assert got["same"] == [[client, client] for *_, client in IMAGE_PAIRS]


def test_image_seen_matches_naive_same_image_and_is_fast():
    """imageSeen (Duplikat-Bilder im Artikeltext) entscheidet wie der frühere Vergleich jedes Bildes mit
    allen gezeigten per sameImage, braucht bei Tausenden Bildern aber nur lineare Zeit."""
    from tests.test_extract import IMAGE_PAIRS

    urls = [u for a, b, *_ in IMAGE_PAIRS for u in (a, b)] + [
        "https://nyt.example/2026/09/26/world/foto-grossesbild-superJumbo.jpg",
        "https://nyt.example/2026/09/26/world/foto-grossesbild-articleLarge.jpg",
        "https://taz.example/picture/8763231/948/screenshot.jpg",
        "https://taz.example/picture/8763231/624/screenshot.jpg",
        "https://a.example/2025/10/screenshot.png", "https://a.example/2026/09/screenshot.png", None]
    script = """
const fmt = await import(process.argv[1] + '/format.js');
const urls = JSON.parse(process.argv[2]);
const naive = (list) => { const seen = []; return list.map((u) => {
  if (seen.some((s) => fmt.sameImage(s, u))) return true; seen.push(u); return false; }); };
const fast = (list) => { const seen = fmt.imageSeen(); return list.map((u) => seen(u)); };
let r = 7;   // feste Pseudo-Zufallsfolge: mehrere Reihenfolgen derselben URLs
const rnd = () => { r = (r * 1103515245 + 12345) % 2147483648; return r / 2147483648; };
const orders = [urls, [...urls].reverse()];
for (let k = 0; k < 20; k++) orders.push([...urls].sort(() => rnd() - 0.5));
const same = orders.map((o) => JSON.stringify(naive(o)) === JSON.stringify(fast(o)));
// Feindlicher Text: 2000 Bilder, gleiche Website, gleicher Dateiname, verschiedene Ordner (alle
// im selben Vergleichs-Eimer, keines gleich), dazu 1000 genaue Wiederholungen.
const big = [];
for (let i = 0; i < 2000; i++) big.push(`https://a.example/ordner${i}/gleichesbild.jpg`);
for (let i = 0; i < 1000; i++) big.push(`https://a.example/ordner${i}/gleichesbild.jpg?w=${i}`);
const small = big.slice(0, 300).concat(big.slice(2000, 2300));
const t0 = performance.now();
const res = fast(big);
const ms = performance.now() - t0;
console.log(JSON.stringify({ same, ms, dups: res.filter(Boolean).length,
  smallSame: JSON.stringify(naive(small)) === JSON.stringify(fast(small)) }));
"""
    res = subprocess.run([NODE, "--input-type=module", "-e", script, str(JS.as_uri()), json.dumps(urls)],
                         capture_output=True, text=True, timeout=60, env={"PATH": ""})
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)
    assert all(got["same"]), got["same"]
    assert got["smallSame"]
    assert got["dups"] == 1000
    assert got["ms"] < 1000, got["ms"]   # früher O(n²): mehrere Sekunden


def test_articles_loader_skips_on_response_when_stale():
    """Eine verspätete Antwort einer ersetzten Liste darf den Wasserstand (onResponse) nicht überschreiben."""
    script = """
globalThis.window = { matchMedia: () => ({ matches: false, addEventListener() {} }) };
globalThis.location = { origin: 'http://kiosk.test' };
globalThis.fetch = async () => new Response(JSON.stringify({ items: [{ id: 1 }], max_id: 7, next_before: 5 }),
  { headers: { 'content-type': 'application/json' } });
const { articlesLoader } = await import(process.argv[1] + '/ui/feed.js');
const got = [];
const load = articlesLoader({}, { onResponse: (res, cursor) => got.push([res.max_id, cursor]) });
const stale = await load(null, () => true);
await load(null, () => false);
await load(3);
console.log(JSON.stringify({ got, stale }));
"""
    res = subprocess.run([NODE, "--input-type=module", "-e", script, str(JS.as_uri())],
                         capture_output=True, text=True, timeout=30, env={"PATH": ""})
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)
    assert got["got"] == [[7, None], [7, 3]]
    assert got["stale"] == {"items": [{"id": 1}], "next": 5}
