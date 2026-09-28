// Blatt (Sheet) von unten über gedimmter Ansicht; ab 900px als zentrierter Dialog.
import { h, replace } from './dom.js';

const host = () => document.getElementById('sheet');
let current = null;
// Fokussierbares im Blatt (Tab-Falle, erster Fokus).
const FOCUSABLE = 'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), '
  + 'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Sichtbare fokussierbare Elemente im Blatt, in Reihenfolge (erster Fokus und Tab-Falle). */
function focusables(panel) {
  return [...panel.querySelectorAll(FOCUSABLE)].filter((x) => x.getClientRects().length);
}

/** Hintergrund (#shell) für Tastatur, Klicks und Vorleser sperren, solange ein Blatt offen ist. */
function setBackgroundInert(on) {
  const shell = document.getElementById('shell');
  if (shell) shell.inert = on;
}

/**
 * Öffnet ein Blatt. build(panel) füllt das Panel; onClose wird beim Schließen gerufen
 * (Escape, Tippen auf das Overlay, closeSheet()). Gibt das Panel zurück (für rebuildSheet).
 */
export function openSheet(build, { label = 'Blatt', tall = false, onClose, onDispose } = {}) {
  closeSheet({ silent: true });
  const el = host();
  const panel = h('section', {
    class: `sheet-panel${tall ? ' sheet-panel--tall' : ''}`,
    role: 'dialog', 'aria-modal': 'true', 'aria-label': label, tabindex: '-1',
  });
  current = { panel, onClose, onDispose, returnFocus: document.activeElement };
  replace(el, panel);
  el.hidden = false;
  document.body.classList.add('sheet-open');
  setBackgroundInert(true);
  build(panel);
  (focusables(panel)[0] || panel).focus({ preventScroll: true });
  return panel;
}

/** Wiedererkennung eines fokussierten Elements nach dem Neuaufbau (gleiches Ziel bzw. gleiche Aufschrift). */
function focusKey(el) {
  return [el.tagName, el.getAttribute('href'), el.getAttribute('aria-label'), (el.textContent || '').trim()].join('|');
}

/** Inhalt des Blatts `panel` (Rückgabe von openSheet) neu aufbauen. Ist inzwischen ein anderes
 *  Blatt offen oder keins mehr (späte Antwort nach dem Schließen), passiert nichts. */
export function rebuildSheet(build, panel) {
  if (!current || !panel || current.panel !== panel) return;
  const scroll = panel.scrollTop;
  const active = panel.contains(document.activeElement) && document.activeElement !== panel ? document.activeElement : null;
  const key = active ? focusKey(active) : null;
  panel.replaceChildren();
  build(panel);
  panel.scrollTop = scroll;
  // Der fokussierte Knopf ist beim Neuaufbau verschwunden: auf das entsprechende neue Element (sonst das
  // erste im Blatt), damit Tastatur und Vorleser nicht springen.
  if (!panel.contains(document.activeElement)) {
    const items = focusables(panel);
    const same = key ? items.find((x) => focusKey(x) === key) : null;
    (same || items[0] || panel).focus({ preventScroll: true });
  }
}

/** Ist ein Blatt offen? Mit `panel`: ist genau dieses Blatt noch offen? */
export function isSheetOpen(panel) {
  return !!current && (!panel || current.panel === panel);
}

export function closeSheet({ silent = false } = {}) {
  if (!current) return;
  const { onClose, onDispose, returnFocus } = current;
  current = null;
  const el = host();
  el.hidden = true;
  el.replaceChildren();
  document.body.classList.remove('sheet-open');
  setBackgroundInert(false);   // vor dem Zurückgeben des Fokus (inerte Elemente nehmen keinen an)
  if (returnFocus && document.contains(returnFocus)) returnFocus.focus({ preventScroll: true });
  onDispose?.();   // immer (auch still geschlossen oder durch ein anderes Blatt ersetzt)
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
    const items = focusables(current.panel);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
});
