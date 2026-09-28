// Suche (E2Suche): Suchfeld, Zeitungs- und Zeitraum-Chips, Trefferzeile, Hervorhebung.
// Passende Zeitungen stehen als eigener Block über den Artikeln (E4SucheZeile, E4SucheMehrere).
import { h, hint, replace } from '../ui/dom.js';
import {
  arrowRight, close as closeIcon, icon, search as searchIcon, star,
} from '../ui/icons.js';
import { backButton } from '../ui/header.js';
import { chip } from '../ui/chips.js';
import { createFeed } from '../ui/feed.js';
import { emptyState } from '../ui/empty.js';
import { api } from '../api.js';
import { loadSources, on, state } from '../store.js';
import { matchSources, monograms } from '../sourceMatch.js';
import { parseHash } from '../route.js';

const RANGES = [
  { value: '1', label: '1 Tag' },
  { value: '7', label: '7 Tage' },
  { value: '30', label: '30 Tage' },
  { value: '', label: 'Alle' },
];
const PAPERS_SHOWN = 3;   // mehr Zeitungen erst nach „N weitere Zeitungen“
// Anfrage und Zeitungs-Chip, zu denen der Block zuletzt aufgeklappt wurde: übersteht Zeitung öffnen
// und Zurück (die Ansicht wird dabei neu gebaut).
let openedFor = null;
on('user-reset', () => { openedFor = null; });   // nicht an den nächsten Benutzer weitergeben

/** Ungelesen von heute: Zahl aus der Leiste bevorzugen (wie der Kopf der Zeitungsansicht). */
function unreadToday(s) {
  const barItem = (state.bar || []).find((i) => i.type === 'source' && i.id === s.id);
  return barItem ? barItem.unread : s.unread;
}

/** Was eine Zeile im Zeitungsblock zeigt (auch Vergleichsgrundlage, ob neu gezeichnet werden muss). */
function paperModel(s, mono) {
  const off = s.enabled === false;
  const unread = unreadToday(s);
  const meta = [s.country];
  if (off) meta.push('AUSGESCHALTET');
  else if (typeof unread === 'number' && unread > 0) meta.push(`${unread} UNGELESEN HEUTE`);   // 0/null: nur das Land
  return { id: s.id, name: s.name, mono, off, fav: !!s.is_favorite, meta: meta.filter(Boolean).join(' / ') };
}

/** Eine Zeile im Zeitungsblock: Monogramm, Name, Land und Ungelesen, Pfeil. */
function paperRow(m) {
  let fav = null;
  if (m.fav) {
    const starEl = star(12, true);
    starEl.classList.add('star-on');
    fav = [starEl, h('span', { class: 'visually-hidden', text: 'Lieblingszeitung,' })];
  }
  return h('a', { class: `paper${m.off ? ' is-off' : ''}`, href: `#/s/${encodeURIComponent(m.id)}` },
    h('span', { class: 'paper-mono', 'aria-hidden': 'true', text: m.mono }),
    h('span', { class: 'paper-main' },
      h('span', { class: 'paper-name', text: m.name }),
      h('span', { class: 'paper-meta t-meta' }, fav, h('span', { text: m.meta }))),
    h('span', { class: 'paper-go', 'aria-hidden': 'true' }, arrowRight(18)));
}

/** Suchparameter aus einer Route (#/suche?q=…&tage=…&s=…). */
function routeParams(route) {
  return {
    q: (route.params.get('q') || '').trim(),
    days: route.params.get('tage') || '',
    source: route.params.get('s') || '',
  };
}

/** Vergleichsschlüssel für Suchparameter. */
const paramsKey = ({ q, days, source }) => JSON.stringify([q, days, source]);

/** Adresse der Suche zu Anfrage, Zeitraum und Zeitung. */
function searchHashFor(q, days, source) {
  const p = new URLSearchParams();
  if (q) p.set('q', q);
  if (days) p.set('tage', days);
  if (source) p.set('s', source);
  const s = p.toString();
  return `#/suche${s ? `?${s}` : ''}`;
}

/** Wörter der Anfrage für die Hervorhebung (ohne FTS-Operatoren). */
export function queryWords(q) {
  return [...new Set(
    q.replace(/["()*^:]/g, ' ').split(/\s+/)
      .filter((w) => w.length >= 2 && !['AND', 'OR', 'NOT', 'NEAR'].includes(w)),
  )];
}

export function searchView(route) {
  let { q, days, source } = routeParams(route);
  let fromSource = source; // Zeitung, aus deren Ansicht die Suche kam
  // Parameter, die diese Ansicht zuletzt aus der Adresse übernommen oder in sie geschrieben hat. Bringt
  // der Router genau diese zurück (Zurück aus einem Artikel, in dem weitergetippt wurde), ist das
  // nur die Rückkehr zur Liste: dann bleibt die aktuelle Anfrage.
  let urlKey = paramsKey({ q, days, source });
  state.nextCtx = 'local';

  let timer = 0;
  let feed = null;
  let alive = true;
  let papers = [];          // passende Zeitungen zur aktuellen Anfrage
  let papersOpen = false;   // alle Zeitungen statt höchstens PAPERS_SHOWN
  let papersFor = null;     // Anfrage und Zeitungs-Chip, zu denen papersOpen gehört
  let papersSig = '';       // was der Block gerade zeigt (unverändert: DOM nicht anfassen)
  let count = null;         // Artikelzahl der aktuellen Anfrage (Text), null = noch keine Antwort
  let emptyEl = null;       // Leerzustand der Artikel, falls gerade zu sehen
  let sourcesAsked = false;
  const results = h('div', {});
  const papersEl = h('div', { class: 'papers', hidden: true });
  // Links entweder „n TREFFER“ (ohne Zeitungen, wie bisher) oder die Überschrift ARTIKEL.
  const hitsEl = h('span', { class: 't-section' });
  const articlesHead = h('h2', { class: 't-section', text: 'Artikel', hidden: true });
  const lineCaption = h('span', { class: 't-caption' });
  const resultLine = h('div', { class: 'result-line', hidden: true }, hitsEl, articlesHead, lineCaption);
  const lineWrap = h('div', { class: 'list', style: { paddingBottom: '0' } }, papersEl, resultLine);

  const input = h('input', {
    type: 'search', value: q, 'aria-label': 'Suchbegriff', placeholder: 'Suchen',
    autocomplete: 'off', enterkeyhint: 'search', spellcheck: 'false', maxlength: 200,
  });
  const clearBtn = h('button', {
    type: 'button', class: 'icon-btn icon-btn--plain', 'aria-label': 'Suche löschen',
    on: { click: () => { input.value = ''; input.focus(); schedule(0); } },
  }, closeIcon(14));
  const field = h('label', { class: 'search-field' }, searchIcon(20), input, clearBtn);

  const chipsEl = h('div', { class: 'chips', 'aria-label': 'Suchfilter' });

  function buildChips() {
    const src = fromSource ? state.sourceMap.get(fromSource) : null;
    replace(chipsEl,
      chip('Alle Zeitungen', { on: !source, onClick: () => { source = ''; update(); } }),
      src ? chip(src.name, { on: source === fromSource, onClick: () => { source = fromSource; update(); } }) : null,
      h('span', { class: 'chips-sep', 'aria-hidden': 'true' }),
      RANGES.map((r) => chip(r.label, { on: days === r.value, onClick: () => { days = r.value; update(); } })));
  }

  // Adresse nur ersetzen, solange die Suche selbst in der Adresse steht. Am Desktop kann daneben ein
  // Artikel offen sein (#/a/N): dessen Eintrag bleibt, gemerkt wird die Suche nur als Liste (Zurück
  // aus dem Artikel, Taste /).
  function syncUrl() {
    const hash = searchHashFor(q, days, source);
    if (parseHash(location.hash).name === 'search') {
      const tagged = !!(history.state && history.state.kiosk);
      history.replaceState({ kiosk: tagged }, '', hash);
      urlKey = paramsKey({ q, days, source });
    }
    state.lastListHash = hash;
  }

  // Zeitungsblock: nur mit „Alle Zeitungen“; die Zeitraum-Chips gelten nur für Artikel.
  function paintPapers() {
    papers = !source && q.length >= 2 ? matchSources(state.sources, q) : [];
    const monos = monograms(papers);   // Gleichstand im ganzen Treffer-Satz, nicht nur im Sichtbaren
    const rest = papersOpen ? 0 : Math.max(0, papers.length - PAPERS_SHOWN);
    const models = papers.slice(0, rest ? PAPERS_SHOWN : papers.length).map((s) => paperModel(s, monos.get(s.id)));
    const sig = JSON.stringify([papers.length, rest, models]);
    if (sig === papersSig) return;
    papersSig = sig;
    papersEl.hidden = !papers.length;
    if (!papers.length) { papersEl.replaceChildren(); return; }
    const more = rest ? h('button', {
      type: 'button', class: 'papers-more t-meta',
      on: {
        click: () => {
          papersOpen = true;
          openedFor = papersFor;
          paintPapers();
          papersEl.querySelectorAll('.paper')[PAPERS_SHOWN]?.focus();   // Knopf ist weg: erste neue Zeile
        },
      },
    }, `${rest} ${rest === 1 ? 'weitere Zeitung' : 'weitere Zeitungen'}`, icon('chevronDown', 16)) : null;
    replace(papersEl,
      h('div', { class: 'result-line' },
        h('h2', { class: 't-section', text: 'Zeitungen' }), h('span', { class: 't-caption', text: String(papers.length) })),
      h('nav', { class: 'papers-block', 'aria-label': 'Zeitungen' }, models.map(paperRow), more));
  }

  // Trefferzeile der Artikel: mit Zeitungsblock „ARTIKEL · 23 · Nach Relevanz“ (bei 0 Artikeln gar
  // keine Zeile, nur der leise Satz darunter), sonst „23 TREFFER“.
  function paintLine() {
    const withPapers = papers.length > 0;
    resultLine.hidden = count === null || (withPapers && count === '0');
    hitsEl.hidden = withPapers;
    articlesHead.hidden = !withPapers;
    hitsEl.textContent = `${count} TREFFER`;
    lineCaption.textContent = withPapers ? `${count} · Nach Relevanz` : 'Nach Relevanz';
  }

  function emptyFor() {
    // Eine Zeitung wurde gefunden: dann kein großes „KEINE TREFFER“, nur ein Satz zu den Artikeln.
    return papers.length
      ? h('p', { class: 'hint t-teaser', text: `Keine Artikel mit „${q}“.` })
      : emptyState({ title: 'KEINE TREFFER', text: `Nichts gefunden für „${q}“.` });
  }

  /** Zeitungen neu gegen die aktuelle Anfrage (auch wenn /api/sources später kommt oder sich ändert). */
  function refreshPapers() {
    // Fokus im Block (Tastatur) bleibt auf derselben Zeitung bzw. auf „weitere“.
    const active = papersEl.contains(document.activeElement) ? document.activeElement : null;
    const href = active && active.getAttribute('href');
    paintPapers();
    if (active) {
      const same = [...papersEl.querySelectorAll('a, button')]
        .find((x) => (href ? x.getAttribute('href') === href : x.tagName === 'BUTTON'));
      same?.focus();
    }
    if (count !== null) paintLine();
    if (emptyEl && emptyEl.isConnected) {
      const next = emptyFor();
      emptyEl.replaceWith(next);
      emptyEl = next;
    }
  }

  function update() {
    buildChips();
    run();
  }

  function schedule(delay = 300) {
    clearTimeout(timer);
    timer = setTimeout(() => {
      q = input.value.trim();
      run();
    }, delay);
  }

  function run() {
    syncUrl();
    feed?.destroy();
    feed = null;
    count = null;
    emptyEl = null;
    // Aufgeklappt bleibt der Block nur bei gleicher Anfrage und gleichem Zeitungs-Chip (Zeitraum egal).
    if (papersFor !== `${source}\n${q}`) papersOpen = openedFor === `${source}\n${q}`;
    papersFor = `${source}\n${q}`;
    resultLine.hidden = true;
    clearBtn.hidden = !input.value;
    paintPapers();
    if (!state.sources && !sourcesAsked) {
      // /api/sources fehlt (Start gescheitert): einmal nachladen, dann den Block nachziehen.
      sourcesAsked = true;
      loadSources().then(() => { if (alive) refreshPapers(); }).catch(() => {});
    }
    if (q.length < 2) {
      replace(results, q.length ? hint('Mindestens 2 Zeichen', 'hint--pad') : null);
      state.listItems = [];
      state.listIsCtx = false;
      return;
    }
    const words = queryWords(q);
    feed = createFeed({
      dayGroups: false,
      words,
      load: async (cursor, isStale) => {
        const params = {
          q, limit: 30, days: days || undefined,
          source: source || undefined, offset: cursor ?? undefined,
        };
        const res = await api('articles', { params });
        // Verspätete Antwort einer älteren Anfrage (oder nach dem Leeren): Trefferzeile nicht anfassen.
        if (cursor == null && !isStale()) {
          const total = typeof res.total === 'number' ? res.total : null;
          count = total !== null ? (total >= 1000 ? '1000+' : String(total)) : `${res.items.length}${res.next_offset ? '+' : ''}`;
          paintLine();
        }
        return { items: res.items || [], next: res.next_offset };
      },
      empty: () => (emptyEl = emptyFor()),
    });
    state.listItems = feed.items;
    state.listIsCtx = false;
    replace(results, feed.el);
    feed.start().catch(() => {});
  }

  input.addEventListener('input', () => { clearBtn.hidden = !input.value; schedule(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); schedule(0); input.blur(); }
  });

  const el = h('div', { class: 'view-list' },
    h('header', { class: 'hdr' }, backButton('#/'), field),
    chipsEl, lineWrap, results);

  buildChips();
  run();
  // Zähler und Lieblingszeitungen im Zeitungsblock aktuell halten (Leiste und /api/sources neu geladen).
  const offBar = on('bar', () => refreshPapers());

  return {
    el, layout: 'list', bar: false, title: 'Suche',
    // Der Router nutzt die Ansicht wieder (gleicher Schlüssel), etwa bei Zurück auf eine ältere
    // Anfrage: Parameter neu aus der Adresse lesen, bei Änderung neu suchen.
    update(r) {
      const next = routeParams(r);
      const key = paramsKey(next);
      if (key === paramsKey({ q, days, source })) { urlKey = key; return; }
      if (key === urlKey) {
        // Nur zurück zur Liste: Anfrage bleibt, die Adresse folgt ihr.
        syncUrl();
        return;
      }
      // Echte andere Adresse: übernehmen.
      clearTimeout(timer);
      ({ q, days, source } = next);
      urlKey = key;
      if (source) fromSource = source;
      input.value = q;
      buildChips();
      run();
    },
    onShow() {
      // Über Taste / oder die Fokus-Leiste gekommen (openSearch): Feld fokussieren, auch mit Anfrage.
      const asked = state.focusSearch;
      state.focusSearch = false;
      if (!q || asked) input.focus();
      if (asked) input.select();
    },
    destroy() { alive = false; clearTimeout(timer); feed?.destroy(); offBar(); },
  };
}
