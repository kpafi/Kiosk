// Hinweis „Neue Version“ nach einem Deploy: ruhige Pille, die wartet, bis man „Neu laden“ tippt.
// Kein automatisches Neuladen: Das könnte die Stelle im Artikel, eine halb getippte Suche oder ein
// offenes Blatt verwerfen. Die Pille liegt in #shell (Lage je Layout in app.css, gesperrt mit dem
// übrigen Hintergrund, solange ein Blatt offen ist) und nie auf dem Anmelde- oder Offline-Bildschirm.
import { h } from './dom.js';
import { close as closeIcon, icon } from './icons.js';

let pill = null;
let status = null;     // eigene Live-Region nur für den Text; die Knöpfe liegen daneben
let wanted = false;    // neue Version gemeldet, weder neu geladen noch „Später“
let allowed = false;   // angemeldet und eine App-Ansicht zu sehen
let announceTimer = 0;
let onReload = () => location.reload();

function build() {
  status = h('span', { class: 'update-text', role: 'status', 'aria-live': 'polite' });
  const reload = h('button', {
    type: 'button', class: 'update-reload',
    on: { click: () => { reload.disabled = true; onReload(); } },
  }, icon('refresh', 18), h('span', { class: 'update-reload-label', text: 'Neu laden' }));
  pill = h('div', { class: 'update-pill', hidden: true },
    h('span', { class: 'update-dot', 'aria-hidden': 'true' }),
    status,
    reload,
    h('button', {
      type: 'button', class: 'update-close', 'aria-label': 'Später',
      on: { click: later },
    }, closeIcon(16)));
  document.getElementById('shell').append(pill);
}

/** Ist das Element zu sehen (nicht display:none, nicht visibility:hidden wie die Liste im Fokusmodus)? */
function shown(el) {
  return !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
}

/** „Später“: Pille weg; der Fokus (Tastatur) geht auf die Liste, oder auf die Leseansicht, wenn die
 *  Liste nicht zu sehen ist (Handy mit Artikel, Fokusmodus), statt auf body oder den versteckten Knopf. */
function later() {
  const hadFocus = pill.contains(document.activeElement);
  wanted = false;
  sync();
  if (!hadFocus) return;
  const view = document.getElementById('view');
  (shown(view) ? view : document.getElementById('reader'))?.focus({ preventScroll: true });
}

function sync() {
  const show = wanted && allowed;
  if (!pill) {
    if (!show) return;
    build();
  }
  pill.hidden = !show;
  document.body.classList.toggle('has-update', show);
  if (!show) {
    clearTimeout(announceTimer);
    announceTimer = 0;
    status.textContent = '';
    return;
  }
  // Text erst nach dem Sichtbarwerden in die (schon vorhandene) Live-Region: sonst liest ihn der
  // Vorleser nicht vor. Weitere sync()-Aufrufe (Layoutwechsel) lassen einen laufenden Zeitgeber stehen.
  if (!status.textContent && !announceTimer) {
    announceTimer = setTimeout(() => {
      announceTimer = 0;
      if (!pill.hidden) status.textContent = 'Neue Version';
    }, 100);
  }
}

/** Neue Version melden (auch erneut nach „Später“, siehe buildWatcher). */
export function showUpdatePill(reload) {
  if (reload) onReload = reload;
  wanted = true;
  sync();
}

/** Darf die Pille gerade stehen? (app.js: nicht auf Anmeldung und Offline-Hinweis) */
export function setUpdatePillAllowed(value) {
  allowed = !!value;
  sync();
}
