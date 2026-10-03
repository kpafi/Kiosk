// Artikelzeile der Liste (E2Liste): Meta, Titel (3 Zeilen), Teaser (2 Zeilen), Bild 80px.
import { h, replace } from './dom.js';
import { icon } from './icons.js';
import { cleanText, listTime } from '../format.js';
import { on, sourceLabel, state } from '../store.js';

/** Text in el schreiben und Wörter aus `words` als <mark> hervorheben (nur Textknoten). */
export function highlightInto(el, text, words) {
  el.replaceChildren();
  if (!text) return el;
  if (!words || !words.length) { el.textContent = text; return el; }
  const escaped = words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const re = new RegExp(`(${escaped.join('|')})`, 'gi');
  const lower = words.map((w) => w.toLowerCase());
  for (const part of text.split(re)) {
    if (!part) continue;
    if (lower.includes(part.toLowerCase())) el.append(h('mark', { text: part }));
    else el.append(document.createTextNode(part));
  }
  return el;
}

/** Markierung rechts in der Meta-Zeile: Stern (Favorit) oder Lesezeichen (später lesen). */
function markIcon(item, mode) {
  const starred = !!item.starred, later = !!item.later;
  let kind = null;
  if (mode === 'later') kind = later ? 'later' : starred ? 'star' : null;
  else kind = starred ? 'star' : later ? 'later' : null;
  if (kind === 'star') {
    const s = icon('star', 14, { filled: true });
    s.classList.add('row-mark', 'star-on');
    s.setAttribute('aria-label', 'Favorit');
    s.removeAttribute('aria-hidden');
    s.setAttribute('role', 'img');
    return s;
  }
  if (kind === 'later') {
    const b = icon('bookmark', 14, { filled: true });
    b.classList.add('row-mark', 'later-on');
    b.setAttribute('aria-label', 'Später lesen');
    b.removeAttribute('aria-hidden');
    b.setAttribute('role', 'img');
    return b;
  }
  return null;
}

/** Meta-Zeile wie in der Liste ohne Markierung: Ungelesen-Punkt, QUELLE, „/ LAND / ZEIT“. */
/** Meta-Zeile: Ungelesen-Punkt, Kurzname, „/ LAND / ZEIT“; `timeText(iso)` setzt die Zeit (Standard:
 *  relativ wie in der Liste, listTime). */
export function metaParts(item, timeText = listTime) {
  return [
    !item.read ? h('span', { class: 'unread-dot', role: 'img', 'aria-label': 'Ungelesen' }) : null,
    h('span', { class: 'row-src', text: sourceLabel(item.source_id, item.source_name) }),
    h('span', { class: 'row-when', text: whenText(item, timeText) }),
  ];
}

function whenText(item, timeText = listTime) {
  return `/ ${item.country || '?'} / ${timeText(item.published_at)}`;
}

/** Relative Zeit einer Listenzeile neu schreiben (sie veraltet in einer lange offenen App). */
export function refreshRowTime(row) {
  const when = row._item && row.querySelector('.row-when');
  if (!when) return;
  const text = whenText(row._item);
  if (when.textContent !== text) when.textContent = text;
}

function fillMeta(meta, item, mode) {
  replace(meta, metaParts(item), markIcon(item, mode));
}

/**
 * @param {object} item   Listen-Objekt der API
 * @param {object} opts   {mode: 'later'|'star'|undefined, words: [...] für Hervorhebung}
 */
export function articleRow(item, opts = {}) {
  const meta = h('div', { class: 'row-meta t-meta' });
  fillMeta(meta, item, opts.mode);
  const title = highlightInto(h('h2', { class: 'row-title t-headline' }), item.title || '(ohne Titel)', opts.words);
  const summary = cleanText(item.summary, item.title);
  const teaser = summary
    ? highlightInto(h('p', { class: 'row-teaser t-teaser' }), summary, opts.words)
    : null;
  let thumb = null;
  if (item.image_url && state.settings.show_images) {
    thumb = h('img', {
      class: 'thumb', src: item.image_url, alt: '', loading: 'lazy', decoding: 'async',
      referrerpolicy: 'no-referrer', width: 80, height: 80,
    });
    thumb.addEventListener('error', () => thumb.remove(), { once: true });
  }
  const el = h('a', {
    class: `row${item.read ? ' is-read' : ''}${state.openArticleId === item.id ? ' is-open' : ''}`,
    href: `#/a/${item.id}`,
    dataset: { id: item.id },
  }, h('div', { class: 'row-main' }, meta, title, teaser), thumb);
  el._item = item;
  el._mode = opts.mode;
  return el;
}

// Flag-Änderungen (Leseansicht, Tastatur) in allen sichtbaren Zeilen nachziehen.
on('flags', (patch) => {
  for (const el of document.querySelectorAll(`.row[data-id="${Number(patch.id)}"]`)) {
    const item = Object.assign(el._item || {}, patch);
    el.classList.toggle('is-read', !!item.read);
    fillMeta(el.querySelector('.row-meta'), item, el._mode);
  }
  for (const item of state.listItems) if (item.id === patch.id) Object.assign(item, patch);
});

/** Markiert die geöffnete Zeile (Desktop: raised-Karte). */
export function markOpenRow(id) {
  for (const el of document.querySelectorAll('.row.is-open')) el.classList.remove('is-open');
  if (id == null) return;
  for (const el of document.querySelectorAll(`.row[data-id="${Number(id)}"]`)) el.classList.add('is-open');
}
