// Endlos-Liste: lädt Seiten (Keyset-Cursor oder Offset), gruppiert nach Tagen, zeigt Leerzustand.
import { desktopQuery, h, hint, isDesktop } from './dom.js';
import { articleRow, refreshRowTime } from './row.js';
import { dayLabel } from '../format.js';
import { api } from '../api.js';
import { on } from '../store.js';

/**
 * @param {object} opts
 *   load(cursor, isStale) → Promise<{items, next}>   next = Cursor der nächsten Seite oder null;
 *                isStale() ist true, sobald die Antwort nicht mehr gebraucht wird (Liste neu
 *                gestartet oder zerstört): dann nichts mehr außerhalb der Liste schreiben
 *   dayGroups    Tagesüberschriften (HEUTE, GESTERN, …)
 *   mode, words  an articleRow
 *   filter(item) clientseitiger Filter (z. B. „Mit Bild“)
 *   empty()      Element für den Leerzustand
 *   onPage(items, first) nach jeder Seite
 */
export function createFeed(opts) {
  const el = h('div', { class: 'list' });
  const sentinel = h('div', { class: 'sentinel', 'aria-hidden': 'true' });
  let cursor = null, done = false, loading = false, lastDay = null, gen = 0;
  const items = [];
  let observer = null;
  let resized = null;   // ResizeObserver: Sentinel wird wieder gerendert (Liste nach Artikel wieder da)
  let errorEl = null;
  // Handy scrollt das Fenster, Desktop die Spalte #view: Beobachter beim Umschalten neu anlegen.
  const onLayout = () => { if (observer) observe(); };
  let offClock = null;

  function append(list) {
    const frag = document.createDocumentFragment();
    for (const item of list) {
      if (opts.filter && !opts.filter(item)) continue;
      items.push(item);
      if (opts.dayGroups) {
        const label = dayLabel(item.published_at);
        if (label !== lastDay) {
          lastDay = label;
          const head = h('h2', { class: 'day t-section', text: label });
          head._iso = item.published_at;   // für relabel(): dieselbe Gruppe heißt morgen GESTERN
          frag.append(head);
        }
      }
      frag.append(articleRow(item, opts));
    }
    el.insertBefore(frag, sentinel);
  }

  async function more() {
    if (loading || done) return;
    loading = true;
    const myGen = gen;
    const isStale = () => myGen !== gen;
    try {
      // Mit Filter können Seiten leer bleiben: dann weiterladen (höchstens 5 Seiten am Stück).
      for (let i = 0; i < 5 && !done; i++) {
        const before = items.length;
        const res = await opts.load(cursor, isStale);
        if (isStale()) return;
        cursor = res.next ?? null;
        done = cursor === null || cursor === undefined || !res.items.length;
        append(res.items);
        opts.onPage?.(res.items, before === 0);
        if (items.length > before) break;
      }
      if (done) finish();
    } catch (err) {
      if (isStale()) return;
      // Nicht aufgeben: Hinweis mit „Erneut laden“, derselbe Cursor wird wiederholt.
      showError(err);
      loading = false;
      return;
    } finally {
      if (!isStale()) loading = false;
    }
    // Sentinel noch sichtbar (kurze Seite)? Dann direkt nachladen.
    if (!done && isVisible()) more();
  }

  function showError(err) {
    errorEl?.remove();
    errorEl = h('div', { class: 'feed-error' },
      hint(`Fehler: ${err.detail || err.message}`),
      h('button', {
        type: 'button', class: 'pill', text: 'Erneut laden',
        on: { click: () => { errorEl?.remove(); errorEl = null; more(); } },
      }));
    el.insertBefore(errorEl, sentinel);
  }

  function isVisible() {
    // Nicht gerendert (Handy mit offenem Artikel: #view ist display:none, das Rechteck wäre 0,0,0,0
    // und gälte als sichtbar): nicht nachladen; der ResizeObserver meldet sich, sobald die Liste wieder da ist.
    if (!sentinel.isConnected || !sentinel.getClientRects().length) return false;
    const r = sentinel.getBoundingClientRect();
    return r.top < window.innerHeight + 600 && r.bottom > -600;
  }

  /** Zeiten („5 MIN“) und Tagesüberschriften („HEUTE“) neu schreiben (lange offene App, Mitternacht). */
  function relabel() {
    let label = null;
    for (const node of el.children) {
      if (node.classList.contains('day')) {
        label = dayLabel(node._iso);
        if (node.textContent !== label) node.textContent = label;
      } else if (node.classList.contains('row')) {
        refreshRowTime(node);
      }
    }
    if (opts.dayGroups) lastDay = label;   // nächste Seite hängt an die umbenannte Gruppe an
  }

  function finish() {
    if (!items.length) {
      const empty = opts.empty?.();
      if (empty) el.insertBefore(empty, sentinel);
    }
  }

  function start() {
    gen++;
    cursor = null; done = false; loading = false; lastDay = null;
    items.length = 0;
    errorEl = null;
    el.replaceChildren(sentinel);
    if (!observer) {
      observe();
      desktopQuery.addEventListener('change', onLayout);
      offClock = on('clock', relabel);
      // Ohne Box gilt der Sentinel für den IntersectionObserver als „schneidend“ (Rechteck 0,0), beim
      // Wiedereinblenden meldet er dann nichts Neues. Die Größe wechselt dabei aber 0 ↔ 1 px.
      if (typeof ResizeObserver === 'function') {
        resized = new ResizeObserver(() => { if (!errorEl && isVisible()) more(); });
        resized.observe(sentinel);
      }
    }
    return more();
  }

  function observe() {
    observer?.disconnect();
    observer = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting) && !errorEl && isVisible()) more();
    }, { root: isDesktop() ? document.getElementById('view') : null, rootMargin: '600px 0px' });
    observer.observe(sentinel);
  }

  function destroy() {
    observer?.disconnect();
    observer = null;
    resized?.disconnect();
    resized = null;
    desktopQuery.removeEventListener('change', onLayout);
    offClock?.();
    offClock = null;
    gen++;
  }

  return { el, start, reload: start, destroy, items, get done() { return done; } };
}

/** Fertige Listen-Ladefunktion für /api/articles mit Keyset- oder Offset-Paging. onResponse nur für
 *  Antworten, die die Liste noch braucht (sonst überschriebe eine verspätete Antwort z. B. den
 *  Wasserstand der neuen Liste). */
export function articlesLoader(params, { offsetPaging = false, onResponse } = {}) {
  return async (cursor, isStale = () => false) => {
    // Ohne free-Parameter blendet das Backend Paywall-Artikel immer aus.
    const p = { limit: 40, ...params };
    if (cursor != null) p[offsetPaging ? 'offset' : 'before'] = cursor;
    const res = await api('articles', { params: p });
    if (!isStale()) onResponse?.(res, cursor);
    return { items: res.items || [], next: offsetPaging ? res.next_offset : res.next_before };
  };
}
