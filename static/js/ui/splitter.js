// Ziehbare Trennlinie zwischen Artikelliste (#view) und Leseansicht (#reader), nur am Desktop.
// Setzt --list-w auf #shell (Spalte der Liste, Breite der Leiste unten). Maus/Stift/Finger per
// Pointer Events (einmal je Bild), Tastatur als role="separator" (Pfeile ±16 px, Pos1/Ende),
// Doppelklick setzt auf 520 px zurück. Gespeichert je Gerät in localStorage.
import { h } from './dom.js';
import { on } from '../store.js';
import {
  LIST_DEFAULT, LIST_KEY, LIST_MIN, clampListWidth, keyWidth, listMax, parseStoredWidth,
} from '../listwidth.js';

function readStored() {
  try { return parseStoredWidth(localStorage.getItem(LIST_KEY)); } catch { return null; }
}
function writeStored(value) {
  try {
    if (value === null) localStorage.removeItem(LIST_KEY);
    else localStorage.setItem(LIST_KEY, JSON.stringify(value));
  } catch { /* privat/gesperrt: gilt nur bis zum Neuladen */ }
}

export function installListResizer() {
  const shell = document.getElementById('shell');
  const reader = document.getElementById('reader');
  let wanted = readStored();   // gewählte Breite (null = Standard); angewendet wird sie begrenzt
  let width = 0;

  const handle = h('div', {
    id: 'list-resize', class: 'list-resize', role: 'separator', tabindex: '0',
    'aria-orientation': 'vertical', 'aria-label': 'Breite der Artikelliste', 'aria-controls': 'view',
    title: 'Ziehen: Breite ändern · Doppelklick: zurücksetzen',
  });

  function apply(value) {
    width = clampListWidth(value ?? LIST_DEFAULT, window.innerWidth);
    shell.style.setProperty('--list-w', `${width}px`);
    shell.classList.toggle('list-narrow', width < 360);   // schmale Liste: Pille ohne Beschriftung
    handle.setAttribute('aria-valuemin', String(LIST_MIN));
    handle.setAttribute('aria-valuemax', String(listMax(window.innerWidth)));
    handle.setAttribute('aria-valuenow', String(width));
    handle.setAttribute('aria-valuetext', `${width} Pixel`);
  }

  function choose(value) {
    apply(value);
    wanted = value === null ? null : width;
    writeStored(wanted);
  }

  // ---- Ziehen ----
  let drag = null;   // {id, startX, startW, ratio, moved}
  let frame = 0;
  let pendingX = 0;
  const MOVE_MIN = 2;   // erst ab so vielen Pixeln Bewegung ist es ein Ziehen (sonst ein Klick)

  const visible = () => handle.getClientRects().length > 0;

  /** Ziehen abbrechen (Griff verschwunden, z. B. Fokusmodus): alte Breite, nichts speichern. */
  function cancelDrag() {
    if (!drag) return;
    const { id, startW } = drag;
    drag = null;
    cancelAnimationFrame(frame);
    frame = 0;
    shell.classList.remove('is-resizing');
    if (handle.hasPointerCapture(id)) handle.releasePointerCapture(id);
    apply(startW);
  }

  function paintDrag() {
    frame = 0;
    if (!drag) return;
    if (!visible()) { cancelDrag(); return; }
    if (Math.abs(pendingX - drag.startX) >= MOVE_MIN) drag.moved = true;
    if (!drag.moved) return;
    apply(drag.startW + (pendingX - drag.startX));
    // Leseansicht behält ihre Lage im Text (Anteil), obwohl der Text neu umbricht.
    const max = reader.scrollHeight - reader.clientHeight;
    if (drag.ratio !== null && max > 0) reader.scrollTop = Math.round(drag.ratio * max);
  }

  handle.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || drag) return;
    e.preventDefault();
    const max = reader.scrollHeight - reader.clientHeight;
    drag = { id: e.pointerId, startX: e.clientX, startW: width, ratio: max > 0 ? reader.scrollTop / max : null, moved: false };
    pendingX = e.clientX;
    handle.setPointerCapture(e.pointerId);
    shell.classList.add('is-resizing');
  });
  handle.addEventListener('pointermove', (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    pendingX = e.clientX;
    if (!frame) frame = requestAnimationFrame(paintDrag);
  });
  /** Ende des Ziehens: gespeichert wird nur, wenn sich der Zeiger wirklich bewegt hat (ein Klick
   *  überschreibt die gewählte Breite nicht). */
  function endDrag(e) {
    if (!drag || e.pointerId !== drag.id) return;
    cancelAnimationFrame(frame);
    frame = 0;
    paintDrag();
    if (!drag) return;   // paintDrag hat abgebrochen (Griff verschwunden)
    const { moved } = drag;
    drag = null;
    shell.classList.remove('is-resizing');
    if (handle.hasPointerCapture(e.pointerId)) handle.releasePointerCapture(e.pointerId);
    if (moved) choose(width);
  }
  handle.addEventListener('pointerup', endDrag);
  handle.addEventListener('pointercancel', endDrag);
  handle.addEventListener('lostpointercapture', endDrag);
  // Fokusmodus (Taste F) blendet den Griff aus: ein laufendes Ziehen endet ohne Speichern.
  on('settings', (patch) => { if ('focus_mode' in patch) cancelDrag(); });
  handle.addEventListener('dblclick', () => choose(null));

  // ---- Tastatur ----
  handle.addEventListener('keydown', (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const next = keyWidth(e.key, width, window.innerWidth);
    if (next === null) return;
    e.preventDefault();
    e.stopPropagation();
    choose(next);
  });

  // ---- Fenstergröße: begrenzen, die gewählte Breite bleibt gespeichert ----
  let resizeFrame = 0;
  window.addEventListener('resize', () => {
    if (!resizeFrame) resizeFrame = requestAnimationFrame(() => { resizeFrame = 0; if (!drag) apply(wanted); });
  });

  apply(wanted);
  shell.insertBefore(handle, reader);
  return handle;
}
