"""Kleine Unit-Tests für reine Frontend-Funktionen (format.js, route.js), ausgeführt mit node."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
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


def test_chip_profiles_hides_off_profiles_but_keeps_active():
    """Filterchips der Liste: nur Profile mit in_chips (fehlt = an), das aktive immer und vorn."""
    script = """
globalThis.window = { matchMedia: () => ({ matches: false, addEventListener() {} }) };
const { chipProfiles } = await import(process.argv[1] + '/store.js');
const profiles = [{ id: 1, in_chips: true }, { id: 2, in_chips: false }, { id: 3 }, { id: 4, in_chips: false }];
const ids = (ctx, list = profiles) => chipProfiles(list, ctx).map((p) => p.id);
console.log(JSON.stringify({
  all: ids({ type: 'all' }),
  activeOn: ids({ type: 'profile', id: 3 }),
  activeOff: ids({ type: 'profile', id: '4' }),
  unknown: ids({ type: 'profile', id: 99 }),
  source: ids({ type: 'source', id: 2 }),
  none: ids({ type: 'all' }, null),
  noCtx: ids(null),
}));
"""
    res = subprocess.run([NODE, "--input-type=module", "-e", script, str(JS.as_uri())],
                         capture_output=True, text=True, timeout=30, env={"PATH": ""})
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)
    assert got["all"] == [1, 3]
    assert got["activeOn"] == [3, 1]
    assert got["activeOff"] == [4, 1, 3]    # aus der Leiste oder per Link geöffnet: sichtbar, abwählbar
    assert got["unknown"] == [1, 3]
    assert got["source"] == [1, 3]          # Zeitungs-Kontext: kein Profil aktiv
    assert got["none"] == []
    assert got["noCtx"] == [1, 3]


def test_desktop_home_helpers():
    """Helfer der rechten Seite am Desktop: Punkt-Histogramm, Quellenzeile, deutsches Datum."""
    script = """
const fmt = await import(process.argv[1] + '/format.js');
const sun = new Date(2026, 8, 27, 18, 40);   // 27.09.2026 ist ein Sonntag
console.log(JSON.stringify({
  levels: fmt.histogramLevels([0, 1, 5, 10, 3, null, -2, 'x'], 6),
  levelsZero: fmt.histogramLevels([0, 0, 0]),
  levelsNone: fmt.histogramLevels(undefined),
  levelsRows: fmt.histogramLevels([2, 4], 3),
  line: [fmt.sourcesLine(['SPIEGEL', 'GUARDIAN', 'NZZ'], 10), fmt.sourcesLine(['SPIEGEL', 'NZZ'], 2),
         fmt.sourcesLine(['SPIEGEL', null, 'NZZ'], 1), fmt.sourcesLine([], 4), fmt.sourcesLine(null, 0)],
  longDate: [fmt.longDate('2026-09-27'), fmt.longDate('2026-03-02'), fmt.longDate(sun), fmt.longDate('kaputt'), fmt.longDate(null)],
  shortDate: [fmt.shortDate('2026-09-27'), fmt.shortDate(new Date(2026, 0, 5)), fmt.shortDate('')],
  clock: fmt.clockTime(new Date(2026, 8, 27, 8, 5)),
}));
"""
    res = subprocess.run([NODE, "--input-type=module", "-e", script, str(JS.as_uri())],
                         capture_output=True, text=True, timeout=30, env={"TZ": "Europe/Berlin", "PATH": ""})
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)
    # ceil(n / max * 6): 1/10 → 1 Punkt (nie 0 bei Artikeln), 5/10 → 3, 3/10 → 2; kaputte Werte → 0.
    assert got["levels"] == [0, 1, 3, 6, 2, 0, 0, 0]
    assert got["levelsZero"] == [0, 0, 0]
    assert got["levelsNone"] == []
    assert got["levelsRows"] == [2, 3]
    assert got["line"] == ["SPIEGEL, GUARDIAN, NZZ +7", "SPIEGEL, NZZ", "SPIEGEL, NZZ", "+4", ""]
    assert got["longDate"] == ["Sonntag 27. September", "Montag 2. März", "Sonntag 27. September", "", ""]
    assert got["shortDate"] == ["So 27.09.", "Mo 05.01.", ""]
    assert got["clock"] == "08:05"


# Kleines Schein-DOM für Module, die Elemente bauen (h() in ui/dom.js): genug für Aufbau, Klassen,
# Attribute, Ereignisse und einfache Klassen-Selektoren. Kein Layout, kein Rendern.
FAKE_DOM = r"""
class FakeNode { constructor() { this.childNodes = []; this.parentNode = null; }
  get parentElement() { return this.parentNode; }
  remove() { if (this.parentNode) { const c = this.parentNode.childNodes; c.splice(c.indexOf(this), 1); this.parentNode = null; } } }
class FakeText extends FakeNode { constructor(t) { super(); this.data = String(t); }
  get textContent() { return this.data; } set textContent(v) { this.data = String(v); } }
class FakeEl extends FakeNode {
  constructor(tag) { super(); this.tagName = tag.toUpperCase(); this.attrs = {}; this.dataset = {}; this.style = {};
    this.listeners = {}; this.className = '';
    const self = this;
    this.classList = { add: (...c) => { for (const x of c) if (!self.cls().includes(x)) self.className = [...self.cls(), x].join(' '); },
      remove: (...c) => { self.className = self.cls().filter((x) => !c.includes(x)).join(' '); },
      toggle: (c, on) => { const has = self.cls().includes(c); const want = on === undefined ? !has : !!on;
        if (want) self.classList.add(c); else self.classList.remove(c); return want; },
      contains: (c) => self.cls().includes(c) }; }
  cls() { return this.className.split(/\s+/).filter(Boolean); }
  get children() { return this.childNodes.filter((n) => n instanceof FakeEl); }
  get firstChild() { return this.childNodes[0] || null; }
  get firstElementChild() { return this.children[0] || null; }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(''); }
  set textContent(v) { this.replaceChildren(); if (v !== '' && v != null) this.append(String(v)); }
  set innerHTML(v) { this._html = v; }
  append(...kids) { for (let k of kids) { if (!(k instanceof FakeNode)) k = new FakeText(k); k.remove(); k.parentNode = this; this.childNodes.push(k); } }
  prepend(...kids) { const old = this.childNodes.splice(0); this.append(...kids); for (const o of old) this.childNodes.push(o); }
  replaceChildren(...kids) { for (const c of this.childNodes) c.parentNode = null; this.childNodes = []; this.append(...kids); }
  after(k) { const p = this.parentNode; k.remove(); k.parentNode = p; p.childNodes.splice(p.childNodes.indexOf(this) + 1, 0, k); }
  get nextElementSibling() { const p = this.parentNode; if (!p) return null; const c = p.children; return c[c.indexOf(this) + 1] || null; }
  insertBefore(k, ref) { this.append(k); if (ref) { this.childNodes.pop(); this.childNodes.splice(this.childNodes.indexOf(ref), 0, k); } }
  setAttribute(k, v) { this.attrs[k] = String(v); } getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; } hasAttribute(k) { return k in this.attrs; }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); } removeEventListener() {}
  focus() { document.activeElement = this; }
  contains(n) { for (let x = n; x; x = x.parentNode) if (x === this) return true; return false; }
  closest() { return null; }
  get isConnected() { return false; }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  querySelectorAll(sel) { const m = /^\.([\w-]+)$/.exec(sel); return m ? this.all().filter((e) => e.cls().includes(m[1])) : []; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
const ids = {};
for (const id of ['shell', 'view', 'reader', 'focus-rail', 'bar']) { ids[id] = new FakeEl('div'); ids[id].id = id; }
ids.bar.hidden = true;
globalThis.Node = FakeNode;
globalThis.document = { createElement: (t) => new FakeEl(t), createElementNS: (ns, t) => new FakeEl(t),
  createTextNode: (t) => new FakeText(t), getElementById: (id) => ids[id] || null, addEventListener() {},
  querySelector: () => null, querySelectorAll: () => [], documentElement: new FakeEl('html'), body: new FakeEl('body'),
  activeElement: null, visibilityState: 'visible' };
let desktop = true;
globalThis.window = { matchMedia: () => ({ get matches() { return desktop; }, addEventListener() {}, removeEventListener() {} }) };
globalThis.location = { origin: 'http://kiosk.test', hash: '#/' };
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.CSS = { escape: (s) => s };
const calls = [];
let route = () => ({});
globalThis.fetch = async (url, init = {}) => {
  const u = new URL(url); const body = init.body ? JSON.parse(init.body) : undefined;
  calls.push([init.method || 'GET', u.pathname, body]);
  const out = await route(u.pathname, init.method || 'GET', body);
  return new Response(JSON.stringify(out), { headers: { 'content-type': 'application/json' } });
};
const tick = () => new Promise((r) => setTimeout(r, 0));
const defer = () => { let resolve, reject; const p = new Promise((a, b) => { resolve = a; reject = b; }); return { p, resolve, reject }; };
"""


def _run_fake_dom(script: str) -> dict:
    res = subprocess.run([NODE, "--input-type=module", "-e", FAKE_DOM + script, str(JS.as_uri())],
                         capture_output=True, text=True, timeout=30, env={"TZ": "Europe/Berlin", "PATH": ""})
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_serial_saver_sends_last_value_and_rolls_back_to_confirmed():
    """Fokusmodus schnell umschalten (Taste F): eine Anfrage zugleich, danach der letzte Wert; bei einem
    Fehler gilt der zuletzt bestätigte Wert, nicht der vorige Zwischenstand."""
    script = """
const { serialSaver } = await import(process.argv[1] + '/saver.js');
function setup(initial) {
  const s = { local: initial, sent: [], pending: [], applied: [] };
  s.saver = serialSaver({ get: () => s.local, apply: (v, err) => { s.local = v; s.applied.push([v, !!err]); },
    send: (v) => { s.sent.push(v); const d = defer(); s.pending.push(d); return d.p; } });
  s.set = (v) => { s.saver.remember(); s.local = v; return s.saver.flush(); };
  return s;
}
const out = {};
// an, aus, an während die erste Anfrage läuft: nur eine Anfrage, Ergebnis an
let s = setup(false);
let p1 = s.set(true); s.set(false); const p3 = s.set(true);
await tick(); s.pending[0].resolve(true); out.same = [await p1, await p3, s.sent, s.local];
// an, dann aus während sie läuft: danach genau eine weitere Anfrage mit aus
s = setup(false);
p1 = s.set(true); s.set(false); await tick(); s.pending[0].resolve(true); await tick(); await tick();
s.pending[1].resolve(false); out.follow = [await p1, s.sent, s.local];
// Fehler: zurück auf den bestätigten Wert (aus), nicht auf den Zwischenstand
s = setup(false);
p1 = s.set(true); s.set(false); s.set(true); await tick(); s.pending[0].reject(new Error('weg'));
out.fail = [await p1, s.local, s.applied];
// Fehler bei der Folgeanfrage: bestätigt war an
s = setup(false);
p1 = s.set(true); s.set(false); await tick(); s.pending[0].resolve(true); await tick(); await tick();
s.pending[1].reject(new Error('weg')); out.failLater = [await p1, [...s.sent], s.local];
// Danach wieder normal: neuer Ausgangswert ist der aktuelle
s.set(false); await tick(); out.again = [s.sent, s.local];
console.log(JSON.stringify(out));
"""
    got = _run_fake_dom(script)
    assert got["same"] == [True, True, [True], True]
    assert got["follow"] == [True, [True, False], False]
    assert got["fail"] == [False, False, [[False, True]]]
    assert got["failLater"] == [False, [True, False], True]
    assert got["again"] == [[True, False, False], False]


def test_set_focus_mode_serializes_patches_and_rolls_back():
    """setFocusMode (focus.js) mit Schein-API: schnelles Umschalten schickt nacheinander, der Server
    endet beim letzten Wert; scheitert das Speichern, gilt der bestätigte Wert auch im DOM (data-focus)."""
    script = """
const store = await import(process.argv[1] + '/store.js');
const { setFocusMode } = await import(process.argv[1] + '/views/focus.js');
ids.shell.dataset.layout = 'article';
let gate = defer(); let fail = false;
route = async (path, method, body) => { if (method === 'PATCH') { await gate.p; if (fail) throw new TypeError('offline');
  return { ...store.state.settings, focus_mode: body.focus_mode }; } return {}; };
const patches = () => calls.filter((c) => c[0] === 'PATCH').map((c) => c[2].focus_mode);
const a = setFocusMode(true); setFocusMode(false); const c = setFocusMode(true);
await tick(); await tick();
const during = [document.documentElement.dataset.focus, patches()];
gate.resolve(); const ok = [await a, await c];
const after = [store.state.settings.focus_mode, document.documentElement.dataset.focus, patches()];
gate = defer(); fail = true;
const d = setFocusMode(false); setFocusMode(true); setFocusMode(false);
await tick(); gate.resolve(); const failed = await d;
console.log(JSON.stringify({ during, ok, after, failed, rolled: [store.state.settings.focus_mode,
  document.documentElement.dataset.focus], readerFocused: document.activeElement === ids.reader }));
"""
    got = _run_fake_dom(script)
    assert got["during"] == ["on", [True]]           # sofort an, eine Anfrage unterwegs
    assert got["ok"] == [True, True]
    assert got["after"] == [True, "on", [True]]       # Endstand an: keine weitere Anfrage nötig
    assert got["failed"] is False
    assert got["rolled"] == [True, "on"]              # zurück auf den bestätigten Wert
    assert got["readerFocused"] is True               # Fokusmodus an: Tastatur auf der Leseansicht


def test_bar_wanted_and_home_visible():
    """Leiste unten und rechte Seite ohne Artikel (layout.js)."""
    script = """
const { barWanted, homeVisible } = await import(process.argv[1] + '/layout.js');
const b = (o) => barWanted({ viewLayout: 'list', viewBar: true, layout: 'list', desktop: true, focus: false, ...o });
console.log(JSON.stringify({
  bar: [b({}), b({ layout: 'article' }), b({ layout: 'article', focus: true }), b({ layout: 'article', desktop: false }),
        b({ focus: true }), b({ viewBar: false }), b({ viewLayout: 'page', layout: 'page' }), b({ layout: 'page' })],
  home: [homeVisible({ authed: true, readerOpen: false, layout: 'list', desktop: true }),
         homeVisible({ authed: true, readerOpen: true, layout: 'article', desktop: true }),
         homeVisible({ authed: true, readerOpen: false, layout: 'list', desktop: false }),
         homeVisible({ authed: true, readerOpen: false, layout: 'page', desktop: true }),
         homeVisible({ authed: null, readerOpen: false, layout: 'list', desktop: true })],
}));
"""
    got = _run_fake_dom(script)
    # Fokusmodus blendet die Leiste nur mit offenem Artikel aus; am Handy mit Artikel nie.
    assert got["bar"] == [True, True, False, False, True, False, False, False]
    assert got["home"] == [True, False, False, False, False]


def test_home_view_switches_kind_and_resets_on_user_change():
    """Rechte Seite ohne Artikel: lädt nur sichtbar, wechselt mit desktop_empty zwischen Übersicht und
    Titelseite, übersteht Teilantworten und zeigt nach reset() (Benutzerwechsel) nichts Altes mehr."""
    script = """
const store = await import(process.argv[1] + '/store.js');
const { readerHome } = await import(process.argv[1] + '/views/home.js');
let slow = null;
route = async (path) => {
  if (slow && path !== '/api/profiles') await slow.p;
  if (path === '/api/profiles') return [{ id: 1, name: 'Deutschland', unread: 4 }, null];
  if (path === '/api/overview') return { date: '2026-09-27', now_hour: new Date().getHours(),
    counts: { new_today: 5 }, top_stories: [null, { article: {} }, { sources_count: 3, sources: ['A'], article: { id: 7, title: 'T' } }],
    hourly: [1, 2] };
  if (path === '/api/frontpage') return { lead: { article: { title: 'ohne id' } },
    sections: [null, { profile_id: 1, name: 'Deutschland', items: [null, { article: { id: 9, title: 'X' } }] }, { items: [] }] };
  return {};
};
const home = readerHome();
const kinds = () => home.el.children.map((c) => c.className);
const hidden = calls.length; store.emit('unread-changed');
const before = [calls.length, home.el.children.length];
home.setVisible(true); await tick(); await tick(); await tick();
const overview = [kinds(), home.el.querySelectorAll('.home-story').length, home.el.querySelectorAll('.home-profile').length,
  home.label()];
store.state.settings.desktop_empty = 'frontpage'; store.emit('settings', { desktop_empty: 'frontpage' });
await tick(); await tick(); await tick();
const front = [kinds(), home.el.querySelectorAll('.fp-lead').length, home.el.querySelectorAll('.fp-item').length, home.label()];
slow = defer(); store.state.settings.desktop_empty = 'overview'; store.emit('settings', { desktop_empty: 'overview' });
await tick(); home.reset(); slow.resolve(); await tick(); await tick(); await tick();
const reset = [home.el.children.length];
// Neu sichtbar, Server nicht erreichbar: kurzer Hinweis statt Platzhalter
const ok = route; route = async (path) => { if (path === '/api/overview') throw new TypeError('offline'); return ok(path); };
home.setVisible(true); await tick(); await tick(); await tick();
const down = home.el.textContent;
home.reset();
console.log(JSON.stringify({ hidden, before, overview, front, reset, down }));
process.exit(0);
"""
    got = _run_fake_dom(script)
    assert got["before"] == [got["hidden"], 0]                    # unsichtbar: keine Anfrage
    assert got["overview"][0] == ["home-page home-page--overview"]
    assert got["overview"][1:] == [1, 1, "Übersicht"]            # Einträge ohne Artikel-ID fallen weg
    assert got["front"][0] == ["home-page home-page--front"]
    assert got["front"][1:] == [0, 1, "Titelseite"]              # Aufmacher ohne ID fehlt, eine Karte bleibt
    assert got["reset"] == [0]                                    # späte Antwort nach reset() zeigt nichts
    assert got["down"].startswith("HEUTE") and "Übersicht gerade nicht erreichbar" in got["down"]   # Kopf bleibt


def test_serial_saver_no_change_flush_and_reset():
    """flush() ohne Änderung blockiert nichts; reset() (Abmelden) beendet ein laufendes Speichern,
    ohne danach noch etwas zu setzen."""
    script = """
const { serialSaver } = await import(process.argv[1] + '/saver.js');
function setup(initial) {
  const s = { local: initial, sent: [], pending: [], applied: [] };
  s.saver = serialSaver({ get: () => s.local, apply: (v, err) => { s.local = v; s.applied.push([v, !!err]); },
    send: (v) => { s.sent.push(v); const d = defer(); s.pending.push(d); return d.p; } });
  s.set = (v) => { s.saver.remember(); s.local = v; return s.saver.flush(); };
  return s;
}
const out = {};
let s = setup(false);
s.saver.remember(); const idle = await s.saver.flush();
out.noChange = [idle, s.saver.current(), s.sent.length];
const p = s.set(true); await tick(); s.pending[0].resolve(true);
out.after = [await p, [...s.sent], s.saver.current()];
// Abmelden während des Speicherns: kein Zurückrollen, keine Meldung, nichts mehr gesendet
s = setup(false);
const q = s.set(true); await tick(); s.saver.reset(); s.local = false;   // nächster Benutzer: aus
s.pending[0].reject(new Error('401'));
out.reset = [await q, s.local, s.applied, s.saver.current()];
// danach arbeitet der Speicher normal weiter
const r = s.set(true); await tick(); s.pending[1].resolve(true);
out.fresh = [await r, [...s.sent]];
console.log(JSON.stringify(out));
"""
    got = _run_fake_dom(script)
    assert got["noChange"] == [True, None, 0]
    assert got["after"] == [True, [True], None]
    assert got["reset"] == [False, False, [], None]
    assert got["fresh"] == [True, [True, True]]


def test_save_settings_does_not_clobber_running_focus_save():
    """Taste F (Speichern verzögert) und gleichzeitig eine andere Einstellung: saveSettings übernimmt aus
    seiner Antwort nur die eigenen Schlüssel, Server und Browser enden mit Fokusmodus an. Solange das
    Speichern läuft, meldet setFocusMode keinen Erfolg vorab."""
    script = """
const store = await import(process.argv[1] + '/store.js');
const { setFocusMode } = await import(process.argv[1] + '/views/focus.js');
ids.shell.dataset.layout = 'article';
const server = { ...store.DEFAULT_SETTINGS };
let gate = defer();
route = async (path, method, body) => {
  if (method !== 'PATCH') return {};
  if ('focus_mode' in body) await gate.p;
  Object.assign(server, body);
  return { ...server };
};
const out = {};
const f = setFocusMode(true);
await store.saveSettings({ text_size: 'l' });            // Antwort kennt focus_mode noch als false
out.mid = [store.state.settings.focus_mode, document.documentElement.dataset.focus, store.state.settings.text_size];
let early = 'pending'; setFocusMode(true).then((v) => { early = v; });   // schon an: laufendes Speichern
await tick(); out.early = early;
gate.resolve(); out.done = await f; await tick();
out.end = [server.focus_mode, store.state.settings.focus_mode, document.documentElement.dataset.focus, server.text_size];
// Einstellungsseite: Titelseite wählen und Fokusmodus ausschalten, beides zugleich
gate = defer();
const g = setFocusMode(false);
await store.saveSettings({ desktop_empty: 'frontpage' });
gate.resolve(); await g; await tick();
out.page = [server.focus_mode, store.state.settings.focus_mode, server.desktop_empty, store.state.settings.desktop_empty];
console.log(JSON.stringify(out));
"""
    got = _run_fake_dom(script)
    assert got["mid"] == [True, "on", "l"]
    assert got["early"] == "pending"
    assert got["done"] is True
    assert got["end"] == [True, True, "on", "l"]
    assert got["page"] == [False, False, "frontpage", "frontpage"]


def test_settings_focus_switch_shows_real_state_after_failed_saves():
    """Einstellungen: zwei schnelle Klicks auf „Fokusmodus“, Speichern scheitert: der Schalter zeigt
    danach den tatsächlichen Stand (aus), nicht den Zwischenstand."""
    script = """
const store = await import(process.argv[1] + '/store.js');
const { toggle } = await import(process.argv[1] + '/ui/toggle.js');
const { focusSwitchChange } = await import(process.argv[1] + '/views/focus.js');
ids.shell.dataset.layout = 'list';
let gate = defer(); let fail = true;
route = async (path, method, body) => { if (method === 'PATCH') { await gate.p; if (fail) throw new TypeError('offline');
  return { ...store.state.settings, ...body }; } return {}; };
const sw = toggle(false, 'Fokusmodus', focusSwitchChange);
const click = () => sw.listeners.click[0]({ preventDefault() {}, stopPropagation() {} });
const c1 = click(); const c2 = click();
const mid = [sw.getAttribute('aria-checked'), store.state.settings.focus_mode];
await tick(); gate.resolve(); await c1; await c2; await tick();
const failed = [sw.getAttribute('aria-checked'), store.state.settings.focus_mode];
gate = defer(); fail = false;
const c3 = click(); await tick(); gate.resolve(); await c3; await tick();
console.log(JSON.stringify({ mid, failed, ok: [sw.getAttribute('aria-checked'), store.state.settings.focus_mode] }));
"""
    got = _run_fake_dom(script)
    assert got["mid"] == ["false", False]      # an, gleich wieder aus (lokal)
    assert got["failed"] == ["false", False]   # Schalter und Zustand stimmen überein
    assert got["ok"] == ["true", True]


def test_tiny_inline_images_are_dropped_but_deliberately_small_ones_stay():
    """Bilder im Text unter 120 px Breite (taz-Variante /14/) fallen nach dem Laden weg (Handy und
    Desktop), die Bildzeile bleibt; kleine laut URL, width/height oder Autorenfoto bleiben."""
    script = """
const { dropWhenTiny } = await import(process.argv[1] + '/views/article.js');
desktop = false;   // gilt auch am Handy
const body = document.createElement('div');
function img(src, natural, attrs = {}) {
  const el = document.createElement('img'); el.setAttribute('src', src);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  el.naturalWidth = natural; body.append(el);
  const cap = document.createElement('p'); body.append(cap);
  dropWhenTiny(el, cap, 'Anna Autorin');
  el.listeners.load.forEach((fn) => fn());
  return [!!el.parentNode, cap.classList.contains('art-caption')];
}
console.log(JSON.stringify({
  tiny: img('https://taz.de/picture/8765303/14/0861.webp', 14),
  big: img('https://taz.de/picture/8765303/948/0861.webp', 948),
  smallUrl: img('https://cdn.spiegel.de/autor_w72.jpg', 72),
  smallAttr: img('https://a.example/icon.png', 40, { width: '40' }),
  author: img('https://a.example/kopf.jpg', 96, { alt: 'Anna Autorin' }),
  unknown: img('https://a.example/x.jpg', 0),
  heroCopy: (() => { const el = document.createElement('img'); el.dataset.heroCopy = '1'; el.naturalWidth = 14; body.append(el);
    dropWhenTiny(el, null, ''); el.listeners.load.forEach((fn) => fn()); return !!el.parentNode; })(),
}));
"""
    got = _run_fake_dom(script)
    assert got["tiny"] == [False, True]        # weg, Bildzeile bleibt als Bildzeile
    assert got["big"] == [True, False]
    assert got["smallUrl"] == [True, False]
    assert got["smallAttr"] == [True, False]
    assert got["author"] == [True, False]
    assert got["unknown"] == [True, False]     # Breite unbekannt: bleibt
    assert got["heroCopy"] is True             # Wiederholung des Titelbilds: regelt das Titelbild


def test_tiny_image_credit_moves_under_hero_or_goes():
    """taz #158: Nach dem winzigen Bild bleibt nur „Foto: …“. Gibt es ein Titelbild, wandert die Zeile
    darunter (hat es schon eine, entfällt sie); eine Bildzeile mit Inhalt bleibt, wo sie ist."""
    script = """
const { dropIfTiny } = await import(process.argv[1] + '/views/article.js');
desktop = false;
function scene(capText, heroCaption) {
  const art = document.createElement('div');
  const hero = document.createElement('img'); hero.className = 'art-hero'; art.append(hero);
  if (heroCaption) { const c = document.createElement('p'); c.className = 'art-caption'; c.textContent = 'Foto: Hero'; art.append(c); }
  const body = document.createElement('div'); art.append(body);
  const img = document.createElement('img'); img.setAttribute('src', 'https://taz.de/picture/1/14/0861.webp'); img.naturalWidth = 14;
  const cap = document.createElement('p'); cap.textContent = capText; body.append(img, cap);
  const removed = dropIfTiny(img, cap, '', () => hero);
  return { removed, where: cap.parentNode === art ? art.children.indexOf(cap) : cap.parentNode === body ? 'body' : 'weg',
    cls: cap.className, moved: cap.dataset.movedCredit || null };
}
console.log(JSON.stringify({
  toHero: scene('Foto: Toby Melville/reuters', false),
  heroHasOne: scene('Foto: Toby Melville/reuters', true),
  realCaption: scene('Demonstrierende vor dem Kanzleramt am Sonntagabend in Berlin, dahinter die Polizei', false),
}));
"""
    got = _run_fake_dom(script)
    assert got["toHero"] == {"removed": True, "where": 1, "cls": "art-caption", "moved": "1"}   # direkt unter dem Titelbild
    assert got["heroHasOne"]["removed"] is True and got["heroHasOne"]["where"] == "weg"
    assert got["realCaption"] == {"removed": True, "where": "body", "cls": "art-caption", "moved": None}


def test_overview_and_frontpage_handle_24h_window_and_missing_numbers():
    """Kurz nach Mitternacht (window '24h'): MEISTBERICHTET und der Kopf der Titelseite sagen
    „Letzte 24 Std“. Fehlende Zähler zeigen „–“, leeres hourly zeigt nur leere Punkte."""
    script = """
const { overviewContent } = await import(process.argv[1] + '/views/overview.js');
const { frontpageContent } = await import(process.argv[1] + '/views/frontpage.js');
const now = new Date(2026, 8, 28, 0, 20);
const cap = (el, cls) => el.querySelectorAll(cls).map((e) => e.textContent);
const o24 = overviewContent({ window: '24h', date: '2026-09-28', now_hour: 0,
  counts: { new_today: null, unread_today: 3, later: 0, active_sources: null }, top_stories: [], hourly: [] }, now);
const oDay = overviewContent({ window: 'today', counts: {}, hourly: [1, 2] }, now);
const f24 = frontpageContent({ window: '24h', lead: null, sections: [{ profile_id: 1, name: 'P', items: [{ article: { id: 3, title: 'X' } }] }] }, now);
const fDay = frontpageContent({ lead: null, sections: [{ profile_id: 1, name: 'P', items: [{ article: { id: 3, title: 'X' } }] }] }, now);
console.log(JSON.stringify({
  heads24: o24.querySelectorAll('.home-head').map((e) => e.textContent),
  headsDay: oDay.querySelectorAll('.home-head').map((e) => e.textContent).slice(0, 1),
  tiles: cap(o24, '.home-tile-num'),
  histCols: o24.querySelectorAll('.hist-col').length,
  histOn: o24.querySelectorAll('.on').length,
  front24: f24.querySelector('.home-top').textContent,
  frontDay: fDay.querySelector('.home-top').textContent,
}));
"""
    got = _run_fake_dom(script)
    assert got["heads24"][0] == "MEISTBERICHTETLetzte 24 Std"
    assert got["headsDay"] == ["MEISTBERICHTETZeitungen"]
    assert got["tiles"] == ["–", "3", "0", "–"]
    assert got["histCols"] == 24 and got["histOn"] == 0
    assert got["front24"].endswith("/ Letzte 24 Std") and "Profilen" not in got["front24"]
    assert got["frontDay"].endswith("/ aus deinen Profilen")


def test_list_width_clamp_and_stored_values():
    """Ziehbare Artikelliste (listwidth.js): Grenzen je Fensterbreite, kaputte gespeicherte Werte,
    Tastatur, Zurücksetzen."""
    script = """
const lw = await import(process.argv[1] + '/listwidth.js');
console.log(JSON.stringify({
  max: [lw.listMax(900), lw.listMax(1280), lw.listMax(1600), lw.listMax(1894), lw.listMax(3000), lw.listMax(undefined)],
  clamp: [lw.clampListWidth(100, 1600), lw.clampListWidth(600.4, 1600), lw.clampListWidth(900, 1600),
          lw.clampListWidth(700, 1280), lw.clampListWidth(700, 1600), lw.clampListWidth(null, 1600),
          lw.clampListWidth(NaN, 1600), lw.clampListWidth('600', 1600), lw.clampListWidth(Infinity, 1894)],
  parse: [lw.parseStoredWidth('600'), lw.parseStoredWidth(null), lw.parseStoredWidth(''), lw.parseStoredWidth('abc'),
          lw.parseStoredWidth('"600"'), lw.parseStoredWidth('1e999'), lw.parseStoredWidth('{}'), lw.parseStoredWidth('412.5')],
  keys: [lw.keyWidth('ArrowLeft', 520, 1600), lw.keyWidth('ArrowRight', 520, 1600), lw.keyWidth('ArrowLeft', 330, 1600),
         lw.keyWidth('ArrowRight', 795, 1600), lw.keyWidth('Home', 600, 1600), lw.keyWidth('End', 400, 1280),
         lw.keyWidth('j', 520, 1600)],
  // Fenster wird schmaler und wieder breiter: angewendet begrenzt, gewählt bleibt gespeichert
  shrink: [lw.clampListWidth(740, 1200), lw.clampListWidth(740, 1600)],
  reset: lw.clampListWidth(lw.parseStoredWidth(null) ?? lw.LIST_DEFAULT, 1600),
}));
"""
    got = _run_fake_dom(script)
    assert got["max"] == [520, 640, 760, 760, 760, 520]   # Hälfte des Fensters, 520..760
    assert got["clamp"] == [320, 600, 760, 640, 700, 520, 520, 520, 520]   # Unbrauchbares: Standard
    assert got["parse"] == [600, None, None, None, None, None, None, 412.5]
    assert got["keys"] == [504, 536, 320, 760, 320, 640, None]
    assert got["shrink"] == [600, 740]
    assert got["reset"] == 520


def test_list_resizer_drag_click_keys_and_focus_mode():
    """Ziehbare Trennlinie (ui/splitter.js) mit Schein-DOM: Ziehen speichert, ein Klick nicht,
    Doppelklick setzt zurück, Fenstergröße begrenzt ohne die Wahl zu vergessen, lostpointercapture
    beendet das Ziehen, Fokusmodus mitten im Ziehen bricht ab, Tasten mit preventDefault."""
    script = """
const store = new Map();
globalThis.localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k) };
const winListeners = {};
Object.assign(globalThis.window, { innerWidth: 1600, addEventListener: (t, fn) => { (winListeners[t] ||= []).push(fn); } });
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
globalThis.cancelAnimationFrame = (id) => clearTimeout(id);
let hidden = false;
FakeEl.prototype.getClientRects = function () { return hidden ? [] : [{}]; };
FakeEl.prototype.setPointerCapture = function (id) { this.cap = id; };
FakeEl.prototype.hasPointerCapture = function (id) { return this.cap === id; };
FakeEl.prototype.releasePointerCapture = function () { this.cap = null; };
ids.shell.style.setProperty = (k, v) => { ids.shell.style[k] = v; };
ids.shell.append(ids.reader);
Object.assign(ids.reader, { scrollHeight: 3000, clientHeight: 1000, scrollTop: 0 });
const storeMod = await import(process.argv[1] + '/store.js');
const { installListResizer } = await import(process.argv[1] + '/ui/splitter.js');
const hd = installListResizer();
const fire = (type, ev = {}) => hd.listeners[type].forEach((fn) => fn({ button: 0, pointerId: 1, preventDefault() {}, stopPropagation() {}, ...ev }));
const st = () => [ids.shell.style['--list-w'], hd.getAttribute('aria-valuenow'), store.get('kiosk.listWidth') ?? null];
const out = {};
out.start = [...st(), hd.getAttribute('aria-valuetext'), hd.getAttribute('aria-valuemax'), hd.getAttribute('role')];
fire('pointerdown', { clientX: 520 }); fire('pointerup', { clientX: 520 }); await tick();
out.click = st();
fire('pointerdown', { clientX: 520 }); fire('pointermove', { clientX: 600 }); await tick(); fire('pointerup', { clientX: 600 }); await tick();
out.drag = st();
fire('pointerdown', { clientX: 600 }); fire('pointermove', { clientX: 601 }); await tick(); fire('pointerup', { clientX: 601 }); await tick();
out.clickAfter = st();
window.innerWidth = 1000; winListeners.resize.forEach((fn) => fn()); await tick();
out.narrow = [...st(), hd.getAttribute('aria-valuemax')];
window.innerWidth = 1600; winListeners.resize.forEach((fn) => fn()); await tick();
out.wide = st();
fire('pointerdown', { clientX: 600 }); fire('pointermove', { clientX: 700 }); await tick(); fire('lostpointercapture', { clientX: 700 });
fire('pointermove', { clientX: 400 }); await tick();
out.lost = st();
fire('pointerdown', { clientX: 700 }); fire('pointermove', { clientX: 650 }); await tick();
const mid = st();
storeMod.emit('settings', { focus_mode: true }); hidden = true;
fire('pointermove', { clientX: 500 }); await tick(); fire('pointerup', { clientX: 500 }); await tick();
out.focus = [mid, st()];
hidden = false;
let prevented = 0; const key = (k) => hd.listeners.keydown.forEach((fn) => fn({ key: k, preventDefault() { prevented += 1; }, stopPropagation() {} }));
key('ArrowLeft'); const left = st(); key('End'); const end = st(); key('Home'); const home = st(); key('j');
out.keys = [left, end, home, prevented];
hd.listeners.dblclick.forEach((fn) => fn({})); out.reset = st();
console.log(JSON.stringify(out));
process.exit(0);
"""
    got = _run_fake_dom(script)
    assert got["start"] == ["520px", "520", None, "520 Pixel", "760", "separator"]
    assert got["click"] == ["520px", "520", None]          # Klick speichert nichts
    assert got["drag"] == ["600px", "600", "600"]
    assert got["clickAfter"] == ["600px", "600", "600"]    # 1 px ist kein Ziehen
    assert got["narrow"] == ["520px", "520", "600", "520"]  # begrenzt, Wahl bleibt gespeichert
    assert got["wide"] == ["600px", "600", "600"]
    assert got["lost"] == ["700px", "700", "700"]           # Ende per lostpointercapture, danach nichts mehr
    assert got["focus"] == [["650px", "650", "700"], ["700px", "700", "700"]]   # abgebrochen, nicht gespeichert
    assert got["keys"] == [["684px", "684", "684"], ["760px", "760", "760"], ["320px", "320", "320"], 3]
    assert got["reset"] == ["520px", "520", None]


def test_match_sources_finds_newspapers_in_search():
    """Zeitungen in der Suche (sourceMatch.js): Wortanfänge in Name und Kurzname, ohne Leerzeichen,
    Akzente und ae/oe/ue, FTS-Operatoren; Reihenfolge und Monogramm."""
    script = """
const { matchSources, monogram, monograms } = await import(process.argv[1] + '/sourceMatch.js');
const S = [
  { id: 'spiegel', name: 'Der Spiegel', short: 'SPIEGEL' },
  { id: 'sz', name: 'Süddeutsche Zeitung', short: 'SZ' },
  { id: 'faz', name: 'Frankfurter Allgemeine', short: 'FAZ' },
  { id: 'ntv', name: 'n-tv', short: 'N-TV' },
  { id: 'thelocalde', name: 'The Local Germany', short: 'LOCAL DE' },
  { id: 'thelocalat', name: 'The Local Austria', short: 'LOCAL AT' },
  { id: 'thelocalch', name: 'The Local Switzerland', short: 'LOCAL CH', enabled: false },
  { id: 'taipeitimes', name: 'Taipei Times', short: 'TAIPEI TIMES', enabled: false },
  { id: 'japantimes', name: 'The Japan Times', short: 'JAPAN TIMES' },
  { id: 'dw', name: 'Deutsche Welle', short: 'DW' },
  { id: 'hurriyet', name: 'Hürriyet Daily News', short: 'HÜRRIYET' },
  { id: 'globeandmail', name: 'The Globe and Mail', short: 'GLOBE & MAIL' },
  { id: 'tagesspiegel', name: 'Tagesspiegel', short: 'TAGESSPIEGEL' },
  { id: 'fr', name: 'Frankfurter Rundschau', short: 'FR' },
  { id: 'derstandard', name: 'Der Standard', short: 'STANDARD' },
];
const ids = (q, list = S) => matchSources(list, q).map((s) => s.id);
const queries = ['spiegel', 'Spiegel', 'der spiegel', 'derspiegel', 'SPIEGEL', 'sz', 'faz', 'süddeutsche',
  'suddeutsche', 'sueddeutsche', 'Süddeutsche Zeitung', 'local', 'times', 'taiwan', 'trump', 'spiegel ukraine',
  'n-tv', 'ntv', 's', ' s ', '', '"spiegel"', 'spiegel AND', 'AND OR', 'hurriyet', 'huerriyet', 'globe mail',
  'japan', 'taipei', 'the', 'dw', 'a*', '"s"', 's.', 'de', 'DE', 'ai', 'eu', 'fr', 'SZ', 'n tv', 'spiegel NOT taz',
  '-spiegel', 'spiegel -ukraine', 'Su\u0308ddeutsche'];
// Rangfolge: genau vor Liebling vor eingeschaltet vor erstes Wort vor Name.
const R = [
  { id: 'b', name: 'Beta Local', enabled: true },
  { id: 'a', name: 'Alpha Local', enabled: true },
  { id: 'c', name: 'Local Courier', enabled: true },
  { id: 'd', name: 'Delta Local', enabled: false },
  { id: 'e', name: 'Echo Local', enabled: false, is_favorite: true },
  { id: 'f', name: 'Local', short: 'LOC', enabled: false },
];
console.log(JSON.stringify({
  q: Object.fromEntries(queries.map((q) => [q, ids(q)])),
  rank: ids('local', R),
  rankFav: ids('loc', R),
  none: [matchSources(null, 'spiegel'), matchSources(S, null)],
  dedupe: Object.fromEntries(monograms([
    { id: 'tehran', short: 'TEHRAN TIMES' }, { id: 'taipei', short: 'TAIPEI TIMES' }, { id: 'focus', short: 'FOCUS' },
    { id: 'foto', name: 'Fotomagazin' }, { id: 'a1', short: 'ABC' }, { id: 'a2', short: 'ABC' },
    { id: 'heise', short: 'HEISE' }, { id: 'helsinki', short: 'HELSINKI' }, { id: 'spiegel', short: 'SPIEGEL' },
    { id: 'lde', short: 'LOCAL DE' }, { id: 'ldk', short: 'LOCAL DK' },
    { id: 'tagesschau', short: 'TAGESSCHAU' }, { id: 'tagesspiegel', short: 'TAGESSPIEGEL' },
    { id: 'derstandard', short: 'STANDARD' }, { id: 'newstatesman', short: 'STATESMAN' }])),
  dedupeNone: [...monograms(null)],
  nfc: ids('muenchner', [{ id: 'mm', name: 'Mu\\u0308nchner Merkur', short: 'MM' }]),
  mono: [...S.map(monogram), monogram({ short: 'EL PAÍS' }), monogram({ short: 'HEISE SEC' }), monogram({ short: 'M&G' }),
         monogram({ name: 'Der Spiegel' }), monogram({ short: 'BALKAN INS.' }), monogram({}), monogram({ short: 'ABC AU' })],
}));
"""
    res = subprocess.run([NODE, "--input-type=module", "-e", script, str(JS.as_uri())],
                         capture_output=True, text=True, timeout=30, env={"PATH": ""})
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)
    q = got["q"]
    for query in ("spiegel", "Spiegel", "der spiegel", "derspiegel", "SPIEGEL", '"spiegel"', "spiegel AND"):
        assert q[query] == ["spiegel"], query   # Tagesspiegel nicht: nur Wortanfänge
    assert q["sz"] == ["sz"] and q["faz"] == ["faz"]
    for query in ("süddeutsche", "suddeutsche", "sueddeutsche", "Süddeutsche Zeitung"):
        assert q[query] == ["sz"], query
    assert q["hurriyet"] == ["hurriyet"] and q["huerriyet"] == ["hurriyet"]
    # Eingeschaltete vor ausgeschalteten, dann nach Name
    assert q["local"] == ["thelocalat", "thelocalde", "thelocalch"]
    assert q["times"] == ["japantimes", "taipeitimes"]
    assert q["japan"] == ["japantimes"] and q["taipei"] == ["taipeitimes"]
    assert q["n-tv"] == ["ntv"] and q["ntv"] == ["ntv"]
    assert q["globe mail"] == ["globeandmail"]
    assert q["dw"] == ["dw"]
    assert set(q["the"]) == {"thelocalde", "thelocalat", "thelocalch", "japantimes", "globeandmail"}
    for query in ("taiwan", "trump", "spiegel ukraine", "s", " s ", "", "AND OR", "a*", '"s"', "s.", "ai", "eu"):
        assert q[query] == [], query
    # Unter 3 Zeichen nur genauer Kurzname oder ID: „de“ bringt keinen Block, „sz“/„dw“/„fr“ schon
    assert q["de"] == [] and q["DE"] == []
    assert q["SZ"] == ["sz"] and q["fr"] == ["fr"] and q["n tv"] == ["ntv"]
    # Keine Ausschlüsse (wie die Artikelsuche): „-spiegel“ sucht „spiegel“, NOT fällt weg, jedes Wort muss passen
    assert q["-spiegel"] == ["spiegel"]
    for query in ("spiegel NOT taz", "spiegel -ukraine"):
        assert q[query] == [], query
    # Zerlegte Umlaute (u + Trema) wie fertige
    assert q["Su\u0308ddeutsche"] == ["sz"]
    assert got["nfc"] == ["mm"]   # zerlegtes ü im Namen wird auch zu ue
    # genau (Name) zuerst, dann Liebling, eingeschaltet, erstes Wort, Name
    assert got["rank"] == ["f", "e", "c", "a", "b", "d"]
    # „loc“: Kurzname LOC ist genau
    assert got["rankFav"] == ["f", "e", "c", "a", "b", "d"]
    assert got["none"] == [[], []]
    assert got["mono"] == ["SP", "SZ", "FAZ", "NTV", "LDE", "LAT", "LCH", "TT", "JT", "DW", "HU", "GM", "TA",
                           "FR", "ST", "EP", "HS", "MG", "DS", "BI", "", "ABC"]
    # Gleiche Monogramme in der angezeigten Liste werden dreistellig, sonst bleiben sie
    assert got["dedupe"] == {"tehran": "TET", "taipei": "TAT", "focus": "FOC", "foto": "FOT", "a1": "ABC", "a2": "ABC",
                             "heise": "HEI", "helsinki": "HEL", "spiegel": "SP", "lde": "LDE", "ldk": "LDK",
                             # dreistellig immer noch gleich: Stelle, an der sie sich trennen
                             "tagesschau": "TSC", "tagesspiegel": "TSP", "derstandard": "SAN", "newstatesman": "SAT"}
    assert got["dedupeNone"] == []


def test_api_timeout_and_caller_abort():
    """Hängende Anfrage → nach der Frist ApiError(0, 'Keine Verbindung'); bricht der Aufrufer ab,
    bleibt es ein AbortError (unterscheidbar von der Frist). Die Frist wird nach Erfolg aufgeräumt."""
    script = """
globalThis.location = { origin: 'http://kiosk.test' };
let mode = 'hang';
globalThis.fetch = (url, init) => new Promise((resolve, reject) => {
  if (mode === 'ok') { resolve(new Response('{"ok":1}', { headers: { 'content-type': 'application/json' } })); return; }
  if (mode === 'net') { reject(new TypeError('Failed to fetch')); return; }
  if (init.signal.aborted) { reject(new DOMException('aborted', 'AbortError')); return; }
  init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
});
const { api, ApiError, fetchFailure, deadlineFor, refreshError } = await import(process.argv[1] + '/api.js');
const outcome = async (p) => { try { return ['ok', await p]; } catch (e) {
  return [e instanceof ApiError ? 'ApiError' : e.name, e.status ?? null, e.detail ?? null]; } };
const t0 = Date.now();
const timeout = await outcome(api('x', { timeout: 30 }));
const waited = Date.now() - t0;
const ctrl = new AbortController();
setTimeout(() => ctrl.abort(), 10);
const caller = await outcome(api('x', { signal: ctrl.signal, timeout: 5000 }));
const pre = new AbortController(); pre.abort();
const already = await outcome(api('x', { signal: pre.signal, timeout: 5000 }));
mode = 'net';
const net = await outcome(api('x'));
mode = 'ok';
const ok = await outcome(api('x'));   // Standardfrist 15 s: nach Erfolg aufgeräumt, node endet sofort
const abortErr = new DOMException('a', 'AbortError');
console.log(JSON.stringify({ timeout, waited, caller, already, net, ok,
  deadlines: ['GET', 'HEAD', 'POST', 'PATCH', 'PUT', 'DELETE', undefined].map((m) => deadlineFor(m)),
  map: [fetchFailure(abortErr, false) === abortErr, fetchFailure(abortErr, true) instanceof ApiError,
        fetchFailure(new TypeError('x'), false).detail],
  timedOutFlag: [fetchFailure(abortErr, true).timedOut, fetchFailure(new TypeError('x'), false).timedOut],
  refresh: [refreshError(new ApiError(503, 'Der Sammler ist ausgeschaltet')), refreshError(new ApiError(503)),
            refreshError(new ApiError(502)), refreshError(new ApiError(403, 'Nur Admin'))] }));
"""
    t0 = time.monotonic()
    res = subprocess.run([NODE, "--input-type=module", "-e", script, str(JS.as_uri())],
                         capture_output=True, text=True, timeout=30, env={"PATH": ""})
    elapsed = time.monotonic() - t0
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)
    assert got["timeout"] == ["ApiError", 0, "Keine Verbindung"]
    assert got["timedOutFlag"] == [True, False]
    # 503 mit Meldung des Servers als solche; ohne Meldung (Caddy beim Deploy) „Fehler: HTTP …“
    assert got["refresh"] == ["Der Sammler ist ausgeschaltet", "Fehler: HTTP 503", "Fehler: HTTP 502", "Fehler: Nur Admin"]   # Frist ≠ Netzfehler (Start: nur echter Netzfehler = offline)
    assert 25 <= got["waited"] < 2000
    assert got["caller"][0] == "AbortError"
    assert got["already"][0] == "AbortError"
    assert got["net"] == ["ApiError", 0, "Keine Verbindung"]
    assert got["ok"] == ["ok", {"ok": 1}]
    assert got["map"] == [True, True, "Keine Verbindung"]
    # Schreiben darf länger dauern (der Server könnte es sonst nach dem Abbruch doch tun)
    assert got["deadlines"] == [15000, 15000, 45000, 45000, 45000, 45000, 15000]
    assert elapsed < 10   # keine offene 15-s-Frist nach den Anfragen


def test_build_watcher_reports_new_build_once():
    """Neue Version nach einem Deploy: nur bei echter Änderung (lokal leer: nie), einmal je Version,
    höchstens alle minGapMs (force übergeht das), Fehler bleiben still."""
    script = """
const { buildChanged, buildWatcher } = await import(process.argv[1] + '/version.js');
let t = 0, build = 'a', fail = false;
const seen = [];
const w = buildWatcher({ fetchBuild: async () => { if (fail) throw new Error('offline'); return build; },
  onNew: (b) => seen.push(b), minGapMs: 1000, now: () => t });
w.setKnown('a');
const r = [];
r.push(await w.check());            // zu früh
t = 2000; r.push(await w.check());  // gleiche Version
build = 'b'; r.push(await w.check());   // zu früh (eben gefragt)
r.push(await w.check({ force: true }));  // neu → melden
t = 5000; r.push(await w.check());  // schon gemeldet
fail = true; t = 9000; r.push(await w.check());
fail = false; build = 'c'; t = 12000; r.push(await w.check());
// „Später“: dieselbe neue Version erst nach remindMs wieder melden
const seen3 = [];
let t3 = 0;
const w3 = buildWatcher({ fetchBuild: async () => 'n', onNew: (b) => seen3.push([b, t3]), minGapMs: 0,
  remindMs: 1000, now: () => t3 });
w3.setKnown('o');
const remind = [await w3.check()];
t3 = 500; remind.push(await w3.check());
t3 = 1500; remind.push(await w3.check());
// Start offline: erste Antwort gilt als bekannt
const seen2 = [];
const w2 = buildWatcher({ fetchBuild: async () => 'x', onNew: (b) => seen2.push(b), now: () => 0 });
const first = await w2.check({ force: true });
console.log(JSON.stringify({ r, seen, first, seen2, remind, seen3,
  changed: [buildChanged('a', 'b'), buildChanged('a', 'a'), buildChanged('', 'b'), buildChanged('a', ''),
            buildChanged(null, 'b'), buildChanged('a', undefined)] }));
"""
    res = subprocess.run([NODE, "--input-type=module", "-e", script, str(JS.as_uri())],
                         capture_output=True, text=True, timeout=30, env={"PATH": ""})
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)
    assert got["r"] == [False, False, False, True, False, False, True]
    assert got["seen"] == ["b", "c"]
    assert got["first"] is False and got["seen2"] == []
    assert got["remind"] == [True, False, True] and got["seen3"] == [["n", 0], ["n", 1500]]
    assert got["changed"] == [True, False, False, False, False, False]


def test_feed_does_not_load_ahead_while_hidden():
    """Handy mit offenem Artikel: #view ist ausgeblendet, der Sentinel hat keine Box. Dann nach der
    ersten Seite nicht weiterladen (vorher lud jede fertige Seite die nächste, bis alles da war)."""
    script = """
let rendered = false;
FakeEl.prototype.getClientRects = function () { return rendered ? [{}] : []; };
FakeEl.prototype.getBoundingClientRect = () => ({ top: 10, bottom: 11 });   // wäre „sichtbar“
Object.defineProperty(FakeEl.prototype, 'isConnected', { get() { return true; } });
globalThis.window.innerHeight = 800;
globalThis.IntersectionObserver = class { observe() {} disconnect() {} };
document.createDocumentFragment = () => new FakeEl('fragment');   // genügt hier: Zeilen landen im Behälter
const { createFeed } = await import(process.argv[1] + '/ui/feed.js');
const run = async () => {
  let pages = 0;
  const feed = createFeed({ empty: () => null, load: async (cursor) => { pages += 1;
    return { items: [{ id: pages, published_at: '2026-09-26T10:00:00Z' }], next: pages < 5 ? pages : null }; } });
  await feed.start();
  for (let i = 0; i < 20; i++) await tick();
  const failed = feed.el.querySelector('.feed-error');
  feed.destroy();
  return failed ? 'Fehler' : pages;
};
const hidden = await run();
rendered = true;
const visible = await run();
console.log(JSON.stringify({ hidden, visible }));
"""
    got = _run_fake_dom(script)
    assert got == {"hidden": 1, "visible": 5}


def test_mark_all_read_needs_watermark_and_loaders_ignore_old_user():
    """read-all nur mit Wasserstand (sonst würde alles markiert); späte Antworten des vorigen
    Benutzers (Leiste, Zeitungen, Profile, Zähler) überschreiben den Zustand nach dem Wechsel nicht."""
    script = """
const store = await import(process.argv[1] + '/store.js');
const { markAllRead } = await import(process.argv[1] + '/views/menu.js');
let err = null;
const start = calls.length;
try { await markAllRead({ type: 'all' }, undefined); } catch (e) { err = e.message; }
const before = calls.length;
route = () => ({ marked: 3 });
await markAllRead({ type: 'source', id: 'taz' }, 42);
const readAll = calls.slice(before).find((c) => c[1] === '/api/articles/read-all');
// Späte Antworten nach resetUserState
const gates = {};
route = (path) => { const d = defer(); gates[path] = d; return d.p; };
const pending = [store.loadBar(), store.loadSources(), store.loadProfiles(), store.loadCounts()];
await tick();
store.resetUserState();
gates['/api/bar'].resolve({ items: [{ type: 'all', unread: 9 }] });
gates['/api/sources'].resolve([{ id: 'old' }]);
gates['/api/profiles'].resolve([{ id: 1 }]);
gates['/api/counts'].resolve({ later: 1 });
await Promise.all(pending);
const s = store.state;
let marks = 0;
store.on('watermark', () => { marks += 1; });
store.keepWatermark({ max_id: 9, next_before: 'x' }, null);
const wm1 = [s.listWatermark, s.listComplete];
store.keepWatermark({ max_id: 12, next_before: null }, 'x');   // Folgeseite: Wasserstand bleibt
const wm2 = [s.listWatermark, s.listComplete, marks];   // gemeldet wird erst nach dem Anhängen (ctxPageShown)
console.log(JSON.stringify({ err, callsWithout: before - start, readAll, wm: [wm1, wm2], late: [s.bar, s.sources, s.profiles, s.counts, s.sourceMap.size],
  reset: [s.nextCtx, s.openArticle, s.prevHash] }));
"""
    got = _run_fake_dom(script)
    assert got["err"] == "Liste noch nicht geladen" and got["callsWithout"] == 0
    assert got["readAll"] == ["POST", "/api/articles/read-all", {"source": "taz", "max_id": 42}]
    assert got["late"] == [None, None, None, None, 0]
    assert got["reset"] == [None, None, None]
    assert got["wm"] == [[9, False], [9, True, 0]]


def test_feed_relabels_times_and_day_groups_on_clock():
    """Lange offene App: Nach Mitternacht heißt die Gruppe von gestern GESTERN (nicht mehr HEUTE),
    „5 MIN“ wird zur aktuellen Zeit, und die nächste Seite hängt an die umbenannte Gruppe an."""
    script = """
const RealDate = Date;
let fakeNow = new RealDate(2026, 8, 26, 23, 50).getTime();
globalThis.Date = class extends RealDate {
  constructor(...a) { super(...(a.length ? a : [fakeNow])); }
  static now() { return fakeNow; }
};
let rendered = false;
Object.defineProperty(FakeEl.prototype, 'isConnected', { get() { return true; } });
FakeEl.prototype.getClientRects = () => (rendered ? [{}] : []);
FakeEl.prototype.getBoundingClientRect = () => ({ top: 10, bottom: 11 });
globalThis.window.innerHeight = 800;
globalThis.IntersectionObserver = class { observe() {} disconnect() {} };
let resized = null;
globalThis.ResizeObserver = class { constructor(fn) { resized = fn; } observe() {} disconnect() {} };
// Fragment: Kinder beim Einfügen direkt in den Behälter (wie im Browser)
document.createDocumentFragment = () => { const f = new FakeEl('fragment'); f.isFragment = true; return f; };
const origInsert = FakeEl.prototype.insertBefore;
FakeEl.prototype.insertBefore = function (k, ref) {
  if (k.isFragment) { for (const c of [...k.childNodes]) origInsert.call(this, c, ref); return k; }
  return origInsert.call(this, k, ref);
};
const store = await import(process.argv[1] + '/store.js');
const { createFeed } = await import(process.argv[1] + '/ui/feed.js');
const at = (d, h, m) => new RealDate(2026, 8, d, h, m).toISOString();
const pages = [
  [{ id: 1, published_at: at(26, 23, 45), country: 'DE' }, { id: 2, published_at: at(26, 9, 0), country: 'DE' }],
  [{ id: 3, published_at: at(26, 8, 0), country: 'DE' }, { id: 4, published_at: at(25, 8, 0), country: 'DE' }],
];
let page = 0;
const feed = createFeed({ dayGroups: true, empty: () => null,
  load: async () => ({ items: pages[page++], next: page < pages.length ? page : null }) });
await feed.start();
const read = () => feed.el.children.filter((n) => n.cls().includes('day') || n.cls().includes('row'))
  .map((n) => n.cls().includes('day') ? n.textContent : n.querySelector('.row-when').textContent);
const before = read();
fakeNow = new RealDate(2026, 8, 27, 0, 10).getTime();
store.emit('clock');
const after = read();
rendered = true;   // Liste wieder zu sehen: nächste Seite
resized();
for (let i = 0; i < 10; i++) await tick();
console.log(JSON.stringify({ before, after, next: read() }));
"""
    got = _run_fake_dom(script)
    assert got["before"] == ["HEUTE", "/ DE / 5 MIN", "/ DE / 14 STD"]
    assert got["after"] == ["GESTERN", "/ DE / 25 MIN", "/ DE / 15 STD"]
    assert got["next"] == ["GESTERN", "/ DE / 25 MIN", "/ DE / 15 STD", "/ DE / 16 STD",
                           "25. SEP", "/ DE / 25. SEP"]


def test_heise_and_spiegel_image_variants_count_as_same_image():
    """Größenvarianten mit Hash-Endung (heise, auch über den Proxy cloudimg.io und .jpg/.jpeg) gelten im
    Browser als dasselbe Bild, damit die Kopie des Titelbilds im Text entfällt; reine Zahlen-/Datums-
    Endungen und andere Namen nicht. urlWidth liest die Breite aus der URL."""
    script = """
const fmt = await import(process.argv[1] + '/format.js');
const H = 'https://www.heise.de/imgs/18/4/9/0/2/7/1/5/';
const C = 'https://heise.cloudimg.io/width/610/q85.png-lossy-85.webp-lossy-85.foil1/_www-heise-de_/imgs/18/4/9/0/2/7/1/5/';
const pairs = {
  hash: [H + 'Daimler_E-lkw-86ffab972e33a670.jpg', H + 'Daimler_E-lkw-a55055d4fc8ac0e5.jpeg'],
  proxy: [H + 'Daimler_E-lkw-86ffab972e33a670.jpg', C + 'Daimler_E-lkw-a55055d4fc8ac0e5.jpeg'],
  proxyQuery: [H + 'Daimler_E-lkw-86ffab972e33a670.jpg', C + 'Daimler_E-lkw-a55055d4fc8ac0e5.jpeg?force_format=avif'],
  otherStem: [H + 'Daimler_E-lkw-86ffab972e33a670.jpg', H + 'Daimler_Bus-a55055d4fc8ac0e5.jpg'],
  digitsOnly: [H + 'Bahnstreik-202609281200.jpg', H + 'Bahnstreik-202609281215.jpg'],
  otherSite: [H + 'Daimler_E-lkw-86ffab972e33a670.jpg', 'https://www.golem.de/imgs/18/4/9/0/2/7/1/5/Daimler_E-lkw-a55055d4fc8ac0e5.jpg'],
  otherIdFolder: ['https://www.heise.de/imgs/18/4/9/2/6/1/1/5/Tesla-Model-Y-86ffab972e33a670.jpeg',
                  'https://www.heise.de/imgs/18/4/9/3/0/7/2/2/Tesla-Model-Y-a55055d4fc8ac0e5.jpeg'],
  // gleicher Name mit Hash in Größen-Ordnern (Ziffern im ganzen Namen): dasselbe Bild
  sizeFolders: ['https://cdn.x.de/800/Daimler_E-lkw-86ffab972e33a670.jpg', 'https://cdn.x.de/400/Daimler_E-lkw-86ffab972e33a670.jpg'],
  // kurzer Rest ohne Hash (pic-<hash>): gleicher Name bleibt unscharf gleich, anderer Hash nicht
  shortSame: ['https://cdn.x.de/800/pic-86ffab972e33a670.jpg', 'https://cdn.x.de/400/pic-86ffab972e33a670.jpg'],
  shortOther: ['https://cdn.x.de/imgs/1/2/3/pic-86ffab972e33a670.jpg', 'https://cdn.x.de/imgs/1/2/3/pic-a55055d4fc8ac0e5.jpg'],
  flatCms: ['https://cms.example/uploads/article-image-86ffab972e33a670.jpg',
            'https://cms.example/uploads/article-image-a55055d4fc8ac0e5.jpg'],
  wpScreenshot: ['https://blog.example/wp-content/uploads/2026/09/screenshot-a1b2c3d4e5f6.png',
                 'https://blog.example/wp-content/uploads/2026/09/screenshot-9f8e7d6c5b4a.png'],
  spiegel: ['https://cdn.prod.www.spiegel.de/images/0f9a3c1e-0001-0002-0000-000000000001_w520_r1.77_fpx50_fpy40.jpg',
            'https://cdn.prod.www.spiegel.de/images/0f9a3c1e-0001-0002-0000-000000000001_w960_r1.77_fpx50_fpy40.jpg'],
};
const same = Object.fromEntries(Object.entries(pairs).map(([k, [a, b]]) => [k, [fmt.sameImage(a, b), fmt.sameImage(b, a)]]));
const seen = fmt.imageSeen();
const seenRun = [pairs.proxy[0], pairs.proxy[1], pairs.otherStem[1]].map((u) => seen(u));
console.log(JSON.stringify({ same, seenRun,
  width: [fmt.urlWidth(pairs.spiegel[0]), fmt.urlWidth(pairs.spiegel[1]), fmt.urlWidth('https://x.example/a.jpg?width=800&q=1'),
          fmt.urlWidth('https://x.example/a.jpg?w=640'), fmt.urlWidth('https://x.example/2026/a.jpg'), fmt.urlWidth(null),
          fmt.urlWidth(C + 'Daimler_E-lkw-a55055d4fc8ac0e5.jpeg'), fmt.urlWidth('https://taz.de/picture/8763231/14/bild.webp'),
          fmt.urlWidth('https://taz.de/picture/8763231/948/bild.jpeg'), fmt.urlWidth('https://x.example/picture/2026/09/bild.jpg'),
          fmt.urlWidth('https://x.example/foto_w2026_r1.jpg'), fmt.urlWidth('https://x.example/foto_w1999.jpg'),
          fmt.urlWidth('https://x.example/foto_w1200.jpg')] }));
"""
    res = subprocess.run([NODE, "--input-type=module", "-e", script, str(JS.as_uri())],
                         capture_output=True, text=True, timeout=30, env={"PATH": ""})
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)
    assert got["same"] == {"hash": [True, True], "proxy": [True, True], "proxyQuery": [True, True],
                           "otherStem": [False, False], "digitsOnly": [False, False],
                           # gleicher Name, andere Bild-ID-Ordner bzw. kurze WordPress-Endung: andere Bilder
                           "otherIdFolder": [False, False], "wpScreenshot": [False, False],
                           "flatCms": [False, False],   # ohne Bild-ID-Ordner: Hash trennt Bilder
                           "sizeFolders": [True, True], "shortSame": [True, True], "shortOther": [False, False],
                           "otherSite": [False, False], "spiegel": [True, True]}
    assert got["seenRun"] == [False, True, False]
    # Datumsordner und Jahreszahlen (_w2026) sind keine Breite
    assert got["width"] == [520, 960, 800, 640, 0, 0, 610, 14, 948, 0, 0, 0, 1200]


def test_caption_credit_title_echo_and_hero_source():
    """Leseansicht: „Bild:“ und „(Bild: …)“ gelten als Bildquelle; eine Überschrift, die mit dem ganzen
    Titel endet (Spiegel-Dachzeile), ist eine Titel-Wiederholung; das Titelbild nimmt eine breitere
    Fassung aus dem Text statt des kleineren Feed-Bilds."""
    script = """
const a = await import(process.argv[1] + '/views/article.js');
const title = 'Evonik schlägt BASF-Übernahmeangebot aus';
const W = (w) => `https://cdn.prod.www.spiegel.de/images/abc-def_w${w}_r1.77_fpx50_fpy40.jpg`;
console.log(JSON.stringify({
  credit: ['Bild: Daimler Truck', '(Bild: Daimler Truck)', 'Bilder: dpa', 'Foto: AP', 'Quelle: heise',
           'Bildung ist wichtig', 'Der Bildband: schön', 'Fotograf Max'].map((t) => a.isCreditLine(t)),
  separate: ['(Bild: Daimler Truck)', 'Foto: AP', 'Photo: Reuters', '© dpa', '(© Hersteller)', 'Quelle: Destatis',
             '(Quelle: Destatis)', '(dpa) Berlin', 'Bildung ist wichtig'].map((t) => a.isSeparateCredit(t)),
  echo: [a.headingEchoesTitle('Verhandlungen in der Chemiebranche Evonik schlägt BASF-Übernahmeangebot aus', title),
         a.headingEchoesTitle(title, title),
         a.headingEchoesTitle('Evonik schlägt BASF-Übernahmeangebot aus – und was jetzt aus den Tarifverhandlungen der Chemie wird', title),
         a.headingEchoesTitle('Was Evonik jetzt vorhat', title),
         a.headingEchoesTitle('Eine sehr lange Überschrift, die mit einem ganz anderen Thema beginnt und viel zu viel davor hat, bevor ' + title, title)],
  hero: [a.heroSource(W(520), [W(960)]), a.heroSource(W(960), [W(520)]), a.heroSource(W(520), []),
         a.heroSource('https://x.example/feed.jpg', [W(960)]), a.heroSource('https://x.example/a/240/b.jpg', ['https://x.example/big.jpg'])],
}));
"""
    got = _run_fake_dom(script)
    assert got["credit"] == [True, True, True, True, True, False, False, False]
    # Eigener Bildquellen-Absatz nach dem Bildtext: nur Bildquellen, nicht „Quelle:“ (Grafik)
    assert got["separate"] == [True, True, True, True, True, False, False, False, False]
    assert got["echo"] == [True, True, False, False, False]
    assert got["hero"] == [
        "https://cdn.prod.www.spiegel.de/images/abc-def_w960_r1.77_fpx50_fpy40.jpg",
        "https://cdn.prod.www.spiegel.de/images/abc-def_w960_r1.77_fpx50_fpy40.jpg",
        "https://cdn.prod.www.spiegel.de/images/abc-def_w520_r1.77_fpx50_fpy40.jpg",
        "https://x.example/feed.jpg",   # Breite unbekannt: Feed-Bild bleibt
        "https://x.example/big.jpg",    # Feed nur Vorschau (/240/): große Fassung
    ]


def test_watermark_is_reported_after_the_page_is_in_list_items():
    """Kurze (einseitige) Liste: 'watermark' kommt erst, wenn die Artikel in listItems stehen; sonst
    hielte das offene Menü die Liste für „nichts ungelesen“ und blendete read-all aus."""
    script = """
Object.defineProperty(FakeEl.prototype, 'isConnected', { get() { return true; } });
FakeEl.prototype.getClientRects = () => [];
globalThis.IntersectionObserver = class { observe() {} disconnect() {} };
document.createDocumentFragment = () => new FakeEl('fragment');
const store = await import(process.argv[1] + '/store.js');
const { articlesLoader, createFeed } = await import(process.argv[1] + '/ui/feed.js');
route = () => ({ items: [{ id: 1, read: 0, published_at: '2026-09-26T10:00:00Z' }], next_before: null, max_id: 7 });
const seen = [];
store.on('watermark', () => seen.push([store.state.listItems.length, store.state.listWatermark, store.state.listComplete]));
const feed = createFeed({ load: articlesLoader({}, { onResponse: store.keepWatermark }), onPage: store.ctxPageShown,
  empty: () => null });
store.beginCtxList(feed.items);
await feed.start();
console.log(JSON.stringify({ seen }));
"""
    got = _run_fake_dom(script)
    # beginCtxList (leer, ohne Wasserstand), dann nach dem Anhängen: 1 Artikel, Wasserstand, ganz geladen
    assert got["seen"] == [[0, None, False], [1, 7, True]]


def test_update_pill_keeps_label_across_quick_layout_changes():
    """Die Beschriftung „Neue Version“ kommt auch, wenn die Pille kurz nach dem Zeigen erneut
    abgeglichen wird (Layoutwechsel innerhalb von 100 ms); verborgen ist sie leer."""
    script = """
const { showUpdatePill, setUpdatePillAllowed } = await import(process.argv[1] + '/ui/update.js');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
setUpdatePillAllowed(true);
showUpdatePill(() => {});
await wait(40);
setUpdatePillAllowed(true);   // Layoutwechsel
await wait(40);
setUpdatePillAllowed(true);
await wait(150);
const pill = document.getElementById('shell').children.find((c) => c.cls().includes('update-pill'));
const text = () => pill.querySelector('.update-text').textContent;
const shown = [pill.hidden, text(), document.body.classList.contains('has-update')];
setUpdatePillAllowed(false);   // Anmeldung
const hidden = [pill.hidden, text(), document.body.classList.contains('has-update')];
setUpdatePillAllowed(true);
await wait(150);
console.log(JSON.stringify({ shown, hidden, again: [pill.hidden, text()] }));
"""
    got = _run_fake_dom(script)
    assert got["shown"] == [False, "Neue Version", True]
    assert got["hidden"] == [True, "", False]
    assert got["again"] == [False, "Neue Version"]


def test_live_blog_time_lines_and_lead_caption_rule():
    """Liveblog: eine fett gesetzte Uhrzeit-Zeile ist kein Rubrik-Absatz (sie und die Überschrift danach
    bleiben stehen). Bildzeile nach dem Titelbild nach derselben Regel wie das Backend (_image_end)."""
    script = """
const a = await import(process.argv[1] + '/views/article.js');
const { h } = await import(process.argv[1] + '/ui/dom.js');
const time = (t, tag = 'strong') => h('p', {}, h(tag, { text: t }));
console.log(JSON.stringify({
  time: [a.isTimeLine(time('28.09. 14:43')), a.isTimeLine(time('14:43')), a.isTimeLine(time('28.09.2026 9:05 Uhr', 'b')),
         a.isTimeLine(h('p', { text: '14:43' })), a.isTimeLine(h('p', {}, h('strong', { text: '14:43' }), ' Uhr: neu')),
         a.isTimeLine(time('Wichtig')), a.isTimeLine(null)],
  lead: ['Präsident Biden in Washington', 'Foto: AP', '(Bild: Tesla)', '© dpa', 'Der Präsident sprach lange.',
         'Was sagte er? ', '„Wir schaffen das“', 'x'.repeat(251), ''].map((t) => a.isLeadCaptionText(t)),
  // Wie _plain_caption: keine Bildzeile bei Doppelpunkt, Auslassung, Klammer, Ortsmarke, „+++“ oder
  // wenn der nächste Absatz genauso aussieht (Gedicht, Aufzählung).
  guards: [a.isLeadCaptionText('Die wichtigsten Punkte:'), a.isLeadCaptionText('Und dann kam alles anders …'),
           a.isLeadCaptionText('Merz in Berlin (Archiv)'), a.isLeadCaptionText('Berlin (dpa) – Die Regierung plant neue Regeln'),
           a.isLeadCaptionText('München (dpa) – Der Stadtrat plant neue Regeln'),
           a.isLeadCaptionText('Streik bei der Bahn +++ Merz in Paris +++ Wetter'),
           a.isLeadCaptionText('Rosen sind rot', 'Veilchen sind blau'),
           a.isLeadCaptionText('Rosen sind rot', 'Der Text beginnt hier mit einem Satz.'),
           a.isLeadCaptionText('Rosen sind rot', 'Foto: dpa')],
}));
"""
    got = _run_fake_dom(script)
    assert got["time"] == [True, True, True, False, False, False, False]
    assert got["lead"] == [True, True, True, True, False, False, False, False, False]   # Zitat: keine Bildzeile
    assert got["guards"] == [False, False, False, False, False, False, False, True, True]


def test_enable_source_updates_current_source_object():
    """„Einschalten“ in einer ausgeschalteten Zeitung: Wurde /api/sources inzwischen neu geladen, ist das
    Objekt der Ansicht veraltet; auch das aktuelle in state.sourceMap muss eingeschaltet werden, sonst
    zeigt die Ansicht weiter AUSGESCHALTET."""
    script = """
Object.defineProperty(FakeEl.prototype, 'isConnected', { get() { return true; } });
FakeEl.prototype.getClientRects = () => [];
globalThis.IntersectionObserver = class { observe() {} disconnect() {} };
document.createDocumentFragment = () => new FakeEl('fragment');
globalThis.document.body.classList.add = () => {};
const store = await import(process.argv[1] + '/store.js');
const { sourceView } = await import(process.argv[1] + '/views/sourceView.js');
store.state.user = { id: 1, is_admin: true };
const off = () => ({ id: 'taz', name: 'taz', country: 'DE', enabled: false, total: 5, unread: 0, unread_all: 0 });
store.state.sources = [off()];
store.state.sourceMap = new Map(store.state.sources.map((s) => [s.id, s]));
route = (path, method) => {
  if (path === '/api/sources') return [off()];   // neu geladen: neue Objekte, noch aus
  if (path === '/api/sources/taz' && method === 'PATCH') return { ...off(), enabled: true };
  if (path === '/api/refresh') return {};
  if (path === '/api/bar') return { items: [] };
  if (path === '/api/status') return {};
  return { items: [], next_before: null, max_id: 3 };
};
const view = sourceView({ id: 'taz', params: new URLSearchParams() });
for (let i = 0; i < 10; i++) await tick();
const btn = view.el.all().find((e) => e.tagName === 'BUTTON' && e.textContent === 'Einschalten');
const before = !!btn;
await store.loadSources();   // z. B. Zähler nachgeladen: sourceMap hat neue Objekte, die Ansicht hält das alte
await btn.listeners.click[0]();
for (let i = 0; i < 10; i++) await tick();
const titles = view.el.all().filter((e) => e.cls().includes('t-section')).map((e) => e.textContent);
console.log(JSON.stringify({ before, enabled: store.state.sourceMap.get('taz').enabled, off: titles.includes('AUSGESCHALTET') }));
view.destroy();
"""
    got = _run_fake_dom(script)
    assert got == {"before": True, "enabled": True, "off": False}


# Schein-DOM-Erweiterung für die Leseansicht: Tag-Selektoren, closest(Tag), previousElementSibling.
ARTICLE_DOM = """
const origQSA = FakeEl.prototype.querySelectorAll;
FakeEl.prototype.querySelectorAll = function (sel) {
  if (/^[a-z]+$/i.test(sel)) return this.all().filter((e) => e.tagName === sel.toUpperCase());
  return origQSA.call(this, sel);
};
FakeEl.prototype.closest = function (sel) {
  for (let x = this; x; x = x.parentNode) if (x.tagName === String(sel).toUpperCase()) return x;
  return null;
};
Object.defineProperty(FakeEl.prototype, 'previousElementSibling', { get() {
  const p = this.parentNode; if (!p) return null; const c = p.children; return c[c.indexOf(this) - 1] || null; } });
const a = await import(process.argv[1] + '/views/article.js');
const { h } = await import(process.argv[1] + '/ui/dom.js');
const cls = (el) => el.className;
"""


def test_plan_hero_lead_image_captions_and_placeholders():
    """planHero/adoptLeadImage im Schein-DOM: Bildzeile als <figcaption> (in <figure> oder lose daneben)
    bleibt allein; ohne sie nur der erste passende Absatz (nicht Liveblog-Uhrzeit, nicht Vorspann mit
    „+++“, nicht „Wie die Bild berichtet“); taz-Platzhalter (14 px) wird durch das Feed-Bild ersetzt,
    ebenso ein Bild unbekannter Breite, das winzig lädt."""
    script = ARTICLE_DOM + """
const T = 'https://taz.de/picture/8763231/';
const out = {};
// 1. <figure><img><figcaption>
{ const img = h('img', { src: 'https://x.example/a/bild-gross.jpg' });
  const body = h('div', {}, h('figure', {}, img, h('figcaption', { text: 'Bildtext · Foto: dpa' })),
    h('p', { text: 'Kurzer Absatz ohne Punkt' }), h('p', { text: 'Text beginnt.' }));
  out.figure = [a.planHero(body, null, ''), body.children.map(cls)]; }
// 2. lose <img><figcaption> nebeneinander
{ const body = h('div', {}, h('p', {}, h('img', { src: 'https://x.example/a/bild-gross.jpg' })),
    h('figcaption', { text: 'Bildtext' }), h('p', { text: 'Kurz ohne Punkt' }), h('p', { text: 'Satz.' }));
  a.planHero(body, null, ''); out.loose = body.children.map(cls); }
// 3. Liveblog: Uhrzeit direkt nach dem Bild ist keine Bildzeile
{ const body = h('div', {}, h('p', {}, h('img', { src: 'https://x.example/a/bild-gross.jpg' })),
    h('p', {}, h('strong', { text: '28.09. 14:43' })), h('h2', { text: 'Merz in Paris' }), h('p', { text: 'Satz.' }));
  a.planHero(body, null, ''); out.live = body.children.map(cls); }
// 4. BBC: kurzer Bildtext ohne Punkt, danach Bildquelle mit Präfix; „Wie die Bild berichtet“ nicht
{ const body = h('div', {}, h('p', {}, h('img', { src: 'https://x.example/a/bild-gross.jpg' })),
    h('p', { text: 'Joe Biden in Washington' }), h('p', { text: 'Foto: Reuters' }), h('p', { text: 'Satz.' }));
  a.planHero(body, null, ''); out.bbc = body.children.map(cls); }
{ const body = h('div', {}, h('p', {}, h('img', { src: 'https://x.example/a/bild-gross.jpg' })),
    h('p', { text: 'Joe Biden in Washington' }), h('p', { text: 'Wie die Bild berichtet, plant Merz eine Reise.' }),
    h('p', { text: 'Satz.' }));
  a.planHero(body, null, ''); out.bild = body.children.map(cls); }
// 5. FAZ-Vorspann mit +++
{ const body = h('div', {}, h('p', {}, h('img', { src: 'https://x.example/a/bild-gross.jpg' })),
    h('p', { text: 'Streik bei der Bahn +++ Merz in Paris +++ Wetter' }), h('p', { text: 'Satz.' }));
  a.planHero(body, null, ''); out.faz = body.children.map(cls); }
// 6. taz-Platzhalter 14 px vorn, Feed-Bild 948 px: Feed-Bild an seine Stelle, figcaption bleibt
{ const img = h('img', { src: T + '14/taz-bild.webp' });
  const body = h('div', {}, h('figure', {}, img, h('figcaption', { text: 'FOTO: Max Muster' })), h('p', { text: 'Satz.' }));
  const plan = a.planHero(body, T + '948/taz-bild.jpeg', '');
  out.taz = [plan, img.getAttribute('src'), img.dataset.fallbackSrc || null, body.children[0].children.map((c) => c.tagName)]; }
// 6b. echtes kleines Bild (200 px laut URL) mit anderem Feed-Bild: bleibt samt Bildzeile
{ const img = h('img', { src: 'https://x.example/a/portraet_w200.jpg' });
  const body = h('div', {}, h('figure', {}, img, h('figcaption', { text: 'Anna Muster' })), h('p', { text: 'Satz.' }));
  a.planHero(body, 'https://x.example/feed/anders_w1200.jpg', '');
  out.smallReal = img.getAttribute('src'); }
// 7. Breite unbekannt, lädt winzig: Feed-Bild; lädt groß: bleibt
for (const [key, nw] of [['tinyLoad', 14], ['bigLoad', 1200], ['smallLoad', 200]]) {
  const img = h('img', { src: 'https://x.example/a/bild-ohne-breite.jpg' });
  const body = h('div', {}, h('figure', {}, img), h('p', { text: 'Satz.' }));
  a.planHero(body, 'https://x.example/feed/anders.jpg', '');
  const flagged = img.dataset.leadCheck === '1';
  img.naturalWidth = nw;
  const before = (img.listeners.load || []).length;
  for (const fn of [...(img.listeners.load || [])]) fn();
  out[key] = [flagged, img.getAttribute('src'), img.dataset.leadCheck || null, img.dataset.fallbackSrc || null,
              (img.listeners.load || []).length - before];
}
console.log(JSON.stringify(out));
"""
    got = _run_fake_dom(script)
    assert got["figure"] == [None, ["", "", ""]]            # figcaption allein, Absätze bleiben Text
    assert got["loose"] == ["", "", "", ""]
    assert got["live"] == ["", "", "", ""]                  # Uhrzeit keine Bildzeile
    assert got["bbc"] == ["", "art-caption", "art-caption", ""]
    assert got["bild"] == ["", "art-caption", "", ""]     # zweiter Absatz nur mit Präfix-Bildquelle
    assert got["faz"] == ["", "", ""]
    # Platzhalter: Feed-Bild, bei Ladefehler zurück zum Original
    assert got["taz"] == [None, "https://taz.de/picture/8763231/948/taz-bild.jpeg",
                          "https://taz.de/picture/8763231/14/taz-bild.webp", ["IMG", "FIGCAPTION"]]
    assert got["smallReal"] == "https://x.example/a/portraet_w200.jpg"
    # winzig geladen: getauscht, mit Rückfall und neuer Winzig-Prüfung für das eingesetzte Bild
    assert got["tinyLoad"] == [True, "https://x.example/feed/anders.jpg", None,
                               "https://x.example/a/bild-ohne-breite.jpg", 1]
    assert got["bigLoad"] == [True, "https://x.example/a/bild-ohne-breite.jpg", None, None, 0]
    assert got["smallLoad"] == [True, "https://x.example/a/bild-ohne-breite.jpg", None, None, 0]   # echtes kleines Bild


def test_toast_wrap_rule_and_sheet_dispose():
    """Toast: kurze Bestätigung einzeilig, Fehler und längere Hinweise (über 28 Zeichen) zweizeilig.
    Blatt: onDispose kommt bei jedem Schließen (auch still und beim Ersetzen durch ein anderes Blatt),
    damit z. B. das Menü seinen 'watermark'-Beobachter abmeldet."""
    script = """
ids.toast = new FakeEl('div');
ids.sheet = new FakeEl('div');
document.contains = () => false;
const { toast } = await import(process.argv[1] + '/ui/dom.js');
const { openSheet, closeSheet } = await import(process.argv[1] + '/ui/sheet.js');
const wrap = (m) => { toast(m); return [ids.toast.cls().includes('toast--wrap'), ids.toast.textContent]; };
const toasts = [wrap('Alles gelesen'), wrap('Nur der Admin kann Zeitungen einschalten'), wrap('Fehler: kaputt')];
const log = [];
openSheet(() => {}, { onDispose: () => log.push('a-dispose'), onClose: () => log.push('a-close') });
openSheet(() => {}, { onDispose: () => log.push('b-dispose'), onClose: () => log.push('b-close') });   // ersetzt a still
closeSheet({ silent: true });
openSheet(() => {}, { onDispose: () => log.push('c-dispose'), onClose: () => log.push('c-close') });
closeSheet();
console.log(JSON.stringify({ toasts, log }));
"""
    got = _run_fake_dom(script)
    assert got["toasts"] == [[False, "Alles gelesen"], [True, "Nur der Admin kann Zeitungen einschalten"],
                             [True, "Fehler: kaputt"]]
    assert got["log"] == ["a-dispose", "b-dispose", "c-dispose", "c-close"]
