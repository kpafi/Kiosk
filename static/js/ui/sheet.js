// Blatt (Sheet) von unten über gedimmter Ansicht; ab 900px als zentrierter Dialog.
import { h, replace } from './dom.js';

const host = () => document.getElementById('sheet');
let current = null;

/**
 * Öffnet ein Blatt. build(panel) füllt das Panel; onClose wird beim Schließen gerufen
 * (Escape, Tippen auf das Overlay, closeSheet()). Gibt das Panel zurück (für rebuildSheet).
 */
export function openSheet(build, { label = 'Blatt', tall = false, onClose } = {}) {
  closeSheet({ silent: true });
  const el = host();
  const panel = h('section', {
    class: `sheet-panel${tall ? ' sheet-panel--tall' : ''}`,
    role: 'dialog', 'aria-modal': 'true', 'aria-label': label, tabindex: '-1',
  });
  current = { panel, onClose, returnFocus: document.activeElement };
  replace(el, panel);
  el.hidden = false;
  document.body.classList.add('sheet-open');
  build(panel);
  const first = panel.querySelector('button, a[href], input');
  (first || panel).focus({ preventScroll: true });
  return panel;
}

/** Inhalt des Blatts `panel` (Rückgabe von openSheet) neu aufbauen. Ist inzwischen ein anderes
 *  Blatt offen oder keins mehr (späte Antwort nach dem Schließen), passiert nichts. */
export function rebuildSheet(build, panel) {
  if (!current || !panel || current.panel !== panel) return;
  const scroll = panel.scrollTop;
  panel.replaceChildren();
  build(panel);
  panel.scrollTop = scroll;
  // Der fokussierte Knopf ist beim Neuaufbau verschwunden: Fokus im Blatt halten.
  if (!panel.contains(document.activeElement)) {
    const first = panel.querySelector('button:not([disabled]), a[href], input');
    (first || panel).focus({ preventScroll: true });
  }
}

/** Ist ein Blatt offen? Mit `panel`: ist genau dieses Blatt noch offen? */
export function isSheetOpen(panel) {
  return !!current && (!panel || current.panel === panel);
}

export function closeSheet({ silent = false } = {}) {
  if (!current) return;
  const { onClose, returnFocus } = current;
  current = null;
  const el = host();
  el.hidden = true;
  el.replaceChildren();
  document.body.classList.remove('sheet-open');
  if (returnFocus && document.contains(returnFocus)) returnFocus.focus({ preventScroll: true });
  if (!silent) onClose?.();
}

export function sheetGrip() {
  return h('div', { class: 'sheet-grip', 'aria-hidden': 'true' });
}

// Overlay-Klick und Escape schließen; Tab bleibt im Blatt.
document.addEventListener('click', (e) => {
  if (current && e.target === host()) closeSheet();
});
document.addEventListener('keydown', (e) => {
  if (!current) return;
  if (e.key === 'Escape') { e.preventDefault(); closeSheet(); return; }
  if (e.key === 'Tab') {
    const items = [...current.panel.querySelectorAll('button:not([disabled]), a[href], input')];
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
});
