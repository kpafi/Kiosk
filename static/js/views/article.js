// Leseansicht (E2Lese, rechts in E2Desktop): Kopf mit Flags, Meta, Titel, Bild, Volltext, Nächster.
import { h, hint, isDesktop, replace } from '../ui/dom.js';
import { arrowRight, back, bookmark, checkCheck, external, star } from '../ui/icons.js';
import { iconButton } from '../ui/header.js';
import { markOpenRow } from '../ui/row.js';
import {
  articleTime, cleanText, isSmallImageUrl, listTime, readMinutes, sameImage,
} from '../format.js';
import { api } from '../api.js';
import {
  emit, goBack, on, replaceHash, setFlag, sourceLabel, state,
} from '../store.js';
import { toast } from '../ui/dom.js';

const NOTE = {
  partial: 'Nur Anfang verfügbar',
  empty: 'Kein Text, Original öffnen',
  failed: 'Kein Text, Original öffnen',
};

/** Parameter für /next im aktuellen Listen-Kontext. */
function nextParams() {
  const ctx = state.nextCtx || {};
  return { unread: 'true', ...ctx };
}

/** Für Listen ohne Server-Kontext (nextCtx 'local'): nächster ungelesener aus der geladenen Liste. */
function nextFromList(id) {
  const list = state.listItems || [];
  const idx = list.findIndex((it) => it.id === id);
  const rest = idx === -1 ? list : list.slice(idx + 1);
  return rest.find((it) => !it.read && it.id !== id) || null;
}

const POLL_MS = 3000;       // Text wird im Hintergrund geholt: so oft nachfragen …
const POLL_MAX_MS = 30000;  // … und so lange höchstens

/** Listen-Objekt aus der geladenen Liste (für die sofortige Vorschau), sonst null. */
function cachedItem(id) {
  return (state.listItems || []).find((it) => it.id === id) || null;
}

/** Gemerkt-Listen zeigen auch Bezahlartikel; alle anderen blenden sie aus. */
function listShowsPaywalled() {
  const ctx = state.nextCtx;
  return !!(ctx && typeof ctx === 'object' && (ctx.starred || ctx.later));
}

/** Artikel hat sich beim Holen als Bezahlartikel erwiesen: aus der aktuellen Liste nehmen
 *  (Gemerkt: nur den Anreißer) und die Zähler neu laden. */
function dropPaywalledRow(id) {
  const keep = listShowsPaywalled();
  for (const row of document.querySelectorAll(`#view .row[data-id="${id}"]`)) {
    if (keep) { row.querySelector('.row-teaser')?.remove(); continue; }
    const prev = row.previousElementSibling;
    const next = row.nextElementSibling;
    row.remove();
    // Tagesüberschrift ohne Zeilen darunter entfällt.
    if (prev && prev.matches('h2.day') && !(next && next.matches('.row'))) prev.remove();
  }
  const list = state.listItems || [];
  const idx = list.findIndex((it) => it.id === id);
  if (idx !== -1) {
    if (keep) Object.assign(list[idx], { paywalled: 1, summary: '' });
    else list.splice(idx, 1);
  }
  emit('unread-changed');
}

// Gleiche Regel wie CAPTION_RE in app/extract.py.
const CAPTION_RE = /^\s*(?:Fotos?|Bilder?|Quelle|Photo|Image)\s*:/i;
// Bildunterschrift, die mit der Bildquelle endet („… Foto: dpa“, „… Bild: AP“).
const CREDIT_END_RE = /(?:Fotos?|Bilder?)\s*:\s*[^:]{1,80}$/i;
const CREDIT_START_RE = /^\s*(?:Fotos?|Bilder?)\s*:/i;
const CAPTION_MAX = 160;   // längere Absätze sind Artikeltext
const CREDIT_MAX = 80;     // Absatz, der nach einem losen „Foto:“ nur die Quelle nennt
const SHORT_P_MAX = 40;    // Rubrik-/Leerabsatz („War“, „Prefer“): kürzer als 40 Zeichen
// Bildzeile direkt unter dem Titelbild auch ohne „Foto:“, wenn sie kurz ist und eine Bildquelle nennt
// („… Teresa Suarez/AP POOL“). Nur ganze Wörter, damit z. B. „Kapitel“ nicht als „AP“ zählt.
const HERO_CREDIT_RE = /\b(?:AP|dpa|Reuters|Getty|AFP|EPA|Bloomberg|imago|POOL|Photo|Foto|Bild)\b/i;
const HERO_CAPTION_MAX = 200;
const HERO_MIN_WIDTH = 300;   // kleineres Feed-Bild (Vorschau) wird nicht hochskaliert, sondern weggelassen

/** Leerer oder kurzer Rubrik-Absatz ohne Bild. */
function isFiller(node) {
  return node.tagName === 'P' && !node.querySelector('img') && (node.textContent || '').trim().length < SHORT_P_MAX;
}

/** Absatz direkt nach einem Bild (auch nach dem Absatz/Link, der nur das Bild umschließt). */
function paragraphAfter(node) {
  let next = node.nextElementSibling;
  if (!next && node.parentElement && node.parentElement.tagName !== 'DIV') next = node.parentElement.nextElementSibling;
  return next && next.tagName === 'P' ? next : null;
}

/** Kurzer Absatz ohne Bild, der eine Bildquelle nennt (Bildzeile unter dem Titelbild). */
function isCreditCaption(p) {
  const text = (p.textContent || '').replace(/\s+/g, ' ').trim();
  return !p.querySelector('img') && text.length < HERO_CAPTION_MAX && HERO_CREDIT_RE.test(text);
}

/** Absatz direkt nach einem Bild, der nur die Bildquelle nennt („Foto: …“). */
function captionAfter(node) {
  const next = paragraphAfter(node);
  return next && CAPTION_RE.test(next.textContent || '') ? next : null;
}

/** Kurzer Absatz nach einem Bild, der mit der Bildquelle endet oder dem sie lose folgt (Spiegel:
 *  „<p>Bildtext</p>Foto: dpa“ bzw. „<p>Bildtext</p>Foto:<p>dpa</p>“), wird als Bildzeile gesetzt;
 *  die lose Quelle kommt mit hinein. */
function markCaption(img) {
  const p = paragraphAfter(img);
  if (!p || p.querySelector('img')) return;
  const text = (p.textContent || '').trim();
  if (!text || text.length >= CAPTION_MAX) return;
  const loose = p.nextSibling && p.nextSibling.nodeType === Node.TEXT_NODE && CREDIT_START_RE.test(p.nextSibling.textContent)
    ? p.nextSibling : null;
  if (!loose && !CAPTION_RE.test(text) && !CREDIT_END_RE.test(text)) return;
  p.classList.add('art-caption');
  if (!loose) return;
  let credit = loose.textContent.trim();
  const source = p.nextElementSibling;
  if (/:$/.test(credit) && loose.nextSibling === source && source && source.tagName === 'P'
      && !source.querySelector('img') && (source.textContent || '').trim().length < CREDIT_MAX) {
    credit += ` ${source.textContent.trim()}`;
    source.remove();
  }
  p.append(` ${credit}`);
  loose.remove();
}

/** Bild samt Bildquellen-Absatz entfernen; dabei leer gewordene <figure>, <p>, <a> mitnehmen. */
function removeImage(img) {
  captionAfter(img)?.remove();
  let node = img.parentElement;
  img.remove();
  while (node && ['FIGURE', 'P', 'A'].includes(node.tagName)) {
    const empty = !node.querySelector('img') && (node.tagName === 'FIGURE' || !node.textContent.trim());
    if (!empty) break;
    const up = node.parentElement;
    node.remove();
    node = up;
  }
}

/** Bild direkt davor im selben Elternelement (nur Überschriften dazwischen, Spiegel: Autorenfoto,
 *  Überschrift, Autorenfoto) bzw. letztes Bild im direkt vorangehenden Element; ein Absatz/Link, der
 *  nur das Bild umschließt, zählt wie das Bild. */
function previousImage(img) {
  let node = img;
  while (!node.previousElementSibling && node.parentElement && ['P', 'A', 'FIGURE'].includes(node.parentElement.tagName)
    && !node.parentElement.textContent.trim()) node = node.parentElement;
  let prev = node.previousElementSibling;
  while (prev && /^H[1-6]$/.test(prev.tagName)) prev = prev.previousElementSibling;
  if (!prev) return null;
  if (prev.tagName === 'IMG') return prev;
  const inner = prev.querySelectorAll('img');
  return inner.length ? inner[inner.length - 1] : null;
}

/** Dasselbe Bild in anderer Größe (Regeln bei imageKey in format.js) nur beim ersten Vorkommen zeigen.
 *  Kleine Bilder (Wappen, Autorenfotos) wiederholen sich absichtlich und bleiben, außer dasselbe Bild
 *  steht direkt davor in anderer Größe (Spiegel: Autorenfoto in 96 und 72 px). */
function dropDuplicateImages(frag) {
  const seen = [];
  for (const img of [...frag.querySelectorAll('img')]) {
    const src = img.getAttribute('src');
    if (isSmallImageUrl(src)) {
      const prev = previousImage(img);
      const prevSrc = prev && prev.getAttribute('src');
      if (prevSrc && prevSrc !== src && sameImage(prevSrc, src)) removeImage(img);
      continue;
    }
    if (seen.some((s) => sameImage(s, src))) removeImage(img);
    else seen.push(src);
  }
}

/** Kleines Bild (Autorenfoto, Symbol): laut URL oder width/height unter 200 px oder Alt-Text = Autor. */
function isSmallImage(img, author) {
  const dim = (name) => Number(img.getAttribute(name)) || 0;
  if (isSmallImageUrl(img.getAttribute('src'))) return true;
  if ((dim('width') && dim('width') < 200) || (dim('height') && dim('height') < 200)) return true;
  const alt = (img.getAttribute('alt') || '').trim().toLowerCase();
  return !!author && alt === author.trim().toLowerCase();
}

// Feed-Bild als Vorschau (Guardian ?width=140, BBC /240/, n-tv /250/): Breite unter 400 px.
const LOW_RES_RE = /\/(\d{3})\/|[?&]width=(\d+)/g;   // dreistellig: keine Monats-/Tagesordner
const LOW_RES_MAX = 400;
function isLowResUrl(url) {
  if (isSmallImageUrl(url)) return true;
  for (const m of String(url || '').toLowerCase().matchAll(LOW_RES_RE)) {
    if (Number(m[1] || m[2]) < LOW_RES_MAX) return true;
  }
  return false;
}

/** Bild, mit dem der Text beginnt: erstes Element nach leeren und kurzen Rubrik-Absätzen ist <img>
 *  oder <figure> (auch ein <p>/<a> ohne Text, das nur das Bild umschließt). */
function leadingImage(bodyEl) {
  let node = bodyEl.firstElementChild;
  while (node && isFiller(node)) node = node.nextElementSibling;
  while (node && ['P', 'A'].includes(node.tagName) && !node.textContent.trim() && node.children.length === 1) {
    node = node.firstElementChild;
  }
  if (!node) return null;
  if (node.tagName === 'IMG') return node;
  return node.tagName === 'FIGURE' ? node.querySelector('img') : null;
}

const TITLE_ECHO_WITHIN = 4;   // nur unter den ersten vier Elementen
const TITLE_ECHO_EXTRA = 20;   // „Rubrik • Titel“: höchstens 20 Zeichen mehr als der Titel
const TITLE_ECHO_MIN = 12;     // „enthält den Titel“ nur bei Titeln ab 12 Zeichen
const TITLE_ECHO_PART = 0.6;   // Teil des Titels (Überschrift bzw. Kern nach „: “): mind. 60 % der Länge
const normTitle = (t) => (t || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');

/** Überschriften (H1–H3) und Absätze am Textanfang, die nur den Titel wiederholen, entfallen samt
 *  direkt davor stehender kurzer Rubrik- oder Leerabsätze. Als Wiederholung zählt eine Überschrift,
 *  die dem Titel gleicht, ihn (mit höchstens 20 Zeichen Rubrik) enthält, ein großer Teil von ihm ist
 *  oder seinen Kern nach dem letzten „: “ enthält (Feed „Österreich: Wie die FPÖ …“, Überschrift
 *  „meinung • Die Lage: Inside Austria Wie die FPÖ …“); ein Absatz nur, wenn er genau dem Titel oder
 *  dem Kern gleicht. */
function dropTitleEcho(bodyEl, title) {
  const t = normTitle(title);
  if (!t) return;
  const cut = (title || '').lastIndexOf(': ');
  const core = cut === -1 ? '' : normTitle(title.slice(cut + 2));
  const hasCore = core.length >= TITLE_ECHO_MIN && core.length >= t.length * TITLE_ECHO_PART;
  const isEcho = (el) => {
    const n = normTitle(el.textContent);
    if (!n) return false;
    if (el.tagName === 'P') return !el.querySelector('img') && (n === t || (hasCore && n === core));
    if (!/^H[1-3]$/.test(el.tagName)) return false;
    if (n === t || (hasCore && n.includes(core))) return true;
    if (t.length < TITLE_ECHO_MIN) return false;
    return (n.includes(t) && n.length - t.length <= TITLE_ECHO_EXTRA)
      || (t.includes(n) && n.length >= t.length * TITLE_ECHO_PART);
  };
  for (const echo of [...bodyEl.children].slice(0, TITLE_ECHO_WITHIN).filter(isEcho)) {
    while (echo.previousElementSibling && isFiller(echo.previousElementSibling)) echo.previousElementSibling.remove();
    echo.remove();
  }
}

/** Ein Bild am Textanfang wird Titelbild: davor übersprungene Rubrik-/Leerabsätze entfallen, und ein
 *  kurzer Absatz direkt danach, der eine Bildquelle nennt, wird als Bildzeile gesetzt. */
function adoptLeadImage(bodyEl, lead) {
  while (bodyEl.firstElementChild && isFiller(bodyEl.firstElementChild)) bodyEl.firstElementChild.remove();
  const p = paragraphAfter(lead);
  if (p && isCreditCaption(p)) p.classList.add('art-caption');
}

/**
 * Titelbild oben: { src, copies } oder null (kein Feed-Bild, oder der Text beginnt mit einem großen
 * Bild; das ist dann das Titelbild und trägt ggf. die Bildunterschrift, siehe adoptLeadImage). Sonst steht das Feed-Bild
 * oben; dasselbe Bild im Text (copies) wird entfernt, sobald das Titelbild geladen ist. Ist das
 * Feed-Bild nur eine Vorschau und steht dasselbe Bild groß im Text, wird oben die große Fassung
 * gezeigt. Kleine Bilder (Autorenfoto) werden nie Titelbild.
 */
export function planHero(bodyEl, imageUrl, author) {
  const big = (img) => !isSmallImage(img, author);
  const lead = bodyEl ? leadingImage(bodyEl) : null;
  if (lead && big(lead)) {
    adoptLeadImage(bodyEl, lead);
    return null;
  }
  if (!imageUrl) return null;
  const copies = bodyEl ? [...bodyEl.querySelectorAll('img')].filter((i) => sameImage(i.getAttribute('src'), imageUrl)) : [];
  const large = isLowResUrl(imageUrl) ? copies.find(big) : null;
  return { src: large ? large.getAttribute('src') : imageUrl, copies };
}

/**
 * Artikeltext aus body_html. Erst in einem <template> aufbereiten (dort laden Bilder noch nicht),
 * dann einfügen: Links in neuem Tab, Bilder faul und ohne Referrer oder (show_images aus) entfernt.
 * Einzige HTML-Stelle: body_html ist serverseitig auf eine Whitelist bereinigt.
 */
export function buildBody(html, title) {
  const tpl = document.createElement('template');
  tpl.innerHTML = html;
  const frag = tpl.content;
  dropDuplicateImages(frag);
  for (const a of frag.querySelectorAll('a')) {
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
  }
  for (const img of frag.querySelectorAll('img')) {
    markCaption(img);
    const caption = captionAfter(img);
    if (!state.settings.show_images) {
      if (caption) caption.classList.add('art-caption');   // Bildquelle ohne Bild: als Bildzeile
      img.remove();
      continue;
    }
    img.loading = 'lazy';
    img.decoding = 'async';
    img.referrerPolicy = 'no-referrer';
    img.addEventListener('error', () => {
      if (caption) caption.classList.add('art-caption');
      img.remove();
    }, { once: true });
  }
  const bodyEl = h('div', { class: 'article-body t-article' });
  bodyEl.append(frag);
  dropTitleEcho(bodyEl, title);
  return bodyEl;
}

export function articleView(id) {
  const el = h('article', { class: 'art-view', 'aria-busy': 'true' });
  let item = null;
  let alive = true;
  let pollTimer = 0;
  let pollUntil = 0;        // Ende des Nachfragens (Zeitstempel), 0 = noch nicht begonnen
  let polling = false;      // fragt gerade nach, ob der Text da ist
  const numId = Number(id);
  // „Nächster“ nur einmal laden; die Elemente überleben ein Neuzeichnen nach dem Nachfragen.
  let nextLoaded = false;
  const onlyUnread = nextParams().unread !== 'false';
  const nextPill = h('a', { class: 'pill only-desktop', hidden: true, href: '#' },
    h('span', { text: onlyUnread ? 'Nächster ungelesener' : 'Nächster' }), arrowRight(18));
  const nextBlock = h('div', { class: 'next-block only-mobile', hidden: true });

  // ---- Kopf ----
  const btnLater = iconButton(bookmark(20), 'Später lesen', { onClick: () => toggle('later') });
  const btnStar = iconButton(star(20), 'Favorit', { onClick: () => toggle('star') });
  const btnRead = iconButton(checkCheck(20), 'Als gelesen markieren', { onClick: () => toggle('read') });
  const header = h('header', { class: 'hdr art-hdr' },
    iconButton(back(20), 'Zurück', { onClick: () => goBack(state.lastListHash), extraClass: 'only-mobile' }),
    h('div', { class: 'hdr-spacer only-mobile' }),
    btnLater, btnStar, btnRead);
  const body = h('div', { class: 'art' });
  el.append(header, body);

  function paintFlags() {
    if (!item) return;
    const set = (btn, on, labelOn, labelOff, iconFn) => {
      btn.classList.toggle('is-on', !!on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      btn.setAttribute('aria-label', on ? labelOn : labelOff);
      if (iconFn) btn.replaceChildren(iconFn(!!on));
    };
    set(btnLater, item.later, 'Später lesen entfernen', 'Später lesen', (f) => bookmark(20, f));
    set(btnStar, item.starred, 'Favorit entfernen', 'Favorit', (f) => star(20, f));
    set(btnRead, item.read, 'Als ungelesen markieren', 'Als gelesen markieren');
  }

  async function toggle(flag) {
    if (!item) return;
    const field = flag === 'star' ? 'starred' : flag;
    try {
      await setFlag(item.id, flag, !item[field]);
    } catch (err) {
      toast(`Fehler: ${err.detail || err.message}`);
    }
  }

  const offFlags = on('flags', (patch) => {
    if (!item || patch.id !== item.id) return;
    Object.assign(item, patch);
    paintFlags();
  });

  function renderBody() {
    const minutes = readMinutes(item);
    const meta = h('div', { class: 'art-meta t-meta' },
      h('span', { class: 'src', text: sourceLabel(item.source_id, item.source_name) }),
      ` / ${item.country || '?'} / ${articleTime(item.published_at)}`,
      minutes ? ` / ${minutes} Min Lesezeit` : '');
    const title = h('h1', { class: 'art-title', text: item.title || '(ohne Titel)' });

    const parts = [meta, title];
    const author = cleanText(item.author);
    if (author) parts.push(h('div', { class: 'art-author t-caption', text: author }));

    let bodyEl = null;
    if (item.paywalled) {
      parts.push(h('div', { class: 'art-note t-caption', text: 'Bezahlartikel werden nicht angezeigt' }));
    } else if (item.body_html) {
      bodyEl = buildBody(item.body_html, item.title);
    }

    const plan = state.settings.show_images && !item.paywalled ? planHero(bodyEl, item.image_url, author) : null;
    if (plan) {
      const { copies } = plan;
      const hero = h('img', {
        class: 'art-hero', src: plan.src, alt: '', decoding: 'async', referrerpolicy: 'no-referrer',
      });
      const fromFeed = () => hero.getAttribute('src') === item.image_url;
      // Wiederholung im Text erst entfernen, wenn das Titelbild wirklich geladen ist; sonst bleibt sie.
      // Alt-Text und Bildzeile der ersten Wiederholung wandern zum Titelbild (auch ein kurzer Absatz
      // mit Bildquelle ohne „Foto:“). Ein Feed-Bild unter 300 px (Vorschau) fällt weg; eine
      // Wiederholung im Text bleibt dann stehen.
      hero.addEventListener('load', () => {
        if (fromFeed() && hero.naturalWidth && hero.naturalWidth < HERO_MIN_WIDTH) {
          hero.remove();
          return;
        }
        if (!copies.length) return;
        hero.alt = copies[0].getAttribute('alt') || '';
        const caption = paragraphAfter(copies[0]);
        if (caption && (caption.classList.contains('art-caption') || isCreditCaption(caption))
            && !CAPTION_RE.test(caption.textContent || '')) {
          caption.classList.add('art-caption');
          hero.after(caption);
        }
        copies.forEach(removeImage);
      }, { once: true });
      // Große Fassung aus dem Text lädt nicht: einmal auf das Feed-Bild zurückfallen, dann aufgeben.
      hero.addEventListener('error', () => {
        if (!fromFeed() && item.image_url) hero.src = item.image_url;
        else hero.remove();
      });
      parts.push(hero);
    }

    if (item.text_status === 'pending' && !item.paywalled) {
      parts.push(h('div', { class: 'art-note art-note--quiet t-caption', text: polling ? 'Hole Text …' : 'Text noch nicht da' }));
    } else if (NOTE[item.text_status] && !item.paywalled) {
      parts.push(h('div', { class: 'art-note t-caption', text: NOTE[item.text_status] }));
    }

    const summary = cleanText(item.summary, item.title);
    if (bodyEl) parts.push(bodyEl);
    else if (summary && !item.paywalled) parts.push(h('div', { class: 'article-body t-article' }, h('p', { text: summary })));

    parts.push(h('div', { class: 'art-actions' },
      item.url ? h('a', {
        class: 'pill', href: item.url, target: '_blank', rel: 'noopener noreferrer',
      }, h('span', { text: 'Original öffnen' }), external(16)) : null,
      nextPill));

    parts.push(nextBlock);
    replace(body, parts);
    loadNext(nextPill, nextBlock);
  }

  /** Sofort-Vorschau aus der Liste (Titel, Meta, Anreißer), solange die Details laden. */
  function renderPreview() {
    const cached = cachedItem(numId);
    const parts = [];
    if (cached) {
      parts.push(h('div', { class: 'art-meta t-meta' },
        h('span', { class: 'src', text: sourceLabel(cached.source_id, cached.source_name) }),
        ` / ${cached.country || '?'} / ${articleTime(cached.published_at)}`));
      parts.push(h('h1', { class: 'art-title', text: cached.title || '(ohne Titel)' }));
    }
    parts.push(h('div', { class: 'art-note art-note--quiet t-caption', text: 'Hole Text …' }));
    const summary = cached && !cached.paywalled ? cleanText(cached.summary, cached.title) : '';
    if (summary) parts.push(h('div', { class: 'article-body t-article' }, h('p', { text: summary })));
    replace(body, parts);
  }

  async function loadNext(pill, block) {
    if (nextLoaded) return;
    nextLoaded = true;
    let next = null;
    if (state.nextCtx === 'local') next = nextFromList(item.id);
    else try {
      const res = await api(`articles/${item.id}/next`, { params: nextParams() });
      next = res ? res.item : null;
    } catch {
      return;   // ohne „Nächster“ weiterlesen
    }
    if (!alive || !next) return;
    const go = (e) => { e.preventDefault(); replaceHash(`#/a/${next.id}`); };
    pill.href = `#/a/${next.id}`;
    pill.hidden = false;
    pill.addEventListener('click', go);
    replace(block,
      h('span', { class: 't-caption', text: nextParams().unread !== 'false' ? 'Nächster ungelesener Artikel' : 'Nächster Artikel' }),
      h('a', { class: 'next-card', href: `#/a/${next.id}`, on: { click: go } },
        h('span', { class: 'next-card-text' },
          h('span', { class: 't-caption', text: `${sourceLabel(next.source_id, next.source_name)} / ${next.country || '?'} / ${listTime(next.published_at)}` }),
          h('span', { class: 'next-card-title', text: next.title || '(ohne Titel)' })),
        h('span', { class: 'chev', style: { color: 'var(--ink)' } }, arrowRight(18))));
    block.hidden = false;
  }

  async function load(first = true) {
    try {
      const data = await api(`articles/${id}`);
      if (!alive) return;
      const before = item || cachedItem(numId);
      item = data;
      state.openArticle = item;
      document.title = `${item.title || 'Artikel'} · Kiosk`;
      el.removeAttribute('aria-busy');
      // Beim Holen als Bezahlartikel erkannt: Zeile aus der Liste nehmen, Zähler neu laden.
      if (item.paywalled && before && !before.paywalled) dropPaywalledRow(item.id);
      // Der Server holt den Text im Hintergrund: alle 3 s nachfragen, höchstens 30 s lang.
      if (item.extracting && !pollUntil) pollUntil = Date.now() + POLL_MAX_MS;
      const wasPolling = polling;
      polling = !!item.extracting && Date.now() + POLL_MS <= pollUntil;
      // Während des Nachfragens nur neu zeichnen, wenn sich etwas ändert (erstes Laden, Ende).
      if (first || !polling || !wasPolling) {
        renderBody();
        paintFlags();
      }
      if (first && state.settings.mark_read_on_open && !item.read) {
        setFlag(item.id, 'read', true).catch(() => {});
      }
      if (polling) pollTimer = setTimeout(() => load(false), POLL_MS);
    } catch (err) {
      if (!alive) return;
      if (!first) {   // Nachfragen gescheitert: Ansicht bleibt, Hinweis „Text noch nicht da“
        polling = false;
        renderBody();
        paintFlags();
        return;
      }
      el.removeAttribute('aria-busy');
      replace(body, hint(err.status === 404 ? 'Artikel nicht gefunden' : `Fehler: ${err.detail || err.message}`));
    }
  }

  state.openArticleId = Number(id);
  markOpenRow(id);
  if (isDesktop()) {
    const row = document.querySelector(`#view .row[data-id="${Number(id)}"]`);
    row?.scrollIntoView({ block: 'nearest' });
  }
  renderPreview();
  load();

  return {
    el,
    id: Number(id),
    toggle,
    destroy() {
      alive = false;
      if (state.openArticle === item) state.openArticle = null;
      clearTimeout(pollTimer);
      offFlags();
    },
  };
}

/** Rechte Spalte auf dem Desktop ohne geöffneten Artikel. */
export function readerEmpty() {
  return h('div', { class: 'reader-empty' },
    h('div', { class: 't-section', text: 'WÄHLE EINEN ARTIKEL' }),
    h('p', { class: 't-caption muted', text: 'j / k blättern · Enter öffnen · / suchen' }));
}
