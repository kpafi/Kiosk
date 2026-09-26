// Suche (E2Suche): Suchfeld, Zeitungs- und Zeitraum-Chips, Trefferzeile, Hervorhebung.
import { h, hint, replace } from '../ui/dom.js';
import { close as closeIcon, search as searchIcon } from '../ui/icons.js';
import { backButton } from '../ui/header.js';
import { chip } from '../ui/chips.js';
import { createFeed } from '../ui/feed.js';
import { emptyState } from '../ui/empty.js';
import { api } from '../api.js';
import { state } from '../store.js';

const RANGES = [
  { value: '1', label: '1 Tag' },
  { value: '7', label: '7 Tage' },
  { value: '30', label: '30 Tage' },
  { value: '', label: 'Alle' },
];

/** Wörter der Anfrage für die Hervorhebung (ohne FTS-Operatoren). */
export function queryWords(q) {
  return [...new Set(
    q.replace(/["()*^:]/g, ' ').split(/\s+/)
      .filter((w) => w.length >= 2 && !['AND', 'OR', 'NOT', 'NEAR'].includes(w)),
  )];
}

export function searchView(route) {
  let q = route.params.get('q') || '';
  let days = route.params.get('tage') || '';
  let source = route.params.get('s') || '';
  const fromSource = source; // Zeitung, aus deren Ansicht die Suche kam
  state.nextCtx = 'local';

  let timer = 0;
  let feed = null;
  const results = h('div', {});
  const resultLine = h('div', { class: 'result-line', hidden: true },
    h('span', { class: 't-section' }), h('span', { class: 't-caption', text: 'Nach Relevanz' }));
  const lineWrap = h('div', { class: 'list', style: { paddingBottom: '0' } }, resultLine);

  const input = h('input', {
    type: 'search', value: q, 'aria-label': 'Suchbegriff', placeholder: 'Suchen',
    autocomplete: 'off', enterkeyhint: 'search', spellcheck: 'false', maxlength: 200,
  });
  const clearBtn = h('button', {
    type: 'button', class: 'icon-btn icon-btn--plain', 'aria-label': 'Suche löschen',
    on: { click: () => { input.value = ''; input.focus(); schedule(0); } },
  }, closeIcon(14));
  const field = h('label', { class: 'search-field' }, searchIcon(20), input, clearBtn);

  const chipsEl = h('div', { class: 'chips', 'aria-label': 'Suchfilter' });

  function buildChips() {
    const src = fromSource ? state.sourceMap.get(fromSource) : null;
    replace(chipsEl,
      chip('Alle Zeitungen', { on: !source, onClick: () => { source = ''; update(); } }),
      src ? chip(src.name, { on: source === fromSource, onClick: () => { source = fromSource; update(); } }) : null,
      h('span', { class: 'chips-sep', 'aria-hidden': 'true' }),
      RANGES.map((r) => chip(r.label, { on: days === r.value, onClick: () => { days = r.value; update(); } })));
  }

  function syncUrl() {
    const p = new URLSearchParams();
    if (q) p.set('q', q);
    if (days) p.set('tage', days);
    if (source) p.set('s', source);
    const s = p.toString();
    const tagged = !!(history.state && history.state.kiosk);
    history.replaceState({ kiosk: tagged }, '', `#/suche${s ? `?${s}` : ''}`);
    state.lastListHash = location.hash;
  }

  function update() {
    buildChips();
    run();
  }

  function schedule(delay = 300) {
    clearTimeout(timer);
    timer = setTimeout(() => {
      q = input.value.trim();
      run();
    }, delay);
  }

  function run() {
    syncUrl();
    feed?.destroy();
    feed = null;
    resultLine.hidden = true;
    clearBtn.hidden = !input.value;
    if (q.length < 2) {
      replace(results, q.length ? hint('Mindestens 2 Zeichen', 'hint--pad') : null);
      state.listItems = [];
      state.listIsCtx = false;
      return;
    }
    const words = queryWords(q);
    feed = createFeed({
      dayGroups: false,
      words,
      load: async (cursor, isStale) => {
        const params = {
          q, limit: 30, days: days || undefined,
          source: source || undefined, offset: cursor ?? undefined,
        };
        const res = await api('articles', { params });
        // Verspätete Antwort einer älteren Anfrage (oder nach dem Leeren): Trefferzeile nicht anfassen.
        if (cursor == null && !isStale()) {
          const total = typeof res.total === 'number' ? res.total : null;
          const n = total !== null ? (total >= 1000 ? '1000+' : total) : `${res.items.length}${res.next_offset ? '+' : ''}`;
          resultLine.firstChild.textContent = `${n} ${total === 1 ? 'TREFFER' : 'TREFFER'}`;
          resultLine.hidden = false;
        }
        return { items: res.items || [], next: res.next_offset };
      },
      empty: () => emptyState({ title: 'KEINE TREFFER', text: `Nichts gefunden für „${q}“.` }),
    });
    state.listItems = feed.items;
    state.listIsCtx = false;
    replace(results, feed.el);
    feed.start().catch(() => {});
  }

  input.addEventListener('input', () => { clearBtn.hidden = !input.value; schedule(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); schedule(0); input.blur(); }
  });

  const el = h('div', { class: 'view-list' },
    h('header', { class: 'hdr' }, backButton('#/'), field),
    chipsEl, lineWrap, results);

  buildChips();
  run();

  return {
    el, layout: 'list', bar: false, title: 'Suche',
    onShow() { if (!q) input.focus(); },
    destroy() { clearTimeout(timer); feed?.destroy(); },
  };
}
