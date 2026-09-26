// Endlos-Liste: lädt Seiten (Keyset-Cursor oder Offset), gruppiert nach Tagen, zeigt Leerzustand.
import { h, hint, isDesktop } from './dom.js';
import { articleRow } from './row.js';
import { dayLabel } from '../format.js';
import { api } from '../api.js';

/**
 * @param {object} opts
 *   load(cursor) → Promise<{items, next}>   next = Cursor der nächsten Seite oder null
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
  let errorEl = null;
  // Handy scrollt das Fenster, Desktop die Spalte #view: Beobachter beim Umschalten neu anlegen.
  const desktopQuery = window.matchMedia('(min-width: 900px)');
  const onLayout = () => { if (observer) observe(); };

  function append(list) {
    const frag = document.createDocumentFragment();
    for (const item of list) {
      if (opts.filter && !opts.filter(item)) continue;
      items.push(item);
      if (opts.dayGroups) {
        const label = dayLabel(item.published_at);
        if (label !== lastDay) {
          lastDay = label;
          frag.append(h('h2', { class: 'day t-section', text: label }));
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
    try {
      // Mit Filter können Seiten leer bleiben: dann weiterladen (höchstens 5 Seiten am Stück).
      for (let i = 0; i < 5 && !done; i++) {
        const before = items.length;
        const res = await opts.load(cursor);
        if (myGen !== gen) return;
        cursor = res.next ?? null;
        done = cursor === null || cursor === undefined || !res.items.length;
        append(res.items);
        opts.onPage?.(res.items, before === 0);
        if (items.length > before) break;
      }
      if (done) finish();
    } catch (err) {
      if (myGen !== gen) return;
      // Nicht aufgeben: Hinweis mit „Erneut laden“, derselbe Cursor wird wiederholt.
      showError(err);
      loading = false;
      return;
    } finally {
      if (myGen === gen) loading = false;
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
    const r = sentinel.getBoundingClientRect();
    return r.top < window.innerHeight + 600 && r.bottom > -600;
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
    }
    return more();
  }

  function observe() {
    observer?.disconnect();
    observer = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting) && !errorEl) more();
    }, { root: isDesktop() ? document.getElementById('view') : null, rootMargin: '600px 0px' });
    observer.observe(sentinel);
  }

  function destroy() {
    observer?.disconnect();
    observer = null;
    desktopQuery.removeEventListener('change', onLayout);
    gen++;
  }

  return { el, start, reload: start, destroy, items, get done() { return done; } };
}

/** Fertige Listen-Ladefunktion für /api/articles mit Keyset- oder Offset-Paging. */
export function articlesLoader(params, { offsetPaging = false, onResponse } = {}) {
  return async (cursor) => {
    // Ohne free-Parameter blendet das Backend Paywall-Artikel immer aus.
    const p = { limit: 40, ...params };
    if (cursor != null) p[offsetPaging ? 'offset' : 'before'] = cursor;
    const res = await api('articles', { params: p });
    onResponse?.(res, cursor);
    return { items: res.items || [], next: offsetPaging ? res.next_offset : res.next_before };
  };
}
