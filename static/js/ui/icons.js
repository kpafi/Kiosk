// Strichsymbole aus den Mockups (Lucide-Pfade, 24er viewBox, 1,5px Strich).
// Jede Funktion liefert ein frisches <svg>-Element. Das Markup ist konstant (kein Nutzerinhalt).

const NS = 'http://www.w3.org/2000/svg';

const SHAPES = {
  search: { body: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>' },
  dots: { body: '<circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/>', fill: true },
  pencil: { body: '<path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>' },
  plus: { body: '<path d="M12 5v14M5 12h14"/>', join: false },
  back: { body: '<path d="M19 12H5M12 19l-7-7 7-7"/>' },
  bookmark: { body: '<path d="M6 3h12v18l-6-4-6 4z"/>', cap: false },
  star: { body: '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2L12 17.3 6.4 20.2l1.1-6.2L3 9.6l6.2-.9z"/>', cap: false },
  checkCheck: { body: '<path d="M18 6 7 17l-5-5M22 10l-7.5 7.5L13 16"/>' },
  close: { body: '<path d="M18 6 6 18M6 6l12 12"/>', join: false },
  refresh: { body: '<path d="M21 12a9 9 0 1 1-9-9c2.5 0 4.9 1 6.7 2.7L21 8"/><path d="M21 3v5h-5"/>' },
  chevronRight: { body: '<path d="m9 6 6 6-6 6"/>' },
  chevronUp: { body: '<path d="m6 15 6-6 6 6"/>' },
  chevronDown: { body: '<path d="m6 9 6 6 6-6"/>' },
  arrowRight: { body: '<path d="M5 12h14M12 5l7 7-7 7"/>' },
  external: { body: '<path d="M15 3h6v6M10 14 21 3M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/>' },
  sliders: { body: '<path d="M21 4h-7M10 4H3M21 12h-9M8 12H3M21 20h-5M12 20H3"/><circle cx="12" cy="4" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="14" cy="20" r="2"/>' },
  newspaper: { body: '<path d="M4 5h16v14H4zM8 9h8M8 13h5"/>' },
  settings: { body: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>' },
  clock: { body: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>' },
  // Fokusmodus (E3DesktopFokus): aufziehen, einklappen, Liste einblenden.
  expand: { body: '<path d="M4 4h6M4 4v6M20 20h-6M20 20v-6M4 4l6 6M20 20l-6-6"/>' },
  collapse: { body: '<path d="M10 4v6H4M14 20v-6h6M10 10 4 4M14 14l6 6"/>' },
  list: { body: '<path d="M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01"/>', join: false },
};

/**
 * @param {string} name   Schlüssel aus SHAPES
 * @param {number} size   Kantenlänge in px (Mockups: 20, 18, 16, 14)
 * @param {object} opts   {filled: true} füllt die Form mit currentColor (Stern, Lesezeichen)
 */
export function icon(name, size = 20, opts = {}) {
  const shape = SHAPES[name];
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('width', size);
  svg.setAttribute('height', size);
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  if (shape.fill) {
    svg.setAttribute('fill', 'currentColor');
  } else {
    svg.setAttribute('fill', opts.filled ? 'currentColor' : 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.5');
    if (shape.cap !== false) svg.setAttribute('stroke-linecap', 'round');
    if (shape.join !== false) svg.setAttribute('stroke-linejoin', 'round');
  }
  svg.innerHTML = shape.body; // konstantes Markup aus SHAPES
  return svg;
}

// Benannte Funktionen für die häufigsten Symbole.
export const search = (s) => icon('search', s);
export const dots = (s) => icon('dots', s);
export const pencil = (s = 16) => icon('pencil', s);
export const plus = (s) => icon('plus', s);
export const back = (s) => icon('back', s);
export const bookmark = (s, filled) => icon('bookmark', s, { filled });
export const star = (s, filled) => icon('star', s, { filled });
export const checkCheck = (s) => icon('checkCheck', s);
export const close = (s = 14) => icon('close', s);
export const chevronRight = (s = 18) => icon('chevronRight', s);
export const arrowRight = (s = 18) => icon('arrowRight', s);
export const external = (s = 16) => icon('external', s);
export const clock = (s = 16) => icon('clock', s);
export const expand = (s = 20) => icon('expand', s);
export const collapse = (s = 20) => icon('collapse', s);
export const listIcon = (s = 20) => icon('list', s);
