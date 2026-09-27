// Tagesübersicht (E3DesktopUebersicht): rechte Seite am Desktop ohne offenen Artikel. Zähler,
// MEISTBERICHTET (Geschichten mit den meisten Zeitungen heute), VERLAUF (Artikel je Stunde als
// Punktmatrix), PROFILE mit Ungelesen-Zahlen, Tastenhinweise. Daten aus GET /api/overview.
import { h } from '../ui/dom.js';
import { chevronRight } from '../ui/icons.js';
import { clockTime, formatCount, histogramLevels, longDate, sourcesLine } from '../format.js';
import { state } from '../store.js';
import { keyHints } from './desktop.js';

const HIST_ROWS = 6;          // Punkte je Stunde
const AXIS = [0, 6, 12, 18];  // Beschriftung der Stunden

/** Abschnittskopf: Punktmatrix-Titel links, Mono-Beschriftung rechts. */
export function homeHead(title, caption) {
  return h('div', { class: 'home-head' },
    h('h3', { class: 'home-sec', text: title }),
    caption ? h('span', { class: 't-caption muted', text: caption }) : null);
}

function tile(value, label, signal = false) {
  const n = typeof value === 'number' ? value : null;
  return h('div', { class: 'home-tile' },
    h('span', { class: `home-tile-num${signal ? ' is-signal' : ''}`, text: n === null ? '–' : formatCount(n) }),
    h('span', { class: 't-caption muted', text: label }));
}

function topStory(story) {
  const a = story.article || {};
  const n = Number(story.sources_count) || 0;
  return h('a', { class: 'home-story', href: `#/a/${a.id}` },
    h('span', { class: 'home-story-num', 'aria-hidden': 'true', text: n }),
    h('span', { class: 'home-story-text' },
      h('span', { class: 'home-story-title', text: a.title || '(ohne Titel)' }),
      h('span', { class: 't-caption muted' },
        h('span', { class: 'visually-hidden', text: `${n} Zeitungen: ` }),
        sourcesLine(story.sources, n))));
}

/** Punkt-Histogramm der Artikel je Stunde: vor jetzt ink, jetzt Signal, danach nur Rasterpunkte. */
function histogram(hourly, nowHour) {
  const counts = Array.from({ length: 24 }, (_, i) => (Array.isArray(hourly) ? hourly[i] : 0) || 0);
  const levels = histogramLevels(counts, HIST_ROWS);
  const total = counts.reduce((a, b) => a + b, 0);
  const peak = counts.indexOf(Math.max(...counts));
  const now = Number.isInteger(nowHour) ? nowHour : new Date().getHours();
  const label = total
    ? `Artikel pro Stunde heute: insgesamt ${total}, am meisten um ${peak} Uhr (${counts[peak]}), jetzt ${now} Uhr: ${counts[now] || 0}`
    : 'Artikel pro Stunde heute: noch keine';
  const cols = levels.map((level, hour) => {
    const kind = hour < now ? 'past' : hour === now ? 'now' : 'later';
    return h('span', { class: `hist-col hist-col--${kind}` },
      Array.from({ length: HIST_ROWS }, (_, i) => h('span', { class: HIST_ROWS - i <= level ? 'on' : '' })));
  });
  return h('div', { class: 'hist' },
    h('div', { class: 'hist-grid', role: 'img', 'aria-label': label }, cols),
    h('div', { class: 'hist-axis', 'aria-hidden': 'true' },
      AXIS.map((hour) => h('span', { class: 't-caption muted', style: { gridColumnStart: String(hour + 1) }, text: hour }))));
}

function profileRow(p) {
  const n = typeof p.unread === 'number' ? p.unread : null;
  return h('a', { class: 'home-profile', href: `#/?p=${encodeURIComponent(p.id)}` },
    h('span', { class: 'home-profile-name', text: p.name }),
    n !== null ? h('span', { class: 'home-profile-num', text: n > 999 ? '999+' : n }) : null,
    h('span', { class: 'chev' }, chevronRight(16)));
}

/** Kopf der Tagesübersicht: HEUTE, Datum (vom Server, sonst now) und Uhr. */
export function overviewHeader(date, now = new Date()) {
  return h('header', { class: 'home-top' },
    h('h2', { class: 'home-title', text: 'HEUTE' }),
    h('span', { class: 't-caption muted' },
      `${longDate(date || now)} / `, h('span', { class: 'home-clock', text: clockTime(now) })));
}

export function overviewContent(data, now = new Date()) {
  const counts = data.counts || {};
  // Teilantworten: Einträge ohne Artikel-ID fallen weg.
  const stories = (Array.isArray(data.top_stories) ? data.top_stories : [])
    .filter((s) => s && s.article && Number.isInteger(s.article.id));
  const profiles = (state.profiles || []).filter((p) => p && p.id != null);

  const top = h('section', { class: 'home-col home-col--main' },
    // Kurz nach Mitternacht zählt der Server die letzten 24 Stunden (window: '24h').
    homeHead('MEISTBERICHTET', data.window === '24h' ? 'Letzte 24 Std' : 'Zeitungen'),
    stories.length
      ? h('div', { class: 'home-stories' }, stories.map(topStory))
      : h('p', { class: 'home-none t-teaser muted', text: 'Heute berichten noch nicht drei Zeitungen über dasselbe Thema.' }));
  const side = h('div', { class: 'home-col home-col--side' },
    h('section', {}, homeHead('VERLAUF', 'Artikel pro Stunde'), histogram(data.hourly, data.now_hour)),
    profiles.length ? h('section', {},
      homeHead('PROFILE', 'Ungelesen'),
      h('div', { class: 'home-profiles' }, profiles.map(profileRow))) : null);

  return h('div', { class: 'home-page home-page--overview' },
    overviewHeader(data.date, now),
    h('div', { class: 'home-tiles' },
      tile(counts.new_today, 'Neu heute'),
      tile(counts.unread_today, 'Ungelesen', true),
      tile(counts.later, 'Später lesen'),
      tile(counts.active_sources, 'Zeitungen aktiv')),
    h('div', { class: 'home-cols' }, top, side),
    keyHints('home-keys'));
}
