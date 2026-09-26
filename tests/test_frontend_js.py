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
