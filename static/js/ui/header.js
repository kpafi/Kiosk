// Kopfzeilen: Startseite (Wortmarke) und Unterseiten (Zurück + Titel).
import { h } from './dom.js';
import { back as backIcon } from './icons.js';
import { goBack } from '../store.js';

/** Runder Icon-Knopf (Link oder Button). */
export function iconButton(iconEl, label, { href, onClick, on = false, small = false, extraClass = '' } = {}) {
  const cls = `icon-btn${small ? ' icon-btn--sm' : ''}${on ? ' is-on' : ''} ${extraClass}`.trim();
  if (href) return h('a', { class: cls, href, 'aria-label': label }, iconEl);
  return h('button', {
    type: 'button', class: cls, 'aria-label': label, on: { click: onClick },
  }, iconEl);
}

export function backButton(fallback, extraClass = '') {
  return iconButton(backIcon(20), 'Zurück', { onClick: () => goBack(fallback), extraClass });
}

/**
 * Unterseiten-Kopf: Zurück, Titel (Punktmatrix) mit optionaler Unterzeile, rechts Aktionen.
 */
export function subHeader({ title, sub, fallback = '#/', actions = [], titleEl, extraClass = '' }) {
  let middle;
  if (titleEl) middle = titleEl;
  else if (sub !== undefined) {
    middle = h('div', { class: 'hdr-titles' },
      h('h1', { class: 't-section', text: title }),
      h('span', { class: 't-caption', text: sub }));
  } else {
    middle = h('h1', { class: 'hdr-title t-section', text: title });
  }
  return h('header', { class: `hdr ${extraClass}`.trim() }, backButton(fallback), middle, ...actions);
}
