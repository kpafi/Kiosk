// Titelseite (E3DesktopTitelseite): rechte Seite am Desktop ohne offenen Artikel, wenn in den
// Einstellungen gewählt; am Handy eine eigene Seite (#/titelseite, views/front.js). Aufmacher (größte
// Geschichte des Tages), je Profil bis zu drei Geschichten und ganz unten die KI-Ausgaben des Tages
// (Rubrik AUSGABE). Daten aus GET /api/frontpage.
import { h } from '../ui/dom.js';
import { metaParts } from '../ui/row.js';
import { cleanText, clockLabel, clockTime, readMinutes, shortDate } from '../format.js';
import { profileById, state } from '../store.js';
import { emptyState } from '../ui/empty.js';
import { homeHead } from './overview.js';
import { keyHints } from './desktop.js';

const LEAD_MIN_WIDTH = 300;   // wie HERO_MIN_WIDTH in article.js: kleinere Feed-Bilder (Vorschau) nicht hochskalieren

/** Bild wie in Liste und Leseansicht: faul, ohne Referrer, bei Fehler (oder schmaler als minWidth)
 *  weg, dann ruft es onGone. Ohne Bild oder mit „Bilder“ aus: null. */
function picture(item, cls, onGone, minWidth = 0) {
  if (!item.image_url || !state.settings.show_images) return null;
  const img = h('img', {
    class: cls, src: item.image_url, alt: '', loading: 'lazy', decoding: 'async', referrerpolicy: 'no-referrer',
  });
  const gone = () => { img.remove(); onGone?.(); };
  img.addEventListener('error', gone, { once: true });
  if (minWidth) img.addEventListener('load', () => { if (img.naturalWidth && img.naturalWidth < minWidth) gone(); }, { once: true });
  return img;
}

/** Fläche anstelle eines fehlenden Kartenbilds (Bilder an): hält die Zeile der Rubrik bündig. */
const placeholder = () => h('span', { class: 'fp-item-img fp-item-img--none', 'aria-hidden': 'true' });

function alsoIn(count) {
  const more = (Number(count) || 0) - 1;
  if (more < 1) return null;
  return h('span', { class: 'fp-also t-caption', text: more === 1 ? 'Auch in einer weiteren Zeitung' : `Auch in ${more} weiteren Zeitungen` });
}

function lead(story) {
  const a = story.article || {};
  const el = h('a', { class: 'fp-lead', href: `#/a/${a.id}` });
  const img = picture(a, 'fp-lead-img', () => el.classList.add('fp-lead--noimg'), LEAD_MIN_WIDTH);
  if (!img) el.classList.add('fp-lead--noimg');
  const summary = a.paywalled ? '' : cleanText(a.summary, a.title);
  if (img) el.append(img);
  el.append(h('span', { class: 'fp-lead-text' },
    h('span', { class: 'row-meta t-meta' }, metaParts(a)),
    h('span', { class: `fp-lead-title${a.read ? ' is-read' : ''}`, text: a.title || '(ohne Titel)' }),
    summary ? h('span', { class: 'fp-lead-summary', text: summary }) : null,
    alsoIn(story.sources_count)));
  return el;
}

function card(entry) {
  const a = entry.article || {};
  const el = h('a', { class: 'fp-item', href: `#/a/${a.id}` });
  // Bilder an: fehlt eins (oder lädt nicht), steht eine Fläche gleicher Höhe da. Bilder aus: keine.
  const fill = () => { el.classList.add('fp-item--ph'); el.prepend(placeholder()); };
  const img = picture(a, 'fp-item-img', fill);
  if (img) el.append(img);
  else if (state.settings.show_images) fill();
  else el.classList.add('fp-item--noimg');
  el.append(
    h('span', { class: 'row-meta t-meta' }, metaParts(a)),
    h('span', { class: `fp-item-title${a.read ? ' is-read' : ''}`, text: a.title || '(ohne Titel)' }));
  return el;
}

function section(sec) {
  const p = profileById(sec.profile_id);
  const n = p && typeof p.unread === 'number' ? p.unread : null;
  return h('section', { class: 'fp-sec' },
    homeHead(sec.name || (p && p.name) || 'Profil', n !== null ? `${n} ungelesen` : null),
    h('div', { class: 'fp-grid' }, (sec.items || []).map(card)));
}

const hasArticle = (e) => !!(e && e.article && Number.isInteger(e.article.id));

/** „Morgen 6 · Abend 20 Uhr“ aus edition_times (["06:00", "20:00"]); fehlt es: „Morgen und Abend“. */
export function editionCaption(times) {
  const hour = (t) => {
    const m = /^(\d{1,2}):(\d{2})$/.exec(String(t ?? ''));
    if (!m) return null;
    return m[2] === '00' ? String(Number(m[1])) : `${Number(m[1])}:${m[2]}`;
  };
  const [morning, evening] = Array.isArray(times) ? times.map(hour) : [];
  return morning && evening ? `Morgen ${morning} · Abend ${evening} Uhr` : 'Morgen und Abend';
}

/** Eine Ausgabe als Zeile wie in der Liste: Meta mit Lesezeit, Titel, zwei Zeilen Vorspann, kein Bild. */
function editionRow(a, now = new Date()) {
  const minutes = readMinutes(a);
  const teaser = cleanText(a.summary, a.title);
  return h('a', { class: 'fp-ed-row', href: `#/a/${a.id}` },
    // Uhrzeit („06:00“) statt „2 STD“: neben der Lesezeit „7 MIN“ wäre „2 MIN“ doppeldeutig.
    h('span', { class: 'row-meta t-meta' }, metaParts(a, (iso) => clockLabel(iso, now)),
      minutes > 0 ? h('span', { class: 'row-when fp-ed-min', text: `/ ${minutes} MIN` }) : null),
    h('span', { class: `fp-ed-title${a.read ? ' is-read' : ''}`, text: a.title || '(ohne Titel)' }),
    teaser ? h('span', { class: 'fp-ed-teaser', text: teaser }) : null);
}

/** Rubrik AUSGABE (KI-Ausgaben des Tages, neueste zuerst); ohne Ausgaben null. */
export function editionsSection(data, now = new Date()) {
  const items = editionsOf(data || {});
  if (!items.length) return null;
  return h('section', { class: 'fp-sec fp-ed' },
    homeHead('AUSGABE', editionCaption(data.edition_times)),
    h('div', { class: 'fp-ed-list' }, items.map((a) => editionRow(a, now))));
}

/** Ausgaben mit gültiger Artikel-ID (Teilantworten: der Rest fällt weg). */
function editionsOf(data) {
  return (Array.isArray(data.editions) ? data.editions : []).filter((a) => a && Number.isInteger(a.id));
}

/** Kopf der Titelseite: TITELSEITE, Kurzdatum, Uhr, „aus deinen Profilen“ (window '24h': kurz nach
 *  Mitternacht zeigt der Server die letzten 24 Stunden, dann „Letzte 24 Std“). */
export function frontpageHeader(now = new Date(), range = 'today') {
  return h('header', { class: 'home-top' },
    h('h2', { class: 'home-title', text: 'TITELSEITE' }),
    h('span', { class: 't-caption muted' },
      `${shortDate(now)} / `, h('span', { class: 'home-clock', text: clockTime(now) }), range === '24h' ? ' / Letzte 24 Std' : ' / aus deinen Profilen'));
}

/** Was die Titelseite zeigt. Teilantworten: Einträge ohne Artikel-ID und leere Rubriken fallen weg. */
function shown(data) {
  const sections = (Array.isArray(data.sections) ? data.sections : [])
    .filter((s) => s && Array.isArray(s.items))
    .map((s) => ({ ...s, items: s.items.filter(hasArticle) }))
    .filter((s) => s.items.length);
  return { leadStory: hasArticle(data.lead) ? data.lead : null, sections, editions: editionsOf(data) };
}

/** Artikel der Titelseite in Anzeigereihenfolge (Aufmacher, die Rubriken, zuletzt die Ausgaben), jeder
 *  nur einmal: am Handy die Liste für „Nächster“ und die sofortige Vorschau (state.listItems). */
export function frontpageArticles(data) {
  const { leadStory, sections, editions } = shown(data || {});
  const seen = new Set();
  const out = [];
  for (const a of [leadStory?.article, ...sections.flatMap((s) => s.items.map((e) => e.article)), ...editions]) {
    if (!a || seen.has(a.id)) continue;
    seen.add(a.id);
    out.push(a);
  }
  return out;
}

/** keys: false lässt die Tastenhinweise im Leerzustand weg (Handy). */
export function frontpageContent(data, now = new Date(), { keys = true } = {}) {
  const { leadStory, sections, editions } = shown(data);
  const top = frontpageHeader(now, data.window);
  if (!leadStory && !sections.length && !editions.length) {
    const empty = emptyState({ title: 'NOCH LEER', text: 'Für die Titelseite gibt es heute noch keine Geschichten aus deinen Profilen.' });
    if (keys) empty.append(keyHints());
    return h('div', { class: 'home-page home-page--front' }, top, empty);
  }
  return h('div', { class: 'home-page home-page--front' },
    top,
    leadStory ? lead(leadStory) : null,
    sections.map(section),
    editionsSection(data, now));
}
