// Leseansicht (E2Lese, rechts in E2Desktop): Kopf mit Flags, Meta, Titel, Bild, Volltext, Nächster.
import { h, hint, isDesktop, replace } from '../ui/dom.js';
import { arrowRight, back, bookmark, checkCheck, external, star } from '../ui/icons.js';
import { iconButton } from '../ui/header.js';
import { markOpenRow } from '../ui/row.js';
import { articleTime, cleanText, listTime, readMinutes } from '../format.js';
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

const CAPTION_RE = /^\s*(Foto|Bild|Fotos|Bilder|Photo|Image)\s*:/i;

/** Absatz direkt nach einem Bild, der nur die Bildquelle nennt („Foto: …“). */
function captionAfter(node) {
  let next = node.nextElementSibling;
  if (!next && node.parentElement && node.parentElement.tagName !== 'DIV') next = node.parentElement.nextElementSibling;
  return next && next.tagName === 'P' && CAPTION_RE.test(next.textContent || '') ? next : null;
}

/**
 * Artikeltext aus body_html. Erst in einem <template> aufbereiten (dort laden Bilder noch nicht),
 * dann einfügen: Links in neuem Tab, Bilder faul und ohne Referrer oder (show_images aus) entfernt.
 * Einzige HTML-Stelle: body_html ist serverseitig auf eine Whitelist bereinigt.
 */
function buildBody(html) {
  const tpl = document.createElement('template');
  tpl.innerHTML = html;
  const frag = tpl.content;
  for (const a of frag.querySelectorAll('a')) {
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
  }
  for (const img of frag.querySelectorAll('img')) {
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
      bodyEl = buildBody(item.body_html);
    }

    // Überschrift am Textanfang, die nur den Titel wiederholt, entfällt.
    const norm = (t) => (t || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');
    const firstEl = bodyEl && bodyEl.firstElementChild;
    if (firstEl && /^H[1-3]$/.test(firstEl.tagName) && norm(firstEl.textContent) === norm(item.title)) firstEl.remove();

    // Titelbild nur, wenn der Text nicht selbst mit einem Bild beginnt oder es enthält.
    const base = (u) => { try { return new URL(u).pathname.split('/').pop(); } catch { return u; } };
    const bodyImgs = bodyEl ? [...bodyEl.querySelectorAll('img')] : [];
    const leadImg = bodyEl && [...bodyEl.children].slice(0, 3).some((c) => c.tagName === 'IMG' || c.querySelector('img'));
    const inBody = leadImg || bodyImgs.some((i) => i.src === item.image_url || base(i.src) === base(item.image_url));
    if (item.image_url && state.settings.show_images && !inBody && !item.paywalled) {
      const hero = h('img', {
        class: 'art-hero', src: item.image_url, alt: '', decoding: 'async', referrerpolicy: 'no-referrer',
      });
      hero.addEventListener('error', () => hero.remove(), { once: true });
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
