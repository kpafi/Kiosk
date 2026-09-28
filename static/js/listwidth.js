// Breite der Artikelliste am Desktop (reine Logik ohne DOM; auch in tests/test_frontend_js.py
// geprüft). Die Liste lässt sich zwischen LIST_MIN und listMax(Fensterbreite) ziehen; gespeichert
// wird die gewählte Breite je Gerät, angewendet die auf das aktuelle Fenster begrenzte.

export const LIST_DEFAULT = 520;
export const LIST_MIN = 320;
export const LIST_MAX = 760;
export const LIST_STEP = 16;     // Pfeiltasten
export const LIST_KEY = 'kiosk.listWidth';

/** Größte Breite: die Hälfte des Fensters, höchstens LIST_MAX, aber nie unter der Standardbreite
 *  (so bleibt das bisherige Layout auch bei schmalen Fenstern ab 900 px erreichbar). */
export function listMax(windowWidth) {
  const half = Math.floor((Number(windowWidth) || 0) / 2);
  return Math.min(LIST_MAX, Math.max(LIST_DEFAULT, half));
}

/** Breite auf [LIST_MIN, listMax] begrenzen und runden; Unbrauchbares ergibt die Standardbreite. */
export function clampListWidth(value, windowWidth) {
  const n = typeof value === 'number' ? value : Number.NaN;
  const width = Number.isFinite(n) ? n : LIST_DEFAULT;
  return Math.round(Math.min(listMax(windowWidth), Math.max(LIST_MIN, width)));
}

/** Gespeicherten Wert lesen: Zahl oder null (fehlt, kaputt, kein endlicher Wert). */
export function parseStoredWidth(raw) {
  if (raw === null || raw === undefined || raw === '') return null;
  let value;
  try { value = JSON.parse(raw); } catch { return null; }
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** Neue Breite für eine Taste (Pfeile ±LIST_STEP, Pos1/Ende min/max) oder null (andere Taste). */
export function keyWidth(key, current, windowWidth) {
  if (key === 'ArrowLeft') return clampListWidth(current - LIST_STEP, windowWidth);
  if (key === 'ArrowRight') return clampListWidth(current + LIST_STEP, windowWidth);
  if (key === 'Home') return LIST_MIN;
  if (key === 'End') return listMax(windowWidth);
  return null;
}
