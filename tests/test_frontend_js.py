"""Kleine Unit-Tests für reine Frontend-Funktionen (format.js, route.js), ausgeführt mit node."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

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
