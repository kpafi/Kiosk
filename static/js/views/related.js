// Kontextspalte zum offenen Artikel (E3DesktopKontext, nur Desktop): DIESELBE GESCHICHTE aus anderen
// Zeitungen und ZUM THEMA. Ab 1400 px eigene Spalte rechts, sonst (oder im Fokusmodus) unter dem
// Artikel; die Lage bestimmt nur CSS (.art-rail in app.css).
import { h, replace } from '../ui/dom.js';
import { metaParts } from '../ui/row.js';
import { matrix } from '../ui/empty.js';
import { api } from '../api.js';
import { on } from '../store.js';

const SAME_SHOWN = 5;   // DIESELBE GESCHICHTE: so viele sofort, der Rest hinter „+ N weitere“
const NONE = 'Noch keine anderen Zeitungen berichten darüber.';

/** Nur Einträge mit Artikel-ID (Teilantworten des Servers dürfen nichts kaputt machen). */
export function validItems(list) {
  return (Array.isArray(list) ? list : []).filter((it) => it && Number.isInteger(it.id));
}

/** Kompakter Eintrag: Meta-Zeile wie in der Liste, Titel; ein echter Link auf den Artikel. */
function railItem(item) {
  const meta = h('div', { class: 'row-meta t-meta' }, metaParts(item));
  const el = h('a', { class: `rail-item${item.read ? ' is-read' : ''}`, href: `#/a/${item.id}`, dataset: { id: item.id } },
    meta, h('span', { class: 'rail-title', text: item.title || '(ohne Titel)' }));
  el._item = item;
  return el;
}

function railSection(title, caption, ...items) {
  return h('section', { class: 'rail-sec' },
    h('div', { class: 'rail-head' },
      h('h2', { class: 'rail-title-sec', text: title }),
      caption ? h('span', { class: 't-caption muted', text: caption }) : null),
    items);
}

/**
 * Kontextspalte. load(id) holt /api/articles/{id}/related (einmal, nach dem Laden des Artikels).
 * fail() (Ladefehler, Bezahlartikel, Artikel fehlt) blendet sie ganz aus, der Artikel bekommt die
 * volle Breite. destroy() bricht eine laufende Anfrage ab (anderer Artikel offen).
 */
export function relatedRail() {
  const el = h('aside', { class: 'art-rail only-desktop', 'aria-label': 'Mehr zu diesem Artikel', 'aria-busy': 'true' });
  const inner = h('div', { class: 'art-rail-inner' },
    h('div', { class: 'rail-loading' }, matrix(), h('p', { class: 't-caption muted', text: 'Lädt' })));
  el.append(inner);
  let ctrl = null;
  let alive = true;
  const known = new Map();   // id → Eintrag (auch noch nicht gezeigte), für den Gelesen-Status

  function fail() {
    ctrl?.abort();
    el.removeAttribute('aria-busy');
    el.hidden = true;
    el.setAttribute('aria-hidden', 'true');
    el.closest('.art-view')?.classList.remove('has-rail');
    replace(inner);
  }

  function render(data) {
    const same = validItems(data.same_story);
    const topic = validItems(data.topic);
    for (const it of [...same, ...topic]) known.set(it.id, it);
    el.removeAttribute('aria-busy');
    if (!same.length && !topic.length) {
      replace(inner, railSection('DIESELBE GESCHICHTE', null, h('p', { class: 'rail-none t-teaser muted', text: NONE })));
      return;
    }
    const parts = [];
    if (same.length) {
      const count = Number(data.sources_count) || 0;
      const caption = count > 1 ? `${count} Zeitungen` : null;
      const list = h('div', { class: 'rail-list' }, same.slice(0, SAME_SHOWN).map(railItem));
      const rest = same.slice(SAME_SHOWN);
      let more = null;
      if (rest.length) {
        more = h('button', {
          type: 'button', class: 'rail-more t-caption', text: `+ ${rest.length} weitere`,
          'aria-label': `${rest.length} weitere Zeitungen zeigen`,
          on: {
            click: () => {
              const added = rest.map(railItem);   // Einträge tragen den aktuellen Gelesen-Status (known)
              list.append(...added);
              more.remove();
              added[0].focus({ preventScroll: true });
            },
          },
        });
      }
      parts.push(railSection('DIESELBE GESCHICHTE', caption, list, more));
    } else {
      parts.push(railSection('DIESELBE GESCHICHTE', null, h('p', { class: 'rail-none t-teaser muted', text: NONE })));
    }
    if (topic.length) parts.push(railSection('ZUM THEMA', null, h('div', { class: 'rail-list' }, topic.map(railItem))));
    replace(inner, parts);
  }

  async function load(id) {
    ctrl?.abort();
    ctrl = new AbortController();
    const { signal } = ctrl;
    try {
      const data = await api(`articles/${Number(id)}/related`, { signal });
      if (!alive || signal.aborted) return;
      render(data || {});
    } catch {
      if (alive && !signal.aborted) fail();   // Fehler: Spalte still ausblenden
    }
  }

  // Gelesen-Status (Liste, Tastatur, andere Ansicht) in den Einträgen nachziehen, auch in denen
  // hinter „+ N weitere“.
  const offFlags = on('flags', (patch) => {
    if (!('read' in patch)) return;
    const item = known.get(Number(patch.id));
    if (!item) return;
    Object.assign(item, patch);
    for (const a of el.querySelectorAll(`.rail-item[data-id="${Number(patch.id)}"]`)) {
      a.classList.toggle('is-read', !!item.read);
      replace(a.querySelector('.row-meta'), metaParts(item));
    }
  });

  return {
    el,
    load,
    fail,
    destroy() {
      alive = false;
      ctrl?.abort();
      offFlags();
    },
  };
}
