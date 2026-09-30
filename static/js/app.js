// Kiosk App: Hash-Router, Layout (Handy/Desktop), Anmeldung, Start.
// Zustand und Ladefunktionen liegen in store.js, Bausteine in ui/, Screens in views/.

import { api, setUnauthorizedHandler } from './api.js';
import {
  applySettings, emit, loadBar, loadBasics, loadSession, loadSources, on, replaceHash, resetUserState,
  setSession, state,
} from './store.js';
import { desktopQuery, isDesktop } from './ui/dom.js';
import { showBar } from './ui/bar.js';
import { installListResizer } from './ui/splitter.js';
import { closeSheet, isSheetOpen } from './ui/sheet.js';
import { markOpenRow } from './ui/row.js';
import { listView } from './views/list.js';
import { articleView } from './views/article.js';
import { readerHome } from './views/home.js';
import { installFocusRail, settleFocus } from './views/focus.js';
import { barWanted as layoutBarWanted, homeVisible } from './layout.js';
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
import { buildWatcher, pageBuild, startBuild } from './version.js';
import { setUpdatePillAllowed, showUpdatePill } from './ui/update.js';

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
const home = readerHome();                  // Desktop ohne Artikel: Tagesübersicht oder Titelseite

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

/** Leiste unten: nur zu Listen, mit offenem Artikel nur am Desktop und nicht im Fokusmodus. */
function barWanted() {
  return layoutBarWanted({
    viewLayout: current.view && current.view.layout, viewBar: !!(current.view && current.view.bar),
    layout: shell.dataset.layout, desktop: isDesktop(), focus: state.settings.focus_mode === true,
  });
}

/** Tagesübersicht/Titelseite laden, sobald sie zu sehen ist (Desktop, Liste, kein Artikel). */
function syncHome() {
  home.setVisible(homeVisible({
    authed: state.authed, readerOpen: !!reader, layout: shell.dataset.layout, desktop: isDesktop(),
  }));
}

function setLayout(layout) {
  shell.dataset.layout = layout;
  showBar(barWanted());
  syncHome();
  // Hinweis „Neue Version“ nur in der App, nicht auf Anmelde- oder Offline-Bildschirm.
  setUpdatePillAllowed(state.authed === true && current.key !== 'login' && current.key !== 'offline');
}

function closeReader() {
  reader?.destroy();
  reader = null;
  state.openArticleId = null;
  markOpenRow(null);
  readerEl.setAttribute('aria-label', home.label());
  if (readerEl.firstChild !== home.el || readerEl.childNodes.length !== 1) {
    readerEl.replaceChildren(home.el);
    readerEl.scrollTop = 0;   // Übersicht beginnt oben, nicht an der Stelle des eben gelesenen Artikels
  }
}

function setTitle(view) {
  document.title = view && view.title && view.title !== 'Kiosk' ? `${view.title} · Kiosk` : 'Kiosk';
}

function showLogin() {
  const r = parseHash(location.hash);
  if (current.key === 'login') {
    // Adresse auf dem Anmeldebildschirm gewechselt (z. B. geteilter Link #/a/5): danach dorthin.
    // afterLoginUser bleibt der Benutzer, dem die Sitzung zuvor gehörte.
    if (r.name !== 'login' && location.hash) afterLogin = location.hash;
    return;
  }
  closeSheet({ silent: true });
  closeReader();
  home.reset();
  if (r.name !== 'login') {
    afterLogin = location.hash || '#/';
    // Die Adresse gehört dem bisherigen Benutzer (null = niemand war angemeldet).
    afterLoginUser = state.user ? state.user.id : null;
  }
  const view = mount({ key: 'login' }, () => loginView({ onSuccess: loggedIn }));
  setLayout('page');
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
  if (r.name !== 'search') state.focusSearch = false;   // openSearch() führte nicht zur Suche
  if (state.authed === null) return;           // Start läuft noch
  if (!state.authed) { showLogin(); return; }
  if (r.name === 'login') { replaceHash('#/'); return; }

  if (r.name !== 'profiles' && isSheetOpen()) closeSheet({ silent: true });
  const wasArticle = shell.dataset.layout === 'article';

  if (r.name === 'article') {
    ensureList();
    if (!wasArticle) listScroll = window.scrollY;
    reader?.destroy();
    reader = articleView(r.id);
    readerEl.setAttribute('aria-label', 'Artikel');
    readerEl.replaceChildren(reader.el);
    setLayout('article');
    openedId = Number(r.id);
    if (isDesktop()) {
      readerEl.scrollTop = 0;
      settleFocus();   // Fokusmodus: Tastatur auf den Artikel, nicht auf die eingeklappte Liste
    } else {
      window.scrollTo(0, 0);
      // Handy: Die Liste ist ausgeblendet, der Fokus läge sonst auf der unsichtbaren Zeile.
      readerEl.focus({ preventScroll: true });
    }
    setTitle({ title: 'Artikel' });
    return;
  }

  closeReader();
  if (r.name === 'profiles') {
    ensureList();
    setLayout('list');
    setTitle({ title: 'Profile' });
    openProfiles(r);
    return;
  }

  const factory = LIST_VIEWS[r.name] || PAGE_VIEWS[r.name] || listView;
  const fresh = current.key !== r.key;
  const view = fresh ? mount(r, factory) : current.view;
  if (!fresh && view.layout === 'list') state.lastListHash = r.hash;
  // Wiederverwendete Ansicht (z. B. Zurück auf eine ältere Suche): Parameter aus der Adresse nachziehen.
  if (!fresh) view.update?.(r);
  setLayout(view.layout);
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
    // Leiste und /api/sources zusammen, dann einmal 'bar': Leiste, Zeitungskopf und Zeitungsblock
    // der Suche lesen beide Zahlen.
    Promise.allSettled([loadBar({ silent: true }), loadSources()]).then(() => emit('bar'));
  }, 800);
}

/** Einmal kurz nach der nächsten lokalen Mitternacht die Zähler neu holen und Zeiten/Tagesgruppen
 *  der Listen neu beschriften, dann für den Tag darauf. Ein einzelner Zeitgeber pro Tag, kein
 *  Abfragen; im Hintergrund übernimmt visibilitychange. */
function scheduleMidnightRefresh() {
  const now = new Date();
  const next = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 0, 0, 5);
  setTimeout(() => {
    if (state.authed) refreshCounters();
    emit('clock');
    scheduleMidnightRefresh();
  }, next - now);
}

/** Neue Version (Deploy) erkennen: Stand beim Start merken, beim Zurückkommen in den Vordergrund
 *  und alle paar Stunden nachfragen, dann die Pille „Neue Version · Neu laden“ zeigen. */
const fetchBuild = async () => ((await api('build')) || {}).build || '';
const versions = buildWatcher({ fetchBuild, onNew: () => showUpdatePill() });

/** Version, mit der der aktive Service Worker registriert wurde (?v=…): Rückfall für eine Seite ohne
 *  eigene Build-Kennung (Kopie im Cache von vor data-build), die beim Start ohne Netz aus seinem Cache kam. */
function workerBuild() {
  try {
    const url = navigator.serviceWorker && navigator.serviceWorker.controller
      ? new URL(navigator.serviceWorker.controller.scriptURL) : null;
    const v = url ? url.searchParams.get('v') : null;
    return v && v !== 'dev' ? v : null;
  } catch {
    return null;
  }
}

/** Stand des Service Workers, falls die Seite (ohne eigene Kennung) nach dem Fehler `err` von
 *  /api/build aus seinem Cache kam: offline oder Serverfehler (5xx; sw.js fällt bei Netzfehler und 5xx
 *  darauf zurück). Nur bei abgelaufener Frist kam die Seite frisch vom Netz: dann null (die erste
 *  spätere Antwort gilt), sonst meldete das ältere ?v= fälschlich eine neue Version. */
function cachedBuild(err) {
  const fromCache = navigator.onLine === false || (err && err.status >= 500)
    || (err && err.status === 0 && !err.timedOut);
  return fromCache ? workerBuild() : null;
}

/** Service Worker einmal beim Start registrieren, auch ohne Anmeldung (Version aus /api/build).
 *  Bekannt ist der Stand der Seite selbst (data-build, siehe startBuild). */
async function registerServiceWorker() {
  const build = await startBuild(versions, { own: pageBuild(document.documentElement), fetchBuild, cachedBuild });
  if (build === null || !('serviceWorker' in navigator)) return;
  navigator.serviceWorker.register(`/sw.js?v=${encodeURIComponent(build || 'dev')}`).catch(() => {});
}

/** Server nicht erreichbar oder gestört (kein 401): Hinweis mit „Erneut versuchen“ statt Anmeldung. */
function showOffline(status = 0) {
  closeSheet({ silent: true });
  closeReader();
  const view = mount({ key: 'offline' }, () => offlineView({ onRetry: start, status }));
  setLayout('page');
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
  current.view?.destroy?.();   // z. B. die Offline-Ansicht (horcht auf „online“)
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
  installFocusRail();
  installListResizer();
  window.addEventListener('hashchange', () => { tagHistory(); route(); });
  document.addEventListener('click', sameHashLinks);
  // Zurück auf einen Eintrag mit derselben Adresse: kein hashchange, trotzdem neu routen.
  window.addEventListener('popstate', () => { if (location.hash === routedHash) route(); });
  on('reroute', route);
  on('unread-changed', refreshCounters);
  on('flags', (patch) => { if ('read' in patch) refreshCounters(); });
  // Die Zähler zählen nur heute (ab 00:00): Kommt die App wieder in den Vordergrund, etwa am
  // nächsten Morgen, Leiste und Zeitungen neu holen; bleibt sie offen, kurz nach Mitternacht.
  // Relative Zeiten („5 MIN“) und Tagesgruppen veralten in einer lange offenen App: beim Zurückkommen
  // und alle 5 Minuten (nur sichtbar) neu beschriften, dazu kurz nach Mitternacht.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    if (state.authed) refreshCounters();
    emit('clock');
    versions.check();
  });
  setInterval(() => { if (document.visibilityState === 'visible') emit('clock'); }, 5 * 60 * 1000);
  setInterval(() => versions.check({ force: true }), 3 * 60 * 60 * 1000);
  window.addEventListener('online', () => versions.check({ force: true }));
  scheduleMidnightRefresh();
  desktopQuery.addEventListener('change', () => {
    showBar(barWanted());
    syncHome();
  });
  // Fokusmodus blendet die Leiste aus (nur mit offenem Artikel am Desktop, siehe barWanted).
  on('settings', (patch) => { if ('focus_mode' in patch) showBar(barWanted()); });

  registerServiceWorker();
  await start();
}

boot();
