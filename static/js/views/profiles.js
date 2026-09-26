// Profile und Lieblingszeitungen (E2Profile) als Blatt; Sortieren; Zeitung hinzufügen.
import { append, h, hint, toast } from '../ui/dom.js';
import { icon, back, chevronRight, close as closeIcon, plus, search as searchIcon } from '../ui/icons.js';
import { closeSheet, openSheet, rebuildSheet, sheetGrip } from '../ui/sheet.js';
import { toggle } from '../ui/toggle.js';
import { api } from '../api.js';
import {
  emit, goBack, loadBar, loadFavorites, loadProfiles, loadSources, state,
} from '../store.js';

/** Untertitel eines Profils: „15 Zeitungen · 3 Begriffe“, „Alle 92 Zeitungen“, „Eigenes · 6 Zeitungen“. */
export function profileSub(p) {
  const total = state.sources ? state.sources.filter((s) => s.enabled).length : null;
  const terms = (p.terms || []).length;
  const termText = terms ? `${terms} ${terms === 1 ? 'Begriff' : 'Begriffe'}` : null;
  let src;
  if (p.sources === null || p.sources === undefined) {
    src = terms ? 'Alle' : `Alle ${total ?? ''} Zeitungen`.replace('  ', ' ');
  } else {
    const n = typeof p.source_count === 'number' ? p.source_count : p.sources.length;
    src = `${n} ${n === 1 ? 'Zeitung' : 'Zeitungen'}`;
  }
  return [p.builtin ? null : 'Eigenes', src, termText].filter(Boolean).join(' · ');
}

const barKey = (i) => i.key || (i.type === 'all' ? 'all' : `${i.type}:${i.id}`);

function arrows(onUp, onDown, first, last, name) {
  return h('span', { class: 'sort-btns' },
    h('button', {
      type: 'button', class: 'icon-btn icon-btn--sm', 'aria-label': `${name} nach oben`, disabled: first,
      on: { click: onUp },
    }, icon('chevronUp', 18)),
    h('button', {
      type: 'button', class: 'icon-btn icon-btn--sm', 'aria-label': `${name} nach unten`, disabled: last,
      on: { click: onDown },
    }, icon('chevronDown', 18)));
}

function move(list, index, delta) {
  const copy = [...list];
  const [item] = copy.splice(index, 1);
  copy.splice(index + delta, 0, item);
  return copy;
}

export function openProfiles(route) {
  let mode = route.params.get('sortieren') ? 'sort' : 'main';
  let query = '';
  const scrollToFavorites = route.frag === 'zeitungen';

  const fail = (err) => toast(`Fehler: ${err.detail || err.message}`);
  const refresh = async () => {
    await Promise.allSettled([loadProfiles(), loadFavorites(), loadBar()]);
    emit('profiles');
    rebuildSheet(build);
  };

  function head(title, ...right) {
    return h('div', { class: 'sheet-head' }, h('h2', { class: 't-section', text: title }), ...right);
  }
  const closeBtn = () => h('button', {
    type: 'button', class: 'icon-btn icon-btn--plain', 'aria-label': 'Schließen', on: { click: () => closeSheet() },
  }, closeIcon(14));

  // ---- Hauptansicht ----
  function profileRow(p) {
    return h('div', { class: 'set-row' },
      h('a', {
        class: 'set-row-text set-row-link', href: `#/?p=${p.id}`, on: { click: () => closeSheet({ silent: true }) },
      }, h('span', { class: 't-ui', text: p.name }), h('span', { class: 't-caption', text: profileSub(p) })),
      toggle(!!p.in_bar, `${p.name} in der Leiste`, async (v) => {
        try {
          await api(`profiles/${p.id}`, { method: 'PATCH', body: { in_bar: v } });
          p.in_bar = v;
          await loadBar();
        } catch (err) { fail(err); err.toasted = true; throw err; }
      }),
      h('a', {
        class: 'icon-btn icon-btn--plain', href: `#/profile/${p.id}`, 'aria-label': `${p.name} bearbeiten`,
      }, chevronRight(18)));
  }

  function favoriteRow(f) {
    const cc = f.country || '?';
    return h('div', { class: 'set-row' },
      h('a', {
        class: 'set-row-text set-row-link', href: `#/s/${encodeURIComponent(f.source_id)}`,
        on: { click: () => closeSheet({ silent: true }) },
      },
      h('span', { class: 't-ui', text: f.name }),
      h('span', { class: 't-caption', text: f.unread ? `${cc} · ${f.unread} ungelesen heute` : cc })),
      toggle(!!f.in_bar, `${f.name} in der Leiste`, async (v) => {
        try {
          await api(`favorites/sources/${encodeURIComponent(f.source_id)}`, { method: 'PUT', body: { in_bar: v } });
          f.in_bar = v;
          await loadBar();
        } catch (err) { fail(err); err.toasted = true; throw err; }
      }),
      h('a', {
        class: 'icon-btn icon-btn--plain', href: `#/s/${encodeURIComponent(f.source_id)}`,
        'aria-label': `${f.name} öffnen`, on: { click: () => closeSheet({ silent: true }) },
      }, chevronRight(18)));
  }

  function buildMain(panel) {
    const profiles = state.profiles;
    const favorites = state.favorites;
    append(panel,
      sheetGrip(),
      head('PROFILE',
        h('span', { class: 't-caption', text: 'In der Leiste' }),
        h('button', { type: 'button', class: 'btn-sm', text: 'Sortieren', on: { click: () => { mode = 'sort'; rebuildSheet(build); } } }),
        closeBtn()),
      profiles ? profiles.map(profileRow) : hint('Profile konnten nicht geladen werden.'),
      h('a', { class: 'pill pill--block', href: '#/profile/neu', style: { marginTop: '4px' } },
        plus(20), h('span', { text: 'Profil anlegen' })),
      h('div', { class: 'sheet-head sheet-head--sub', id: 'zeitungen' }, h('h2', { class: 't-section', text: 'FAVORITEN' })),
      favorites
        ? (favorites.length ? favorites.map(favoriteRow) : hint('Noch keine Lieblingszeitung.'))
        : hint('Lieblingszeitungen konnten nicht geladen werden.'),
      h('button', {
        type: 'button', class: 'pill pill--block', style: { marginTop: '4px' }, disabled: !favorites,
        on: { click: () => { mode = 'pick'; query = ''; rebuildSheet(build); panel.querySelector('input')?.focus(); } },
      }, plus(20), h('span', { text: 'Zeitung hinzufügen' })));
  }

  // ---- Sortieren ----
  function buildSort(panel) {
    const bar = (state.bar || []).filter((i) => i.type !== 'all');
    const profiles = state.profiles || [];
    const favorites = state.favorites || [];

    const saveBar = async (items) => {
      try {
        const res = await api('bar', { method: 'PATCH', body: { order: items.map(barKey) } });
        if (res && res.items) { state.bar = res.items; emit('bar'); } else await loadBar();
        rebuildSheet(build);
      } catch (err) { fail(err); }
    };
    const saveProfiles = async (list) => {
      try {
        await api('profiles/reorder', { method: 'POST', body: { ids: list.map((p) => p.id) } });
        await refresh();
      } catch (err) { fail(err); }
    };
    const saveFavorites = async (list) => {
      try {
        await api('favorites/sources/reorder', { method: 'POST', body: { ids: list.map((f) => f.source_id) } });
        await refresh();
      } catch (err) { fail(err); }
    };
    const removeFavorite = async (f) => {
      try {
        await api(`favorites/sources/${encodeURIComponent(f.source_id)}`, { method: 'DELETE' });
        const s = state.sourceMap.get(f.source_id);
        if (s) s.is_favorite = false;
        await refresh();
      } catch (err) { fail(err); }
    };

    const line = (title, sub, ...right) => h('div', { class: 'set-row' },
      h('span', { class: 'set-row-text' }, h('span', { class: 't-ui', text: title }),
        sub ? h('span', { class: 't-caption', text: sub }) : null), ...right);

    append(panel,
      sheetGrip(),
      head('SORTIEREN',
        h('button', { type: 'button', class: 'btn-sm is-on', text: 'Fertig', on: { click: () => { mode = 'main'; rebuildSheet(build); } } }),
        closeBtn()),
      h('div', { class: 'sheet-head sheet-head--sub' }, h('h2', { class: 't-section', text: 'LEISTE' })),
      bar.length
        ? bar.map((item, i) => line(item.label, item.type === 'profile' ? 'Profil' : 'Zeitung',
          arrows(() => saveBar(move(bar, i, -1)), () => saveBar(move(bar, i, 1)), i === 0, i === bar.length - 1, item.label)))
        : hint(state.bar ? 'Die Leiste ist leer. Schalte Profile oder Zeitungen ein.' : 'Die Leiste konnte nicht geladen werden.'),
      h('div', { class: 'sheet-head sheet-head--sub' }, h('h2', { class: 't-section', text: 'PROFILE' })),
      profiles.map((p, i) => line(p.name, null,
        arrows(() => saveProfiles(move(profiles, i, -1)), () => saveProfiles(move(profiles, i, 1)), i === 0, i === profiles.length - 1, p.name))),
      h('div', { class: 'sheet-head sheet-head--sub' }, h('h2', { class: 't-section', text: 'FAVORITEN' })),
      favorites.map((f, i) => line(f.name, f.country,
        arrows(() => saveFavorites(move(favorites, i, -1)), () => saveFavorites(move(favorites, i, 1)), i === 0, i === favorites.length - 1, f.name),
        h('button', {
          type: 'button', class: 'icon-btn icon-btn--plain', 'aria-label': `${f.name} entfernen`,
          on: { click: () => removeFavorite(f) },
        }, closeIcon(14)))));
  }

  // ---- Zeitung hinzufügen ----
  function buildPick(panel) {
    const favIds = new Set((state.favorites || []).map((f) => f.source_id));
    const input = h('input', {
      type: 'search', value: query, placeholder: 'Zeitung suchen', 'aria-label': 'Zeitung suchen', autocomplete: 'off',
    });
    const listEl = h('div', {});
    const paint = () => {
      const q = query.trim().toLowerCase();
      const candidates = (state.sources || [])
        .filter((s) => s.enabled && !favIds.has(s.id))
        .filter((s) => !q || s.name.toLowerCase().includes(q) || (s.country || '').toLowerCase() === q || s.id.includes(q))
        .sort((a, b) => a.name.localeCompare(b.name, 'de'));
      listEl.replaceChildren(...(candidates.length ? candidates.map((s) => h('button', {
        type: 'button', class: 'set-row', style: { width: '100%', background: 'none', border: '0', borderTop: '1px solid var(--hairline)', textAlign: 'left', padding: '0' },
        on: {
          click: async () => {
            try {
              await api(`favorites/sources/${encodeURIComponent(s.id)}`, { method: 'PUT', body: {} });
              s.is_favorite = true;
              toast(`${s.name} hinzugefügt`);
              mode = 'main';
              await refresh();
            } catch (err) { fail(err); }
          },
        },
      }, h('span', { class: 'set-row-text' }, h('span', { class: 't-ui', text: s.name }), h('span', { class: 't-caption', text: s.country || '' })),
      h('span', { class: 'chev' }, plus(20)))) : [hint('Keine passende aktive Zeitung.')]));
    };
    input.addEventListener('input', () => { query = input.value; paint(); });
    append(panel,
      sheetGrip(),
      h('div', { class: 'sheet-head' },
        h('button', {
          type: 'button', class: 'icon-btn', 'aria-label': 'Zurück', on: { click: () => { mode = 'main'; rebuildSheet(build); } },
        }, back(20)),
        h('label', { class: 'search-field', style: { borderColor: 'var(--rule)' } }, searchIcon(20), input),
        closeBtn()),
      listEl);
    paint();
  }

  function build(panel) {
    if (mode === 'sort') buildSort(panel);
    else if (mode === 'pick') buildPick(panel);
    else buildMain(panel);
  }

  const panel = openSheet(build, {
    label: 'Profile und Favoriten', tall: true,
    onClose: () => goBack(state.lastListHash),
  });
  Promise.allSettled([loadProfiles(), loadFavorites(), loadBar(), state.sources ? null : loadSources()]).then(() => {
    rebuildSheet(build);
    if (scrollToFavorites) panel.querySelector('#zeitungen')?.scrollIntoView({ block: 'start' });
  });
}
