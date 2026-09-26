// Menü-Blatt (E2Menu): Profile, Lieblingszeitungen, Gemerkt, Einstellungen, alles gelesen.
import { h } from '../ui/dom.js';
import { icon, checkCheck, chevronRight, close as closeIcon } from '../ui/icons.js';
import { closeSheet, isSheetOpen, openSheet, rebuildSheet, sheetGrip } from '../ui/sheet.js';
import { toast } from '../ui/dom.js';
import { api } from '../api.js';
import {
  emit, loadBar, loadCounts, loadFavorites, loadProfiles, loadSources, profileById, readAllWatermark, state,
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

export async function markAllRead(ctx = state.ctx, maxId = readAllWatermark()) {
  const res = await api('articles/read-all', { method: 'POST', body: readAllBody(ctx, maxId) });
  const n = res && typeof res.marked === 'number' ? res.marked : null;
  toast(n === null ? 'Alles gelesen' : `${n} Artikel als gelesen markiert`);
  emit('reload-list');
  emit('unread-changed');
}

export function openMenu() {
  let confirming = false;
  let confirmMaxId;   // Stand der Liste beim Öffnen der Rückfrage

  function build(panel) {
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

    let bottom;
    if (!confirming) {
      bottom = h('button', {
        type: 'button', class: 'pill pill--block', style: { marginTop: '12px' },
        on: { click: () => { confirming = true; confirmMaxId = readAllWatermark(); rebuildSheet(build, panel); panel.querySelector('.confirm .pill--primary')?.focus(); } },
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
      menuItem('#/einstellungen', icon('settings', 20), 'Einstellungen', settingsSub),
      bottom);
  }

  const panel = openSheet(build, { label: 'Menü' });
  // Zähler frisch holen und Blatt nachziehen.
  // Zeitungen nur im Zeitungskontext (unread_all für die Rückfrage), sonst unnötig.
  Promise.allSettled([loadCounts(), loadProfiles(), loadBar(), state.ctx.type === 'source' ? loadSources() : null,
    state.counts ? null : loadFavorites()])
    .then(() => rebuildSheet(build, panel));
}
