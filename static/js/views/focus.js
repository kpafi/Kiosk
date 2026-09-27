// Fokusmodus am Desktop (E3DesktopFokus): Liste eingeklappt zur schmalen Leiste, Text mittig und
// größer. Schalter in der Kopfzeile der Leseansicht, Taste F, Esc beendet. Gespeichert als
// Einstellung focus_mode; die Wirkung (nur ab 900 px und nur mit offenem Artikel) steckt in app.css
// unter html[data-focus="on"].
import { h, isDesktop, toast } from '../ui/dom.js';
import { bookmark, dots, listIcon, search } from '../ui/icons.js';
import { iconButton } from '../ui/header.js';
import { setToggle } from '../ui/toggle.js';
import { openMenu } from './menu.js';
import { api } from '../api.js';
import { serialSaver } from '../saver.js';
import { on, setLocalSettings, state } from '../store.js';

const byId = (id) => document.getElementById(id);

/** Wirkt der Fokusmodus gerade (Desktop, Artikel offen, eingeschaltet)? */
export function focusActive() {
  return state.settings.focus_mode === true && isDesktop() && byId('shell').dataset.layout === 'article';
}

/**
 * Tastaturfokus nach einem Wechsel (nur Desktop): Wirkt der Fokusmodus und liegt der Fokus in der
 * (nun unsichtbaren) Liste oder nirgends, geht er auf die Leseansicht (Leertaste/Bild ab scrollen
 * den Artikel). Beim Ausschalten aus der schmalen Leiste heraus geht er auf die Zeile des offenen
 * Artikels (sonst die Leseansicht). Ohne Fokusmodus: Ist das angeklickte Element verschwunden (Link
 * in der Übersicht oder Kontextspalte, #reader wurde neu gefüllt), ebenfalls auf die Leseansicht.
 */
export function settleFocus() {
  if (!isDesktop()) return;
  const active = document.activeElement;
  const view = byId('view');
  const reader = byId('reader');
  const rail = byId('focus-rail');
  const lost = !active || active === document.body || !active.isConnected;
  if (focusActive()) {
    if (lost || view.contains(active)) reader.focus({ preventScroll: true });
  } else if (active && rail && rail.contains(active)) {
    (view.querySelector('.row.is-open') || reader).focus({ preventScroll: true });
  } else if (lost) {
    reader.focus({ preventScroll: true });
  }
}

// Speichern nacheinander (Taste F schnell hintereinander): der Server endet beim letzten Wert.
const saver = serialSaver({
  get: () => state.settings.focus_mode === true,
  send: async (value) => {
    const data = await api('settings', { method: 'PATCH', body: { focus_mode: value } });
    return data && typeof data.focus_mode === 'boolean' ? data.focus_mode : value;
  },
  apply: (value, err) => {
    setLocalSettings({ focus_mode: value });
    settleFocus();
    if (err) toast(`Fehler: ${err.detail || err.message}`);
  },
});

// Abmelden/Benutzerwechsel: laufendes Speichern beenden, ohne danach etwas zu setzen oder zu melden.
on('user-reset', () => saver.reset());

/** Fokusmodus an/aus: sofort anwenden, dann speichern. Liefert true, wenn der Server den Wert hat
 *  (läuft noch ein Speichern, dessen Ergebnis). */
export function setFocusMode(value) {
  const next = !!value;
  if ((state.settings.focus_mode === true) === next) return saver.current() || Promise.resolve(true);
  saver.remember();
  setLocalSettings({ focus_mode: next });
  settleFocus();
  return saver.flush();
}

/** Schalter „Fokusmodus“ in den Einstellungen: nach dem Speichern (auch nach mehreren schnellen
 *  Klicks und einem Fehler) zeigt er den tatsächlichen Stand; Fehler meldet setFocusMode selbst. */
export async function focusSwitchChange(value, el) {
  await setFocusMode(value);
  setToggle(el, state.settings.focus_mode === true);
}

export function toggleFocusMode() {
  return setFocusMode(state.settings.focus_mode !== true);
}

/** Schmale Leiste links (76 px) anstelle der Liste; nur sichtbar, solange der Fokusmodus wirkt. */
export function installFocusRail() {
  const shell = byId('shell');
  const count = h('span', { class: 'focus-rail-num', 'aria-hidden': 'true' });
  const countLabel = h('span', { class: 'visually-hidden' });
  const counter = h('div', { class: 'focus-rail-count' },
    countLabel, count, h('span', { class: 't-caption muted', 'aria-hidden': 'true', text: 'Neu' }));

  const el = h('nav', { id: 'focus-rail', class: 'focus-rail', 'aria-label': 'Seitenleiste' },
    h('div', { class: 'focus-rail-mark', 'aria-hidden': 'true', text: 'K' }),
    iconButton(listIcon(20), 'Liste einblenden', { onClick: () => setFocusMode(false) }),
    iconButton(search(20), 'Suchen', { href: '#/suche' }),
    iconButton(bookmark(20), 'Gemerkt', { href: '#/gemerkt' }),
    iconButton(dots(20), 'Menü', { onClick: openMenu }),
    counter);

  function paintCount() {
    const all = (state.bar || []).find((i) => i.type === 'all');
    const n = all && typeof all.unread === 'number' ? all.unread : null;
    counter.hidden = n === null;
    count.textContent = n === null ? '' : n > 999 ? '999+' : String(n);
    countLabel.textContent = n === null ? '' : `${n} ungelesen heute`;
  }
  paintCount();
  on('bar', paintCount);
  shell.prepend(el);
  return el;
}
