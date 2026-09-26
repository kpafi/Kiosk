// Gemerkt (E2Gemerkt): Reiter „Später lesen“ / „Favoriten“, Liste ohne Tagesgruppen.
import { h, replace } from '../ui/dom.js';
import { subHeader } from '../ui/header.js';
import { chip } from '../ui/chips.js';
import { articlesLoader, createFeed } from '../ui/feed.js';
import { emptyState } from '../ui/empty.js';
import { loadCounts, on, state } from '../store.js';

export function savedView(route) {
  const tab = route.params.get('tab') === 'favoriten' ? 'favoriten' : 'spaeter';
  state.ctx = { type: 'all' };
  // „Nächster“ in Gemerkt: auch gelesene Artikel mit derselben Markierung.
  state.nextCtx = tab === 'favoriten' ? { starred: 'true', unread: 'false' } : { later: 'true', unread: 'false' };

  const chipsEl = h('div', { class: 'chips', role: 'navigation', 'aria-label': 'Gemerkt' });

  function buildChips() {
    const c = state.counts;
    const later = c ? ` · ${c.later}` : '';
    const starred = c ? ` · ${c.starred}` : '';
    replace(chipsEl,
      chip(`Später lesen${later}`, { on: tab === 'spaeter', href: '#/gemerkt' }),
      chip(`Favoriten${starred}`, { on: tab === 'favoriten', href: '#/gemerkt?tab=favoriten' }));
  }

  const params = tab === 'favoriten' ? { starred: 'true' } : { later: 'true' };
  const feed = createFeed({
    dayGroups: false,
    mode: tab === 'favoriten' ? 'star' : 'later',
    load: articlesLoader(params),
    empty: () => emptyState({
      title: 'NOCH NICHTS',
      text: tab === 'favoriten'
        ? 'Mit dem Stern in der Leseansicht merkst du einen Artikel als Favorit.'
        : 'Mit dem Lesezeichen in der Leseansicht legst du einen Artikel auf „Später lesen“.',
    }),
  });
  state.listItems = feed.items;
  state.listIsCtx = false;

  loadCounts().then(buildChips).catch(() => {});

  buildChips();
  feed.start();
  const off = on('flags', () => loadCounts().then(buildChips).catch(() => {}));

  const el = h('div', { class: 'view-list' },
    subHeader({ title: 'GEMERKT', fallback: '#/' }),
    chipsEl, feed.el);

  return {
    el, layout: 'list', bar: false, title: 'Gemerkt',
    destroy() { feed.destroy(); off(); },
  };
}
