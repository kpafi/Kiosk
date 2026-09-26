// Kiosk App: Hash-Router, Layout (Handy/Desktop), Anmeldung, Start.
// Zustand und Ladefunktionen liegen in store.js, Bausteine in ui/, Screens in views/.

import { api, setUnauthorizedHandler } from './api.js';
import {
  applySettings, emit, loadBar, loadBasics, loadSession, loadSources, on, replaceHash, resetUserState,
  setSession, state,
} from './store.js';
import { isDesktop } from './ui/dom.js';
import { showBar } from './ui/bar.js';
import { closeSheet, isSheetOpen } from './ui/sheet.js';
import { markOpenRow } from './ui/row.js';
import { listView } from './views/list.js';
import { articleView, readerEmpty } from './views/article.js';
import { savedView } from './views/saved.js';
import { searchView } from './views/search.js';
import { sourceView } from './views/sourceView.js';
import { settingsView } from './views/settings.js';
import { sourcesView } from './views/sources.js';
import { openProfiles } from './views/profiles.js';
import { profileEditorView } from './views/profileEditor.js';
import { loginView } from './views/login.js';
import { offlineView } from './views/offline.js';
import { installKeyboard } from './views/desktop.js';
import { parseHash } from './route.js';

const shell = document.getElementById('shell');
const viewEl = document.getElementById('view');
const readerEl = document.getElementById('reader');

const LIST_VIEWS = { list: listView, source: sourceView, search: searchView, saved: savedView };
const PAGE_VIEWS = { settings: settingsView, sources: sourcesView, editor: profileEditorView };

let current = { key: null, view: null };   // Inhalt von #view
let reader = null;                          // offene Leseansicht in #reader
let listScroll = 0;                         // Scrollposition der Liste (Handy)
let openedId = null;                        // zuletzt geöffneter Artikel (Handy: Fokus zurück auf seine Zeile)
let afterLogin = null;                      // Ziel nach dem Anmelden
let afterLoginUser = null;                  // Benutzer-ID, zu der afterLogin gehört

// ---- Routen (parseHash in route.js) -------------------------------------------------

function mount(route, factory) {
  current.view?.destroy?.();
  const view = factory(route);
  current = { key: route.key, view };
  viewEl.replaceChildren(view.el);
  if (view.layout === 'list') state.lastListHash = route.hash;
  return view;
}

/** Barrierefreiheit: nach einem Ansichtswechsel den Fokus auf #view setzen (Screenreader lesen
 *  die neue Ansicht, Tab beginnt dort). Nicht bei offenem Blatt und nicht, wenn die Ansicht den
 *  Fokus schon selbst gesetzt hat (z. B. Suchfeld, Anmeldung). */
function focusView(view) {
  if (isSheetOpen() || view.el.contains(document.activeElement)) return;
  viewEl.focus({ preventScroll: true });
}

/** Für Leseansicht und Profil-Blatt: links muss eine Liste stehen. */
function ensureList() {
  if (current.view && current.view.layout === 'list') return current.view;
  const r = parseHash(state.lastListHash);
  return mount(LIST_VIEWS[r.name] ? r : parseHash('#/'), LIST_VIEWS[r.name] || listView);
}

function setLayout(layout, bar) {
  shell.dataset.layout = layout;
  showBar(!!bar);
}

function closeReader() {
  reader?.destroy();
  reader = null;
  state.openArticleId = null;
  markOpenRow(null);
  readerEl.replaceChildren(readerEmpty());
}

function setTitle(view) {
  document.title = view && view.title && view.title !== 'Kiosk' ? `${view.title} · Kiosk` : 'Kiosk';
}

function showLogin() {
  if (current.key === 'login') return;
  closeSheet({ silent: true });
  closeReader();
  const r = parseHash(location.hash);
  if (r.name !== 'login') {
    afterLogin = location.hash || '#/';
    // Die Adresse gehört dem bisherigen Benutzer (null = niemand war angemeldet).
    afterLoginUser = state.user ? state.user.id : null;
  }
  const view = mount({ key: 'login' }, () => loginView({ onSuccess: loggedIn }));
  setLayout('page', false);
  setTitle(view);
  view.onShow?.();
}

/** Ziel nach dem Anmelden. Profil-Adressen (#/profile/<id>, #/?p=<id>) gehören evtl. einem anderen
 *  Benutzer (Profil-IDs sind je Benutzer): dann lieber zur Startliste. */
function loginTarget(hash) {
  if (!hash || hash.startsWith('#/login')) return '#/';
  const r = parseHash(hash);
  if (r.name === 'editor' || (r.name === 'list' && r.params.get('p'))) return '#/';
  return hash;
}

async function loggedIn() {
  const saved = afterLogin;
  const savedUser = afterLoginUser;
  resetUserState();
  try { await loadSession(); } catch { /* Benutzer unbekannt: Admin-Funktionen bleiben aus */ }
  state.authed = true;
  await loadBasics();
  current = { key: null, view: null };
  afterLogin = null;
  afterLoginUser = null;
  // Die gemerkte Adresse gilt für denselben Benutzer und beim Kaltstart ohne vorherige Sitzung
  // (Lesezeichen wie #/s/taz); wer sich als jemand anderes anmeldet, beginnt auf der Startliste.
  const same = savedUser === null || (state.user && state.user.id === savedUser);
  replaceHash(same ? loginTarget(saved) : '#/');
}

let routedHash = null;   // Adresse des letzten route()-Aufrufs

function route() {
  if (location.hash !== routedHash) state.prevHash = routedHash;
  routedHash = location.hash;
  const r = parseHash(location.hash);
  if (state.authed === null) return;           // Start läuft noch
  if (!state.authed) { showLogin(); return; }
  if (r.name === 'login') { replaceHash('#/'); return; }

  if (r.name !== 'profiles' && isSheetOpen()) closeSheet({ silent: true });
  const wasArticle = shell.dataset.layout === 'article';

  if (r.name === 'article') {
    const list = ensureList();
    if (!wasArticle) listScroll = window.scrollY;
    reader?.destroy();
    reader = articleView(r.id);
    readerEl.replaceChildren(reader.el);
    setLayout('article', isDesktop() && list.bar);
    openedId = Number(r.id);
    if (isDesktop()) readerEl.scrollTop = 0;
    else {
      window.scrollTo(0, 0);
      // Handy: Die Liste ist ausgeblendet, der Fokus läge sonst auf der unsichtbaren Zeile.
      readerEl.focus({ preventScroll: true });
    }
    setTitle({ title: 'Artikel' });
    return;
  }

  closeReader();
  if (r.name === 'profiles') {
    const list = ensureList();
    setLayout('list', list.bar);
    setTitle({ title: 'Profile' });
    openProfiles(r);
    return;
  }

  const factory = LIST_VIEWS[r.name] || PAGE_VIEWS[r.name] || listView;
  const fresh = current.key !== r.key;
  const view = fresh ? mount(r, factory) : current.view;
  if (!fresh && view.layout === 'list') state.lastListHash = r.hash;
  setLayout(view.layout, view.bar);
  setTitle(view);
  if (fresh) {
    window.scrollTo(0, 0);
    viewEl.scrollTop = 0;
  } else if (wasArticle && !isDesktop()) {
    // Zurück zur (wiederverwendeten) Liste: Scrollstand wiederherstellen, dann Fokus auf die Zeile
    // des zuletzt geöffneten Artikels (fehlt sie, auf die Liste), damit Tastatur und Vorleser dort
    // weitermachen.
    const id = openedId;
    requestAnimationFrame(() => {
      window.scrollTo(0, listScroll);
      if (current.view !== view || isSheetOpen()) return;
      const row = id != null ? viewEl.querySelector(`.row[data-id="${id}"]`) : null;
      (row || viewEl).focus({ preventScroll: true });
      // Nach mehrfach „Nächster“ kann die Zeile außerhalb des Bildes liegen: dann heranholen.
      if (row) {
        const r = row.getBoundingClientRect();
        if (r.top < 0 || r.bottom > window.innerHeight) row.scrollIntoView({ block: 'nearest' });
      }
    });
  }
  view.onShow?.();
  if (fresh) focusView(view);
}

// ---- Start -------------------------------------------------------------------------

function tagHistory() {
  // Neue Einträge (state null) stammen aus der App; der erste Eintrag ist mit kiosk:false markiert.
  if (history.state === null) history.replaceState({ kiosk: true }, '');
}

let unreadTimer = 0;
function refreshCounters() {
  clearTimeout(unreadTimer);
  unreadTimer = setTimeout(() => {
    loadBar().catch(() => {});
    // Danach Leiste und Zeitungskopf nachziehen: Beide lesen auch Zahlen aus /api/sources.
    loadSources().then(() => emit('bar')).catch(() => {});
  }, 800);
}

/** Einmal kurz nach der nächsten lokalen Mitternacht die Zähler neu holen, dann für den Tag darauf.
 *  Ein einzelner Zeitgeber pro Tag, kein Abfragen; im Hintergrund übernimmt visibilitychange. */
function scheduleMidnightRefresh() {
  const now = new Date();
  const next = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 0, 0, 5);
  setTimeout(() => {
    if (state.authed) refreshCounters();
    scheduleMidnightRefresh();
  }, next - now);
}

/** Service Worker einmal beim Start registrieren, auch ohne Anmeldung (Version aus /api/build). */
async function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  let build = '';
  try { build = ((await api('build')) || {}).build || ''; } catch { /* offline: bisherige Version bleibt */ return; }
  navigator.serviceWorker.register(`/sw.js?v=${encodeURIComponent(build || 'dev')}`).catch(() => {});
}

/** Server nicht erreichbar oder gestört (kein 401): Hinweis mit „Erneut versuchen“ statt Anmeldung. */
function showOffline(status = 0) {
  closeSheet({ silent: true });
  closeReader();
  const view = mount({ key: 'offline' }, () => offlineView({ onRetry: start, status }));
  setLayout('page', false);
  setTitle(view);
}

async function start() {
  let session;
  try {
    session = await api('session');
  } catch (err) {
    // Nur „nicht angemeldet“ führt zur Anmeldung; keine Verbindung oder Serverfehler → Offline.
    if (err.status !== 401) { state.authed = null; showOffline(err.status); return; }
    session = { authenticated: false };
  }
  setSession(session);
  if (state.authed) await loadBasics();
  current = { key: null, view: null };
  route();
}

/** Links auf die aktuelle Adresse lösen kein hashchange aus: dann selbst neu routen
 *  (z. B. „+“ in der Leiste, wenn #/profile schon in der Adresse steht). */
function sameHashLinks(e) {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  const a = e.target instanceof Element ? e.target.closest('a[href^="#"]') : null;
  if (!a || a.target) return;
  if (a.getAttribute('href') !== location.hash) return;
  e.preventDefault();
  route();
}

async function boot() {
  applySettings();
  history.replaceState({ kiosk: !!(history.state && history.state.kiosk) }, '');
  setUnauthorizedHandler(() => {
    if (state.authed === false) return;
    state.authed = false;
    showLogin();
  });
  installKeyboard();
  window.addEventListener('hashchange', () => { tagHistory(); route(); });
  document.addEventListener('click', sameHashLinks);
  // Zurück auf einen Eintrag mit derselben Adresse: kein hashchange, trotzdem neu routen.
  window.addEventListener('popstate', () => { if (location.hash === routedHash) route(); });
  on('reroute', route);
  on('unread-changed', refreshCounters);
  on('flags', (patch) => { if ('read' in patch) refreshCounters(); });
  // Die Zähler zählen nur heute (ab 00:00): Kommt die App wieder in den Vordergrund, etwa am
  // nächsten Morgen, Leiste und Zeitungen neu holen; bleibt sie offen, kurz nach Mitternacht.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && state.authed) refreshCounters();
  });
  scheduleMidnightRefresh();
  window.matchMedia('(min-width: 900px)').addEventListener('change', () => {
    const list = current.view && current.view.layout === 'list';
    const article = shell.dataset.layout === 'article';
    showBar(!!(list && current.view.bar && (!article || isDesktop())));
  });

  registerServiceWorker();
  await start();
}

boot();
