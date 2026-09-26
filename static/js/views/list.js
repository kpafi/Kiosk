// Liste (E2Liste, E2Leer): Kopf, Filterchips, Tagesgruppen, Lade-Pille, Leerzustand.
import { h, replace, toast } from '../ui/dom.js';
import { dots, pencil, search } from '../ui/icons.js';
import { iconButton } from '../ui/header.js';
import { chip } from '../ui/chips.js';
import { articlesLoader, createFeed } from '../ui/feed.js';
import { emptyState, nextRunSentence } from '../ui/empty.js';
import { openMenu } from './menu.js';
import {
  ctxParams, isAdmin, keepWatermark, loadBar, loadStatus, on, profileById, replaceHash, setUnreadFilter, state,
} from '../store.js';
import { api } from '../api.js';

/** Pille „HOLE NEUE ARTIKEL · 25 ZEITUNGEN“, solange ein Sammellauf läuft. */
export function statusPill() {
  const st = state.status;
  if (!st || !st.fetcher || !st.fetcher.running) return null;
  const n = st.settings ? st.settings.sources_enabled : null;
  return h('div', { class: 'status-pill t-caption', role: 'status' },
    h('span', { class: 'load-dots', 'aria-hidden': 'true' }, h('span'), h('span'), h('span')),
    h('span', { text: n ? `Hole neue Artikel · ${n} Zeitungen` : 'Hole neue Artikel' }));
}

// Filterchips: Ein gewählter Profil-Chip rückt an Stelle 2 direkt hinter „Ungelesen“. Die Liste wird
// beim Profilwechsel neu aufgebaut; deshalb merkt sich der Tipp Lage und Farben aller Chips, und die
// neue Zeile gleitet von dort an ihren Platz (FLIP, 200 ms, bei „weniger Bewegung“ ohne Animation).
// Der „Ungelesen“-Chip wechselt seine Farben mit derselben Überblendung.
const CHIP_MS = 200;
// { at: Zeitpunkt, hash: Ziel, focus: Schlüssel des fokussierten Chips, map: Map<Schlüssel, {left, bg, fg, border}> }
let chipsBefore = null;

function rememberChips(row, hash) {
  const map = new Map();
  for (const el of row.querySelectorAll('.chip[data-key]')) {
    const cs = getComputedStyle(el);
    map.set(el.dataset.key, {
      left: el.getBoundingClientRect().left, bg: cs.backgroundColor, fg: cs.color, border: cs.borderColor,
    });
  }
  const focused = row.contains(document.activeElement) ? document.activeElement.closest('.chip[data-key]') : null;
  chipsBefore = { at: Date.now(), hash, focus: focused ? focused.dataset.key : null, map };
}

/** Klick, der die Seite nicht selbst wechselt (neuer Tab/Fenster, Mittelklick): nichts merken. */
const isModified = (e) => e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey;

function animateChips(row) {
  if (!row.isConnected) return;   // Ansicht schon wieder ersetzt
  const before = chipsBefore;
  chipsBefore = null;
  if (!before || Date.now() - before.at > 2000 || before.hash !== (location.hash || '#/')) return;
  const focusChip = before.focus ? row.querySelector(`.chip[data-key="${before.focus}"]`) : null;
  const restoreFocus = () => { if (focusChip && focusChip.isConnected) focusChip.focus({ preventScroll: true }); };
  // Fokus sofort zurückgeben (vor den Animationen), damit Tastatur und Vorleser nicht warten.
  restoreFocus();
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  // Durchsichtiger Chip-Hintergrund wird in der Animation deckend (Seitenfarbe --bg), sonst scheinen
  // sich kreuzende Chips durch.
  const pageBg = getComputedStyle(document.body).backgroundColor;
  const solid = (c) => (c === 'transparent' || /^rgba\(.*,\s*0\)$/.test(c) ? pageBg : c);
  for (const el of row.querySelectorAll('.chip[data-key]')) {
    const old = before.map.get(el.dataset.key);
    if (!old) continue;
    const cs = getComputedStyle(el);
    const dx = old.left - el.getBoundingClientRect().left;
    const recolor = old.bg !== cs.backgroundColor || old.fg !== cs.color;
    if (Math.abs(dx) < 1 && !recolor) continue;
    const from = { offset: 0, transform: `translateX(${dx}px)` };
    const to = { offset: 1, transform: 'none' };
    const frames = [from, to];
    if (recolor) {
      Object.assign(from, { backgroundColor: solid(old.bg), borderColor: old.border, color: old.fg });
      Object.assign(to, { backgroundColor: solid(cs.backgroundColor), borderColor: cs.borderColor, color: cs.color });
      // Schrift wechselt in der Mitte, wenn der Hintergrund halb überblendet ist (sonst kurz unlesbar).
      frames.splice(1, 0, { offset: 0.5, color: old.fg }, { offset: 0.5, color: cs.color });
    }
    // Aktiver und eben abgewählter Chip (Farbwechsel) gleiten über die anderen hinweg.
    if (el.classList.contains('is-on') || recolor) Object.assign(el.style, { position: 'relative', zIndex: '1' });
    const anim = el.animate(frames, { duration: CHIP_MS, easing: 'ease-out' });
    anim.finished.catch(() => {}).then(() => { el.style.position = ''; el.style.zIndex = ''; });
  }
}

export function listView(route) {
  const pid = route.params.get('p');
  const profile = pid ? profileById(pid) : null;
  state.ctx = pid ? { type: 'profile', id: Number(pid) || pid } : { type: 'all' };
  state.nextCtx = ctxParams();

  // Profil gibt es nicht (mehr): zurück zu ALLE. Erst nach dem Aufbau umleiten (nicht mitten im Routing).
  let gone = false;
  function profileGone() {
    if (gone) return;
    gone = true;
    setTimeout(() => { toast('Profil gibt es nicht mehr'); replaceHash('#/'); }, 0);
  }
  if (pid && state.profiles && !profile) profileGone();

  const chipsEl = h('div', { class: 'chips', role: 'toolbar', 'aria-label': 'Filter' });
  const statusSlot = h('div', { class: 'status-slot' });
  const makeFeed = () => createFeed({
    dayGroups: true,
    load: withGoneCheck(articlesLoader({ ...ctxParams(), unread: state.unread ? 'true' : undefined },
      { onResponse: keepWatermark })),
    empty: buildEmpty,
  });
  function withGoneCheck(load) {
    return async (cursor) => {
      try {
        return await load(cursor);
      } catch (err) {
        if (pid && err.status === 404) { profileGone(); return { items: [], next: null }; }
        throw err;
      }
    };
  }
  let feed = makeFeed();
  state.listItems = feed.items;
  state.listIsCtx = true;

  function buildChips() {
    const profiles = state.profiles || [];
    const isActive = (p) => state.ctx.type === 'profile' && String(p.id) === String(state.ctx.id);
    // Aktives Profil an Stelle 2, die übrigen in ihrer gespeicherten Reihenfolge.
    const ordered = [...profiles.filter(isActive), ...profiles.filter((p) => !isActive(p))];
    const unread = chip('Ungelesen', {
      on: state.unread,
      onClick: () => {
        setUnreadFilter(!state.unread);
        rebuildFeed();
        rememberChips(chipsEl, location.hash || '#/');
        buildChips();
        animateChips(chipsEl);
      },
    });
    unread.dataset.key = 'u';
    replace(chipsEl,
      unread,
      ordered.map((p) => {
        const active = isActive(p);
        const el = chip(p.name, { on: active, href: active ? '#/' : `#/?p=${p.id}` });
        el.dataset.key = `p${p.id}`;
        el.addEventListener('click', (e) => { if (!isModified(e)) rememberChips(chipsEl, el.getAttribute('href')); });
        return el;
      }),
      iconButton(pencil(16), 'Profile bearbeiten', { href: '#/profile', small: true }));
  }

  function rebuildFeed() {
    feed.destroy();
    feed = makeFeed();
    feedHolder.replaceChildren(feed.el);
    state.listItems = feed.items;
    state.listIsCtx = true;
    feed.start();
  }

  function buildEmpty() {
    const name = profile ? profile.name : null;
    let text;
    if (state.unread) text = name ? `Im Profil „${name}“ ist alles gelesen.` : 'Alles gelesen.';
    else text = name ? `Im Profil „${name}“ gibt es noch keine Artikel.` : 'Noch keine Artikel da.';
    const next = nextRunSentence();
    if (next) text += ` ${next}`;
    const actions = [];
    if (state.unread) {
      actions.push(h('button', {
        type: 'button', class: 'pill', text: 'Alle Artikel zeigen',
        on: { click: () => { setUnreadFilter(false); buildChips(); rebuildFeed(); } },
      }));
    }
    if (profile) actions.push(h('a', { class: 'pill', href: `#/profile/${profile.id}`, text: 'Profil erweitern' }));
    else if (isAdmin()) {
      // Sammellauf starten darf nur der Admin.
      actions.push(h('button', {
        type: 'button', class: 'pill', text: 'Jetzt abrufen',
        on: { click: async () => { try { await api('refresh', { method: 'POST' }); } catch { /* läuft schon */ } poll(); } },
      }));
    }
    return emptyState({ text, actions });
  }

  // Sammellauf beobachten: Pille zeigen, nach Ende Liste (falls oben) und Leiste neu laden.
  let wasRunning = !!(state.status && state.status.fetcher && state.status.fetcher.running);
  let timer = 0;
  function renderStatus() {
    replace(statusSlot, statusPill());
  }
  async function poll() {
    clearTimeout(timer);
    try { await loadStatus(); } catch { /* offline */ }
    const running = !!(state.status && state.status.fetcher && state.status.fetcher.running);
    renderStatus();
    if (wasRunning && !running) {
      loadBar();
      const scroller = document.getElementById('view');
      const top = Math.max(window.scrollY, scroller.scrollTop);
      if (top < 80) rebuildFeed();
    }
    wasRunning = running;
    timer = setTimeout(poll, running ? 5000 : 60000);
  }

  const feedHolder = h('div', { class: 'feed-holder' }, feed.el);
  const header = h('header', { class: 'hdr hdr--home' },
    h('h1', { class: 'hdr-wordmark t-wordmark', text: 'KIOSK' }),
    iconButton(search(20), 'Suchen', { href: '#/suche' }),
    iconButton(dots(20), 'Menü', { onClick: openMenu }));
  const el = h('div', { class: 'view-list' }, header, chipsEl, statusSlot, feedHolder);

  buildChips();
  // Nach dem Einhängen: Chipzeile ganz nach links (aktiver Chip sichtbar), dann ggf. gleiten lassen.
  requestAnimationFrame(() => {
    chipsEl.scrollLeft = 0;
    animateChips(chipsEl);
  });
  renderStatus();
  feed.start();
  timer = setTimeout(poll, wasRunning ? 5000 : 60000);

  const offs = [
    on('profiles', buildChips),
    on('reload-list', () => rebuildFeed()),
  ];

  return {
    el,
    layout: 'list',
    bar: true,
    title: profile ? profile.name : 'Kiosk',
    destroy() {
      clearTimeout(timer);
      feed.destroy();
      offs.forEach((off) => off());
    },
  };
}
