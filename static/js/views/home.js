// Rechte Seite am Desktop ohne offenen Artikel (#reader): Tagesübersicht oder Titelseite, je nach
// Einstellung desktop_empty. Geladen wird nur, wenn die Seite wirklich zu sehen ist (Desktop,
// Listen-Layout, kein Artikel offen): beim Einblenden, nach Lese-Änderungen (gebündelt), wenn die App
// wieder in den Vordergrund kommt, wenn die Einstellung wechselt, Profile sich ändern und wenn in der
// Zeitzone des Servers ein neuer Tag beginnt. Solange sie zu sehen ist, geht die Uhr im Kopf jede
// Minute weiter (ein Zeitgeber, kein Abfragen des Servers).
import { h, isDesktop, replace } from '../ui/dom.js';
import { matrix } from '../ui/empty.js';
import { api } from '../api.js';
import { clockTime } from '../format.js';
import { on, state } from '../store.js';
import { overviewContent, overviewHeader } from './overview.js';
import { frontpageContent, frontpageHeader } from './frontpage.js';
import { keyHints } from './desktop.js';

const REFRESH_MS = 800;   // wie refreshCounters in app.js
const RECENT_MS = 1000;   // Mindestabstand zweier Ladevorgänge (ein geplantes wartet so lange)
const LABEL = { overview: 'Übersicht', frontpage: 'Titelseite' };

const kindOf = () => (state.settings.desktop_empty === 'frontpage' ? 'frontpage' : 'overview');
const pad = (n) => String(n).padStart(2, '0');
const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

/** Kopf wie bei gezeigten Daten (Datum und Uhr in der Zeitzone des Servers). */
function header(kind, now) {
  return kind === 'frontpage' ? frontpageHeader(now) : overviewHeader(null, now);
}

function loading(kind, now) {
  return h('div', { class: 'home-page' },
    header(kind, now),
    h('div', { class: 'reader-empty home-loading' },
      matrix(), h('p', { class: 't-caption muted', text: 'Lädt' })));
}

/** Fehler ohne gezeigte Daten: Kopf wie sonst, darunter ein kurzer Hinweis und die Tastenhinweise. */
function unreachable(kind, now) {
  return h('div', { class: 'home-page' },
    header(kind, now),
    h('div', { class: 'reader-empty home-error' },
      h('p', { class: 't-teaser muted', text: `${LABEL[kind]} gerade nicht erreichbar` }),
      keyHints()));
}

/**
 * Stunden zwischen der Zeitzone des Servers (KIOSK_TZ, aus now_hour) und dem Browser. Liegt die
 * Anfrage über einem Stundenwechsel, zählt die Stunde vom Anfang oder Ende, die passt.
 */
function serverOffset(nowHour, started, ended) {
  if (!Number.isInteger(nowHour)) return null;
  if (nowHour === started.getHours() || nowHour === ended.getHours()) return 0;
  let diff = (nowHour - ended.getHours() + 24) % 24;
  if (diff > 12) diff -= 24;
  return diff;
}

export function readerHome() {
  const el = h('div', { class: 'home' });
  let visible = false;
  let shownKind = null;    // was gerade gezeigt wird ('overview', 'frontpage' oder null = nichts)
  let shownDay = null;     // Tag (Zeitzone des Servers), für den die Daten gelten
  let shownHour = null;    // Stunde (Server), mit der gezeichnet wurde: VERLAUF markiert sie
  let offsetH = 0;         // Server-Zeitzone minus Browser, in Stunden (aus now_hour)
  let offsetKnown = false;
  let ctrl = null;
  let timer = 0;
  let tick = 0;
  let lastStart = 0;       // Beginn des letzten Ladens (Date.now())

  /** Jetzt in der Zeitzone des Servers (als Datum mit verschobener Ortszeit). */
  const serverNow = () => new Date(Date.now() + offsetH * 3600000);

  function label() {
    return LABEL[kindOf()];
  }

  function paintClock() {
    for (const c of el.querySelectorAll('.home-clock')) c.textContent = clockTime(serverNow());
  }

  /** Minütlich: Uhr weiterstellen; beginnt beim Server eine neue Stunde (VERLAUF) oder ein neuer
   *  Tag, neu laden. */
  function startTick() {
    clearTimeout(tick);
    if (!visible) return;
    const now = new Date();
    tick = setTimeout(() => {
      if (!visible) return;
      const now = serverNow();
      if ((shownDay && isoDay(now) !== shownDay) || (shownHour !== null && now.getHours() !== shownHour)) refresh();
      else paintClock();
      startTick();
    }, (60 - now.getSeconds()) * 1000 - now.getMilliseconds() + 50);
  }

  async function refresh() {
    clearTimeout(timer);
    if (!visible) return;
    ctrl?.abort();
    ctrl = new AbortController();
    const { signal } = ctrl;
    const kind = kindOf();
    lastStart = Date.now();
    if (el.parentElement && !el.parentElement.querySelector('.art-view')) el.parentElement.setAttribute('aria-label', LABEL[kind]);
    if (shownKind !== kind) {
      shownKind = null;
      replace(el, loading(kind, serverNow()));
      el.setAttribute('aria-busy', 'true');
    }
    try {
      let started = new Date();
      // Profile mitladen: PROFILE und die Rubriken der Titelseite zeigen deren Ungelesen-Zahlen.
      // Nach Abbruch (anderer Aufruf, Abmelden) wird nichts mehr in den Zustand geschrieben.
      const profiles = api('profiles', { signal })
        .then((list) => { if (!signal.aborted && Array.isArray(list)) state.profiles = list; })
        .catch(() => {});
      const [data] = await Promise.all([api(kind, { signal }), profiles]);
      if (signal.aborted) return;
      const d = data || {};
      let off = serverOffset(d.now_hour, started, new Date());
      if (off === null && !offsetKnown) {
        // Titelseite ohne now_hour: Zeitzone des Servers einmal aus der Übersicht holen.
        started = new Date();
        const o = await api('overview', { signal }).catch(() => null);
        if (signal.aborted) return;
        off = serverOffset(o && o.now_hour, started, new Date());
        offsetKnown = true;   // nur ein Versuch; ohne Antwort gilt die Uhr des Browsers
      }
      if (off !== null) { offsetH = off; offsetKnown = true; }
      const now = serverNow();
      shownDay = typeof d.date === 'string' ? d.date : isoDay(now);
      shownHour = now.getHours();
      el.removeAttribute('aria-busy');
      // Tastaturfokus auf einem Link überlebt das Neuzeichnen (gleiches Ziel, falls noch da).
      const focused = el.contains(document.activeElement) ? document.activeElement.getAttribute('href') : null;
      replace(el, kind === 'frontpage' ? frontpageContent(d, now) : overviewContent(d, now));
      shownKind = kind;
      if (focused) el.querySelector(`a[href="${CSS.escape(focused)}"]`)?.focus({ preventScroll: true });
    } catch (err) {
      if (signal.aborted || (err && err.name === 'AbortError')) return;
      el.removeAttribute('aria-busy');
      // Gezeigtes bleibt stehen; ohne Inhalt ein kurzer Hinweis mit den Tastenhinweisen.
      if (shownKind !== kind) { replace(el, unreachable(kind, serverNow())); shownKind = null; }
    }
  }

  function schedule() {
    if (!visible) return;
    clearTimeout(timer);
    // Hat eben erst ein Laden begonnen, kommt das nächste, sobald RECENT_MS vergangen sind (die
    // Änderung geht nicht verloren, zwei Anfragen kurz hintereinander gibt es aber nicht).
    const fire = () => {
      const wait = RECENT_MS - (Date.now() - lastStart);
      if (wait > 0) timer = setTimeout(fire, wait);
      else refresh();
    };
    timer = setTimeout(fire, REFRESH_MS);
  }

  on('unread-changed', schedule);
  on('profiles', schedule);
  on('flags', (patch) => { if ('read' in patch || 'later' in patch) schedule(); });
  on('settings', (patch) => { if ('desktop_empty' in patch) refresh(); });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && state.authed) schedule();
  });

  return {
    el,
    label,
    /** Sichtbarkeit setzen; beim Einblenden neu laden. */
    setVisible(value) {
      const next = !!value && isDesktop();
      if (next === visible) return;
      visible = next;
      if (visible) {
        refresh();
        startTick();
      } else {
        clearTimeout(timer);
        clearTimeout(tick);
        ctrl?.abort();
      }
    },
    /** Beim Benutzerwechsel: nichts vom vorigen Benutzer stehen lassen. */
    reset() {
      clearTimeout(timer);
      clearTimeout(tick);
      ctrl?.abort();
      visible = false;
      shownKind = null;
      shownDay = null;
      shownHour = null;
      replace(el);
    },
  };
}
