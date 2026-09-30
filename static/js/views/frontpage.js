// Titelseite (E3DesktopTitelseite): rechte Seite am Desktop ohne offenen Artikel, wenn in den
// Einstellungen gewählt; am Handy eine eigene Seite (#/titelseite, views/front.js). Aufmacher (größte
// Geschichte des Tages) und je Profil bis zu drei Geschichten. Daten aus GET /api/frontpage.
import { h } from '../ui/dom.js';
import { metaParts } from '../ui/row.js';
import { cleanText, clockTime, shortDate } from '../format.js';
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
  return { leadStory: hasArticle(data.lead) ? data.lead : null, sections };
}

/** Artikel der Titelseite in Anzeigereihenfolge (Aufmacher, dann die Rubriken), jeder nur einmal:
 *  am Handy die Liste für „Nächster“ und die sofortige Vorschau (state.listItems). */
export function frontpageArticles(data) {
  const { leadStory, sections } = shown(data || {});
  const seen = new Set();
  const out = [];
  for (const e of [leadStory, ...sections.flatMap((s) => s.items)]) {
    if (!e || seen.has(e.article.id)) continue;
    seen.add(e.article.id);
    out.push(e.article);
  }
  return out;
}

/** keys: false lässt die Tastenhinweise im Leerzustand weg (Handy). */
export function frontpageContent(data, now = new Date(), { keys = true } = {}) {
  const { leadStory, sections } = shown(data);
  const top = frontpageHeader(now, data.window);
  if (!leadStory && !sections.length) {
    const empty = emptyState({ title: 'NOCH LEER', text: 'Für die Titelseite gibt es heute noch keine Geschichten aus deinen Profilen.' });
    if (keys) empty.append(keyHints());
    return h('div', { class: 'home-page home-page--front' }, top, empty);
  }
  return h('div', { class: 'home-page home-page--front' },
    top,
    leadStory ? lead(leadStory) : null,
    sections.map(section));
}
