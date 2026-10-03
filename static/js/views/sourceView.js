// Zeitungsansicht (E2Zeitung): Name in Punktmatrix, Lieblingszeitung, alles gelesen, Liste.
import { h, replace, toast } from '../ui/dom.js';
import { checkCheck, star } from '../ui/icons.js';
import { backButton, iconButton } from '../ui/header.js';
import { chip } from '../ui/chips.js';
import { articlesLoader, createFeed } from '../ui/feed.js';
import { emptyState, nextRunSentence } from '../ui/empty.js';
import { markAllRead } from './menu.js';
import { api, refreshError } from '../api.js';
import { ago } from '../format.js';
import {
  beginCtxList, ctxPageShown, isAdmin, keepWatermark, loadBar, loadSources, loadStatus, on, readAllWatermark, state,
} from '../store.js';

export function sourceView(route) {
  const id = route.id;
  state.ctx = { type: 'source', id };
  state.nextCtx = { source: id };
  let mode = state.unread ? 'unread' : 'all'; // unread | all | images
  let confirming = false;
  let confirmMaxId;   // Stand der Liste beim Öffnen der Rückfrage
  let alive = true;   // false nach destroy(): späte Antworten bauen die Liste nicht mehr neu auf

  const titles = h('div', { class: 'hdr-titles' });
  const favBtn = iconButton(star(20), 'Lieblingszeitung', { onClick: toggleFavorite });
  const readBtn = iconButton(checkCheck(20), 'Alle gelesen', {
    onClick: () => {
      confirmMaxId = readAllWatermark();
      if (!confirmMaxId) return;
      confirming = !confirming;
      paintConfirm();
    },
  });
  // Erst mit Wasserstand (erste Seite da) bedienbar, sonst meldete read-all „Liste noch nicht geladen“.
  const paintReadBtn = () => { readBtn.disabled = !readAllWatermark(); };
  paintReadBtn();
  const confirmSlot = h('div', {});
  const chipsEl = h('div', { class: 'chips', 'aria-label': 'Filter' });
  const feedHolder = h('div', { class: 'feed-holder' });
  let feed = null;

  const src = () => state.sourceMap.get(id);
  // Ungelesen von heute (ab 00:00): Paywall-freie Zahl aus der Leiste bevorzugen (wie der Chip
  // unten), sonst aus /api/sources.
  const unreadCount = () => {
    const barItem = (state.bar || []).find((i) => i.type === 'source' && i.id === id);
    const s = src();
    return barItem ? barItem.unread : s ? s.unread : null;
  };
  // Alle ungelesenen, auch von früheren Tagen (so viele zeigt „Ungelesen“, so viele markiert
  // „Alle gelesen“); null, solange /api/sources fehlt.
  const unreadAllCount = () => {
    const s = src();
    return s && typeof s.unread_all === 'number' ? s.unread_all : null;
  };

  function paintHeader() {
    const s = src();
    const name = s ? s.name : id;
    const parts = [s ? (s.generated ? 'KI-Ausgabe' : s.country) : null];
    const unread = unreadCount();
    if (typeof unread === 'number') parts.push(`${unread} ungelesen heute`);   // null: nicht berechnet
    // Generierte Quellen (KI-Ausgabe) holen nichts, sie schreiben.
    if (s && s.last_ok_at) parts.push(`${ago(s.last_ok_at)} ${s.generated ? 'geschrieben' : 'geholt'}`);
    replace(titles,
      h('h1', { class: 't-section', text: name.toUpperCase() }),
      h('span', { class: 't-caption', text: parts.filter(Boolean).join(' · ') }));
    const fav = !!(s && s.is_favorite);
    favBtn.classList.toggle('is-on', fav);
    favBtn.setAttribute('aria-pressed', String(fav));
    favBtn.setAttribute('aria-label', fav ? 'Aus Lieblingszeitungen entfernen' : 'Zu Lieblingszeitungen');
    favBtn.replaceChildren(star(20, fav));
    // Ausgeschaltet oder gar nichts ungelesen (auch nicht von früheren Tagen): keine Filter und
    // kein „Alle gelesen“. Der Zähler oben zählt nur heute, belegt aber, wenn positiv, dass etwas
    // ungelesen ist (die Leiste ist nach einem Sammellauf oft frischer als unread_all).
    const idle = (!!s && !s.enabled) || (unreadAllCount() === 0 && !(unread > 0));
    chipsEl.hidden = idle;
    readBtn.hidden = idle;
    if (idle && confirming) { confirming = false; paintConfirm(); }
  }

  async function toggleFavorite() {
    const s = src();
    if (!s) return;
    const next = !s.is_favorite;
    s.is_favorite = next;
    paintHeader();
    try {
      if (next) await api(`favorites/sources/${encodeURIComponent(id)}`, { method: 'PUT', body: {} });
      else await api(`favorites/sources/${encodeURIComponent(id)}`, { method: 'DELETE' });
      toast(next ? 'Lieblingszeitung, in der Leiste' : 'Aus Lieblingszeitungen entfernt');
      await loadBar();
    } catch (err) {
      s.is_favorite = !next;
      paintHeader();
      toast(`Fehler: ${err.detail || err.message}`);
    }
  }

  function paintConfirm() {
    readBtn.classList.toggle('is-on', confirming);
    if (!confirming) { confirmSlot.replaceChildren(); return; }
    // unread_all kann nach einem Sammellauf veraltet sein; heute ist nie mehr als insgesamt.
    const all = unreadAllCount();
    const n = all === null ? null : Math.max(all, unreadCount() ?? 0);
    replace(confirmSlot, h('div', { class: 'confirm', style: { margin: '0 var(--gutter) 8px' } },
      h('span', { class: 'confirm-text t-caption', text: n !== null ? `Wirklich? ${n} Artikel` : 'Wirklich alle?' }),
      h('button', { type: 'button', class: 'pill', text: 'Abbrechen', on: { click: () => { confirming = false; paintConfirm(); } } }),
      h('button', {
        type: 'button', class: 'pill pill--primary', text: 'Ja',
        on: {
          click: async (e) => {
            e.currentTarget.disabled = true;   // kein zweiter Auftrag, solange der erste läuft
            try {
              await markAllRead({ type: 'source', id }, confirmMaxId);
            } catch (err) {
              toast(`Fehler: ${err.detail || err.message}`);
            }
            confirming = false;
            paintConfirm();
            refreshSource();
          },
        },
      })));
  }

  function buildChips() {
    replace(chipsEl,
      chip('Ungelesen', { on: mode === 'unread', onClick: () => setMode('unread') }),
      chip('Alle', { on: mode === 'all', onClick: () => setMode('all') }),
      chip('Mit Bild', { on: mode === 'images', onClick: () => setMode('images') }));
  }

  function setMode(m) {
    mode = m;
    buildChips();
    rebuild();
  }

  function rebuild() {
    if (!alive) return;   // sonst überschriebe die alte Ansicht listItems und Wasserstand der neuen
    // Offene Rückfrage gehört zur alten Liste (ihr Wasserstand): schließen.
    if (confirming) { confirming = false; paintConfirm(); }
    feed?.destroy();
    feed = createFeed({
      dayGroups: true,
      load: articlesLoader({ source: id, unread: mode === 'unread' ? 'true' : undefined }, { onResponse: keepWatermark }),
      onPage: ctxPageShown,
      filter: mode === 'images' ? (it) => !!it.image_url : null,
      empty: () => {
        const s = src();
        const name = s ? s.name : id;
        if (s && !s.enabled && !isAdmin()) {
          // Einschalten darf nur der Admin; als Ausweg die Liste aller Artikel.
          return emptyState({
            title: 'AUSGESCHALTET',
            text: 'Diese Zeitung ist ausgeschaltet. Nur der Admin kann sie einschalten.',
            actions: [h('a', { class: 'pill', href: '#/', text: 'Alle Artikel zeigen' })],
          });
        }
        if (s && !s.enabled) {
          // Ausgeschaltete Zeitung: kein Hinweis auf den nächsten Sammellauf, stattdessen Einschalten.
          const onBtn = h('button', {
            type: 'button', class: 'pill', text: 'Einschalten',
            on: {
              click: async () => {
                onBtn.disabled = true;
                try {
                  const updated = await api(`sources/${encodeURIComponent(id)}`, { method: 'PATCH', body: { enabled: true } });
                  // s kann veraltet sein (/api/sources inzwischen neu geladen): auch das aktuelle Objekt
                  // in state.sourceMap nachziehen, sonst zeigt die Ansicht weiter „AUSGESCHALTET“.
                  for (const obj of new Set([s, src()])) if (obj) Object.assign(obj, updated || { enabled: true });
                  paintHeader();
                  toast(`${name} eingeschaltet`);
                  loadBar().catch(() => {});
                  try {
                    await api('refresh', { method: 'POST' });
                    loadStatus().catch(() => {});
                  } catch (err) {
                    toast(err.status === 409 ? 'Wird im nächsten Lauf geholt'
                      : refreshError(err));
                  }
                  rebuild();
                } catch (err) {
                  onBtn.disabled = false;
                  toast(err.status === 403 ? 'Nur der Admin kann Zeitungen einschalten' : `Fehler: ${err.detail || err.message}`);
                }
              },
            },
          });
          return emptyState({
            title: 'AUSGESCHALTET',
            text: 'Diese Zeitung ist ausgeschaltet. Schalte sie ein, um Artikel zu holen.',
            actions: [onBtn],
          });
        }
        const nothingYet = !!s && s.total === 0;
        let text = mode === 'unread' ? `In „${name}“ ist alles gelesen.` : `Aus „${name}“ ist noch nichts da.`;
        if (mode === 'images') text = `In „${name}“ gibt es keine Artikel mit Bild.`;
        if (nothingYet) text = `Aus „${name}“ ist noch nichts da.`;
        const next = nextRunSentence();
        if (next && (mode === 'unread' || nothingYet)) text += ` ${next}`;
        const actions = mode === 'unread' && !nothingYet
          ? [h('button', { type: 'button', class: 'pill', text: 'Alle Artikel zeigen', on: { click: () => setMode('all') } })]
          : [];
        return emptyState({ text, actions });
      },
    });
    beginCtxList(feed.items);
    feedHolder.replaceChildren(feed.el);
    feed.start();
  }

  async function refreshSource() {
    try { await loadSources(); } catch { /* egal */ }
    paintHeader();
  }

  const offs = [
    on('reload-list', () => rebuild()),
    on('watermark', paintReadBtn),
    // Gelesen-Änderungen: refreshCounters (app.js) lädt /api/sources gebündelt nach und meldet 'bar'.
    on('bar', () => paintHeader()),
  ];

  const el = h('div', { class: 'view-list' },
    h('header', { class: 'hdr' }, backButton('#/'), titles, favBtn, readBtn),
    confirmSlot, chipsEl, feedHolder);

  paintHeader();
  buildChips();
  rebuild();
  refreshSource(); // unread_all frisch holen (die Leiste wird nach Sammelläufen öfter neu geladen)

  return {
    el, layout: 'list', bar: true, title: src() ? src().name : 'Zeitung',
    destroy() { alive = false; feed?.destroy(); offs.forEach((off) => off()); },
  };
}
