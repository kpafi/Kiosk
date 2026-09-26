// Liste (E2Liste, E2Leer): Kopf, Filterchips, Tagesgruppen, Lade-Pille, Leerzustand.
import { h, replace, toast } from '../ui/dom.js';
import { dots, pencil, search } from '../ui/icons.js';
import { iconButton } from '../ui/header.js';
import { chip } from '../ui/chips.js';
import { articlesLoader, createFeed } from '../ui/feed.js';
import { emptyState, nextRunSentence } from '../ui/empty.js';
import { openMenu } from './menu.js';
import {
  ctxParams, keepWatermark, loadBar, loadStatus, on, profileById, replaceHash, setUnreadFilter, state,
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
    replace(chipsEl,
      chip('Ungelesen', {
        on: state.unread,
        onClick: () => {
          setUnreadFilter(!state.unread);
          rebuildFeed();
          buildChips();
        },
      }),
      profiles.map((p) => {
        const active = state.ctx.type === 'profile' && String(p.id) === String(state.ctx.id);
        return chip(p.name, { on: active, href: active ? '#/' : `#/?p=${p.id}` });
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
    else {
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

  const feedHolder = h('div', {}, feed.el);
  const header = h('header', { class: 'hdr hdr--home' },
    h('h1', { class: 'hdr-wordmark t-wordmark', text: 'KIOSK' }),
    iconButton(search(20), 'Suchen', { href: '#/suche' }),
    iconButton(dots(20), 'Menü', { onClick: openMenu }));
  const el = h('div', { class: 'view-list' }, header, chipsEl, statusSlot, feedHolder);

  buildChips();
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
