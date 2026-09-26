// Leerzustand (E2Leer): Punktmuster, Titel in Punktmatrix, Satz, Pillen.
import { h } from './dom.js';
import { state } from '../store.js';

// 5×3 Punkte, die hellen bilden ein Plus (wie im Mockup).
const PATTERN = [0, 0, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 0, 0];

export function matrix() {
  return h('div', { class: 'matrix', 'aria-hidden': 'true' },
    PATTERN.map((on) => h('span', { class: on ? 'on' : '' })));
}

export function emptyState({ title = 'NICHTS NEUES', text, actions = [] }) {
  return h('div', { class: 'empty' },
    matrix(),
    h('div', { class: 't-section', text: title }),
    text ? h('p', { class: 't-teaser', text }) : null,
    actions.length ? h('div', { class: 'pill-row' }, actions) : null);
}

/** „Die nächsten Artikel kommen in etwa 9 Minuten.“ aus /api/status. */
export function nextRunSentence() {
  const st = state.status;
  if (!st || !st.fetcher) return '';
  if (st.fetcher.running) return 'Gerade werden neue Artikel geholt.';
  const last = st.fetcher.last_finished ? new Date(st.fetcher.last_finished) : null;
  const interval = st.settings && st.settings.fetch_interval;
  if (!last || Number.isNaN(last.getTime()) || !interval) return '';
  const mins = Math.max(1, Math.ceil((last.getTime() + interval * 1000 - Date.now()) / 60000));
  return mins === 1
    ? 'Die nächsten Artikel kommen in etwa 1 Minute.'
    : `Die nächsten Artikel kommen in etwa ${mins} Minuten.`;
}
