// Globaler Zustand, Ladefunktionen und Navigation. Views importieren von hier, app.js routet.

import { api } from './api.js';

export const DEFAULT_SETTINGS = {
  theme: 'dark',
  text_size: 'm',
  mark_read_on_open: true,
  show_images: true,
  free_only: true,   // nur noch aus Kompatibilität; Paywall-Artikel zeigt die Oberfläche nie
  bar_order: [],
  desktop_empty: 'overview',   // Desktop ohne offenen Artikel: 'overview' (Tagesübersicht) oder 'frontpage'
  focus_mode: false,           // Desktop: Fokusmodus (Liste eingeklappt, Text mittig)
};

const LS_SETTINGS = 'kiosk.settings';   // auch von theme-boot.js gelesen
const LS_UNREAD = 'kiosk.unread';

function lsGet(key) {
  try { return JSON.parse(localStorage.getItem(key)); } catch { return null; }
}
function lsSet(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* privat/gesperrt */ }
}

export const state = {
  authed: null,                 // null = unbekannt, true/false nach /api/session
  user: null,                   // {id, username, display_name, is_admin} aus /api/session
  settings: { ...DEFAULT_SETTINGS, ...(lsGet(LS_SETTINGS) || {}) },
  status: null,                 // /api/status
  sources: null,                // /api/sources
  sourceMap: new Map(),
  profiles: null,               // /api/profiles (null = fehlt/unbekannt)
  favorites: null,              // /api/favorites/sources
  bar: null,                    // /api/bar items
  counts: null,                 // /api/counts
  unread: lsGet(LS_UNREAD) ?? true,  // Filterchip „Ungelesen“
  ctx: { type: 'all' },         // aktueller Listen-Kontext {type: all|profile|source, id}
  listItems: [],                // Artikel der sichtbaren Liste (Tastatur, Fallback für „Nächster“)
  listIsCtx: false,             // listItems gehört zum Kontext ctx (Liste, Zeitung), nicht Suche/Gemerkt
  listWatermark: null,          // max_id der ersten Seite der Kontext-Liste (für read-all)
  lastListHash: '#/',           // letzte Listenansicht (für Zurück und Desktop-Spalte)
  prevHash: null,              // vorige Adresse (vor dem letzten Adresswechsel)
  openArticleId: null,
};

/** Darf der angemeldete Benutzer Zeitungen schalten und Sammelläufe starten? */
export function isAdmin() {
  return !!(state.user && state.user.is_admin);
}

/** Sitzung aus /api/session übernehmen. */
export function setSession(session) {
  state.authed = !!(session && session.authenticated);
  state.user = state.authed ? session.user || null : null;
}

/** Beim Anmelden und Abmelden: Daten des vorigen Benutzers vergessen, auch die lokalen Kopien der
 *  Einstellungen und des Filters „Ungelesen“ (sonst blieben Theme, Textgröße und Filter vom
 *  vorigen Benutzer stehen) sowie den Listen-Kontext (Profil-IDs gehören dem vorigen Benutzer). */
export function resetUserState() {
  try {
    localStorage.removeItem(LS_SETTINGS);
    localStorage.removeItem(LS_UNREAD);
  } catch { /* privat/gesperrt */ }
  state.settings = { ...DEFAULT_SETTINGS };
  Object.assign(state, {
    user: null, status: null, sources: null, sourceMap: new Map(), profiles: null,
    favorites: null, bar: null, counts: null, listItems: [], listIsCtx: false, listWatermark: null,
    lastListHash: '#/', openArticleId: null, unread: true, ctx: { type: 'all' },
  });
  userGen += 1;
  applySettings();
  emit('user-reset');   // z. B. laufendes Speichern des Fokusmodus beenden (focus.js)
  emit('bar');   // Zähler (Leiste, Fokus-Leiste) nicht mit den Zahlen des vorigen Benutzers stehen lassen
}

// ---- Ereignisse ------------------------------------------------------------

const bus = new EventTarget();
export function on(name, fn) {
  const handler = (e) => fn(e.detail);
  bus.addEventListener(name, handler);
  return () => bus.removeEventListener(name, handler);
}
export function emit(name, detail) {
  bus.dispatchEvent(new CustomEvent(name, { detail }));
}

// ---- Laden ------------------------------------------------------------------

export async function loadSettings() {
  const data = await api('settings');
  if (data) {
    state.settings = { ...DEFAULT_SETTINGS, ...data };
    lsSet(LS_SETTINGS, state.settings);
  }
  applySettings();
  return state.settings;
}

export async function loadStatus() {
  state.status = await api('status');
  return state.status;
}

export async function loadSources() {
  const list = await api('sources');
  state.sources = list;
  state.sourceMap = new Map(list.map((s) => [s.id, s]));
  return list;
}

export async function loadProfiles() {
  state.profiles = await api('profiles');
  return state.profiles;
}

export async function loadFavorites() {
  state.favorites = await api('favorites/sources');
  return state.favorites;
}

export async function loadBar() {
  const data = await api('bar');
  state.bar = data ? data.items : null;
  emit('bar');
  return state.bar;
}

export async function loadCounts() {
  state.counts = await api('counts');
  return state.counts;
}

export async function loadSession() {
  const session = await api('session');
  setSession(session);
  return session;
}

/** Alles, was fast jede Ansicht braucht (nach dem Anmelden). */
export async function loadBasics() {
  await Promise.allSettled([loadSettings(), loadSources(), loadProfiles(), loadBar(), loadStatus()]);
}

// ---- Einstellungen --------------------------------------------------------------

const lightQuery = window.matchMedia('(prefers-color-scheme: light)');
lightQuery.addEventListener('change', () => applySettings());

export function applySettings() {
  const s = state.settings;
  const root = document.documentElement;
  const theme = s.theme === 'system' ? (lightQuery.matches ? 'light' : 'dark') : s.theme;
  root.dataset.theme = theme === 'light' ? 'light' : 'dark';
  root.dataset.text = ['s', 'm', 'l'].includes(s.text_size) ? s.text_size : 'm';
  // Fokusmodus wirkt nur am Desktop mit offenem Artikel (CSS); das Attribut steht immer.
  root.dataset.focus = s.focus_mode === true ? 'on' : 'off';
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', theme === 'light' ? '#f2f2f2' : '#000000');
}

/** Einstellungen nur lokal übernehmen und anwenden (ohne Speichern), z. B. zum Zurückrollen. */
export function setLocalSettings(patch) {
  state.settings = { ...state.settings, ...patch };
  lsSet(LS_SETTINGS, state.settings);
  applySettings();
  emit('settings', patch);
}

const settingsSeq = {};   // je Schlüssel: nur die Antwort des letzten Speicherns dieses Schlüssels zählt
let userGen = 0;          // Benutzerwechsel: späte Antworten des vorigen Benutzers zählen nicht

/** Sofort anwenden, dann speichern (Fehler gehen an den Aufrufer). Aus der Antwort werden nur die
 *  eigenen Schlüssel übernommen (focus_mode z. B. speichert focus.js selbst, nacheinander). */
export async function saveSettings(patch) {
  const gen = userGen;
  const seqs = {};
  for (const key of Object.keys(patch)) seqs[key] = settingsSeq[key] = (settingsSeq[key] || 0) + 1;
  setLocalSettings(patch);
  const data = await api('settings', { method: 'PATCH', body: patch });
  if (!data || gen !== userGen) return;
  // Hat der Server für einen eigenen Schlüssel etwas anderes gespeichert, gilt das.
  const changed = {};
  for (const key of Object.keys(patch)) {
    if (settingsSeq[key] !== seqs[key] || !(key in data)) continue;   // inzwischen erneut gespeichert
    if (JSON.stringify(data[key]) !== JSON.stringify(state.settings[key])) changed[key] = data[key];
  }
  if (Object.keys(changed).length) setLocalSettings(changed);
}

export function setUnreadFilter(value) {
  state.unread = !!value;
  lsSet(LS_UNREAD, state.unread);
}

// ---- Artikel-Flags ---------------------------------------------------------------

const FLAG_FIELD = { read: 'read', star: 'starred', later: 'later' };

/** Setzt read/star/later, meldet die Änderung an alle Zeilen und die Leseansicht. */
export async function setFlag(id, flag, value) {
  const field = FLAG_FIELD[flag];
  emit('flags', { id, [field]: value ? 1 : 0 });
  try {
    await api(`articles/${id}/${flag}`, { method: 'POST', body: { value: !!value } });
    if (flag === 'read') emit('unread-changed');
    return true;
  } catch (err) {
    emit('flags', { id, [field]: value ? 0 : 1 }); // zurückrollen
    throw err;
  }
}

// ---- Kontext -------------------------------------------------------------------------

/** API-Parameter für den Listen-Kontext. */
export function ctxParams(ctx = state.ctx) {
  if (ctx.type === 'profile') return { profile: ctx.id };
  if (ctx.type === 'source') return { source: ctx.id };
  return {};
}

export function profileById(id) {
  return (state.profiles || []).find((p) => String(p.id) === String(id)) || null;
}

/**
 * Profil-Chips der Liste in Anzeigereihenfolge: das aktive Profil (ctx) vorn, danach die übrigen mit
 * in_chips in ihrer gespeicherten Reihenfolge. Das aktive steht auch ohne in_chips da (z. B. aus der
 * Leiste oder per Link geöffnet), damit man sieht, was filtert, und es abwählen kann.
 */
export function chipProfiles(profiles, ctx) {
  const list = profiles || [];
  const active = (p) => isActiveProfile(p, ctx);
  return [...list.filter(active), ...list.filter((p) => !active(p) && p.in_chips !== false)];
}

/** Ist `p` das Profil des Listen-Kontexts `ctx`? (IDs als Zahl oder Text) */
export function isActiveProfile(p, ctx) {
  return !!ctx && ctx.type === 'profile' && String(p.id) === String(ctx.id);
}

/** Anzeigename einer Quelle: Kurzname (short) oder Name. */
export function sourceLabel(id, fallback) {
  const s = state.sourceMap.get(id);
  return (s && (s.short || s.name)) || fallback || id;
}

// ---- Navigation ------------------------------------------------------------------------
// Einträge, die innerhalb der App erreicht wurden, tragen history.state.kiosk = true.
// Nur dann ist history.back() sicher (sonst würde Zurück die App verlassen).

export function navigate(hash) {
  if (location.hash === hash) { emit('reroute'); return; }
  location.hash = hash;
}

export function replaceHash(hash) {
  const tagged = !!(history.state && history.state.kiosk);
  history.replaceState({ kiosk: tagged }, '', hash);
  emit('reroute');
}

/** Wasserstand für read-all: max_id aus der ersten Seite der Kontext-Liste (auch wenn sie leer
 *  ist), damit ein Sammellauf danach keine ungesehenen Artikel als gelesen markiert. */
export function readAllWatermark() {
  return state.listIsCtx && state.listWatermark ? state.listWatermark : undefined;
}

/** onResponse für articlesLoader in Kontext-Listen: Wasserstand je Laden der Liste merken. */
export function keepWatermark(res, cursor) {
  if (cursor == null) state.listWatermark = res && res.max_id ? res.max_id : null;
}

export function goBack(fallback = state.lastListHash || '#/') {
  if (history.state && history.state.kiosk) history.back();
  else replaceHash(fallback);
}
