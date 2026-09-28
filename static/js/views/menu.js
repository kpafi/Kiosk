// Menü-Blatt (E2Menu): Profile, Lieblingszeitungen, Gemerkt, Einstellungen, alles gelesen.
import { h } from '../ui/dom.js';
import { icon, checkCheck, chevronRight, close as closeIcon } from '../ui/icons.js';
import { closeSheet, isSheetOpen, openSheet, rebuildSheet, sheetGrip } from '../ui/sheet.js';
import { toast } from '../ui/dom.js';
import { api } from '../api.js';
import {
  emit, loadBar, loadCounts, loadFavorites, loadProfiles, loadSources, on, profileById, readAllWatermark, state,
} from '../store.js';

const THEME_LABEL = { system: 'System', dark: 'Dunkel', light: 'Hell' };

function menuItem(href, iconEl, title, sub) {
  return h('a', { class: 'menu-item', href, on: { click: () => closeSheet({ silent: true }) } },
    h('span', { class: 'menu-icon' }, iconEl),
    h('span', { class: 'menu-text' },
      h('span', { class: 't-ui', text: title }),
      h('span', { class: 't-caption', text: sub || ' ' })),
    h('span', { class: 'chev' }, chevronRight(18)));
}

/** Alle Ungelesenen im aktuellen Kontext (für die Rückfrage), auch von früheren Tagen. Die Zähler
 *  zählen nur heute; eine Gesamtzahl gibt es nur für Zeitungen (unread_all), sonst null. */
function contextUnreadAll() {
  const ctx = state.ctx;
  if (ctx.type === 'source') {
    const s = state.sourceMap.get(ctx.id);
    return s && typeof s.unread_all === 'number' ? s.unread_all : null;
  }
  return null;
}

/** Ist in der geladenen Kontext-Liste nichts mehr ungelesen? Zeitung: unread_all (die Liste kann
 *  „Mit Bild“ filtern); sonst ganz geladene Liste ohne ungelesenen Artikel (auch leer). */
function nothingUnread() {
  if (state.ctx.type === 'source') return contextUnreadAll() === 0;
  return !!state.listWatermark && state.listComplete && !(state.listItems || []).some((it) => !it.read);
}

/** Was der Knopf „Alles als gelesen markieren“ gerade zeigt (neu bauen nur bei Änderung). */
function readAllKey() {
  return JSON.stringify([state.listIsCtx, !!readAllWatermark(), nothingUnread()]);
}

function contextName() {
  const ctx = state.ctx;
  if (ctx.type === 'profile') return (profileById(ctx.id) || {}).name || 'Profil';
  if (ctx.type === 'source') return (state.sourceMap.get(ctx.id) || {}).name || ctx.id;
  return null;
}

/** Body für read-all im aktuellen Kontext; max_id begrenzt auf die angezeigten Artikel, damit ein
 *  Sammellauf zwischen Rückfrage und Klick keine ungesehenen Artikel als gelesen markiert. */
export function readAllBody(ctx = state.ctx, maxId = readAllWatermark()) {
  const body = {};
  if (ctx.type === 'source') body.source = ctx.id;
  if (ctx.type === 'profile') body.profile = Number(ctx.id);
  if (maxId) body.max_id = maxId;
  return body;
}

/** Alles als gelesen markieren geht nur mit Wasserstand einer echten Kontext-Liste (Liste, Profil,
 *  Zeitung); ohne ihn (Suche, Gemerkt, erste Seite noch nicht da) würde read-all alles markieren. */
export async function markAllRead(ctx = state.ctx, maxId = readAllWatermark()) {
  if (!maxId) throw new Error('Liste noch nicht geladen');
  const res = await api('articles/read-all', { method: 'POST', body: readAllBody(ctx, maxId) });
  const n = res && typeof res.marked === 'number' ? res.marked : null;
  toast(n === null ? 'Alles gelesen' : `${n} Artikel als gelesen markiert`);
  emit('reload-list');
  emit('unread-changed');
}

export function openMenu() {
  let confirming = false;
  let confirmMaxId;   // Stand der Liste beim Öffnen der Rückfrage
  let builtKey = '';

  function build(panel) {
    builtKey = readAllKey();
    const c = state.counts;
    const profiles = state.profiles;
    const barItems = state.bar || [];
    const inBarProfiles = barItems.filter((i) => i.type === 'profile').length;
    const inBarSources = barItems.filter((i) => i.type === 'source').length;

    const profileSub = profiles
      ? `${profiles.length} ${profiles.length === 1 ? 'Profil' : 'Profile'} · ${inBarProfiles} in der Leiste`
      : 'Nicht geladen';
    const favCount = c ? c.favorites : state.favorites ? state.favorites.length : null;
    const favSub = favCount !== null && favCount !== undefined
      ? `${favCount} ${favCount === 1 ? 'Zeitung' : 'Zeitungen'} · ${inBarSources} in der Leiste`
      : 'Nicht geladen';
    const savedSub = c ? `${c.later} später lesen · ${c.starred} Favoriten` : '';
    const interval = state.status && state.status.settings ? Math.round(state.status.settings.fetch_interval / 60) : null;
    const settingsSub = [THEME_LABEL[state.settings.theme] || 'System', interval ? `alle ${interval} Min` : null]
      .filter(Boolean).join(' · ');

    const unread = contextUnreadAll();
    const name = contextName();

    // Nur zu einer Kontext-Liste (Liste, Profil, Zeitung), nicht aus Suche oder Gemerkt und nicht, wenn
    // dort nichts ungelesen ist; solange deren erste Seite (Wasserstand) fehlt, ausgegraut (das Blatt
    // wird neu gebaut, sobald sie da ist).
    let bottom = null;
    if (!state.listIsCtx || (!confirming && nothingUnread())) {
      confirming = false;
    } else if (!confirming) {
      const ready = !!readAllWatermark();
      bottom = h('button', {
        type: 'button', class: 'pill pill--block', style: { marginTop: '12px' }, disabled: !ready,
        on: {
          click: () => {
            confirmMaxId = readAllWatermark();
            if (!confirmMaxId) return;
            confirming = true;
            rebuildSheet(build, panel);
            panel.querySelector('.confirm .pill--primary')?.focus();
          },
        },
      }, checkCheck(20), h('span', { text: 'Alles als gelesen markieren' }));
    } else {
      const what = unread !== null && unread !== undefined ? `${unread} Artikel` : 'Alle Artikel';
      bottom = h('div', { class: 'confirm' },
        h('span', { class: 'confirm-text t-caption', text: `Wirklich? ${what}${name ? ` in „${name}“` : ''}` }),
        h('button', {
          type: 'button', class: 'pill', text: 'Abbrechen',
          on: { click: () => { confirming = false; rebuildSheet(build, panel); } },
        }),
        h('button', {
          type: 'button', class: 'pill pill--primary', text: 'Ja',
          on: {
            click: async (e) => {
              e.currentTarget.disabled = true;
              try {
                await markAllRead(state.ctx, confirmMaxId);
                if (isSheetOpen(panel)) closeSheet();   // nicht ein inzwischen geöffnetes anderes Blatt
              } catch (err) {
                toast(`Fehler: ${err.detail || err.message}`);
                confirming = false;
                rebuildSheet(build, panel);
              }
            },
          },
        }));
    }

    panel.append(
      sheetGrip(),
      h('div', { class: 'sheet-head' },
        h('h2', { class: 't-section', text: 'MENÜ' }),
        h('button', {
          type: 'button', class: 'icon-btn icon-btn--plain', 'aria-label': 'Schließen',
          on: { click: () => closeSheet() },
        }, closeIcon(14))),
      menuItem('#/profile', icon('sliders', 20), 'Profile', profileSub),
      menuItem('#/profile#zeitungen', icon('newspaper', 20), 'Lieblingszeitungen', favSub),
      menuItem('#/gemerkt', icon('bookmark', 20), 'Gemerkt', savedSub),
      menuItem('#/einstellungen', icon('settings', 20), 'Einstellungen', settingsSub));
    if (bottom) panel.append(bottom);
  }

  // Wasserstand oder Seiten der Liste kommen, während das Blatt offen ist: Knopf nachziehen. Beim
  // Schließen (auch still oder durch ein anderes Blatt) wieder abmelden.
  let offWatermark = () => {};
  const panel = openSheet(build, { label: 'Menü', onDispose: () => offWatermark() });
  offWatermark = on('watermark', () => {
    if (isSheetOpen(panel) && readAllKey() !== builtKey) rebuildSheet(build, panel);
  });
  // Zähler frisch holen und Blatt nachziehen.
  // Zeitungen nur im Zeitungskontext (unread_all für die Rückfrage), sonst unnötig.
  Promise.allSettled([loadCounts(), loadProfiles(), loadBar(), state.ctx.type === 'source' ? loadSources() : null,
    state.counts ? null : loadFavorites()])
    .then(() => rebuildSheet(build, panel));
}
