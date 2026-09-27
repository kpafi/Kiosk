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
globalThis.window = { matchMedia: () => ({ get matches() { return desktop; }, addEventListener() {} }) };
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
