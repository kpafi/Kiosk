// Zeitungen verwalten (E2Quellen): Zähler, Jetzt abrufen, Filterchips, Ländergruppen, Schalter.
import { h, hint, replace, toast } from '../ui/dom.js';
import { clock } from '../ui/icons.js';
import { iconButton, subHeader } from '../ui/header.js';
import { chipGroup } from '../ui/chips.js';
import { toggle } from '../ui/toggle.js';
import { api } from '../api.js';
import { ago, countryName, formatCount, groupByCountry } from '../format.js';
import { loadBar, loadSources, loadStatus, state } from '../store.js';

const FILTERS = [
  { value: 'all', label: 'Alle' },
  { value: 'active', label: 'Aktive' },
  { value: 'error', label: 'Mit Fehler' },
  { value: 'de', label: 'Deutsch' },
  { value: 'en', label: 'Englisch' },
];

const hasError = (s) => s.enabled && !!s.last_error;

function subLine(s) {
  const cc = s.country || '?';
  if (!s.enabled) return h('span', { class: 't-caption', text: `${cc} · Aus` });
  if (s.last_error) {
    return h('span', { class: 't-caption' }, `${cc} · `,
      h('span', { class: 'err-inline', text: `Fehler: ${s.last_error}` }));
  }
  const parts = [cc, `${formatCount(s.total)} Artikel`];
  if (s.last_ok_at) parts.push(ago(s.last_ok_at));
  return h('span', { class: 't-caption', text: parts.join(' · ') });
}

export function sourcesView() {
  let filter = 'all';
  const header = subHeader({ title: 'ZEITUNGEN', sub: ' ', fallback: '#/einstellungen' });
  const subEl = header.querySelector('.t-caption');
  const refreshBtn = iconButton(clock(16), 'Jetzt abrufen', { onClick: refresh });
  header.append(refreshBtn);
  const chipsEl = h('div', { class: 'chips', role: 'group', 'aria-label': 'Filter' });
  const listEl = h('div', { class: 'page', style: { paddingTop: '0' } });
  const el = h('div', { class: 'view-page' }, header, chipsEl, listEl);

  async function refresh() {
    refreshBtn.disabled = true;
    try {
      await api('refresh', { method: 'POST' });
      toast('Sammellauf gestartet');
    } catch (err) {
      toast(err.status === 409 ? 'Läuft bereits' : `Fehler: ${err.detail || err.message}`);
    } finally {
      refreshBtn.disabled = false;
      loadStatus().catch(() => {});
    }
  }

  function paintHead() {
    const all = state.sources || [];
    const enabled = all.filter((s) => s.enabled).length;
    const failing = all.filter(hasError).length;
    subEl.textContent = `${enabled} von ${all.length} aktiv${failing ? ` · ${failing} mit Fehler` : ''}`;
  }

  function matches(s) {
    if (filter === 'active') return s.enabled;
    if (filter === 'error') return hasError(s);
    if (filter === 'de') return (s.lang || '').toLowerCase().startsWith('de');
    if (filter === 'en') return (s.lang || '').toLowerCase().startsWith('en');
    return true;
  }

  function sourceRow(s) {
    return h('div', { class: 'set-row' },
      h('a', { class: 'set-row-text set-row-link', href: `#/s/${encodeURIComponent(s.id)}` },
        h('span', { class: 't-ui', text: s.name }),
        subLine(s)),
      toggle(s.enabled, `${s.name} aktiv`, async (value) => {
        const updated = await api(`sources/${encodeURIComponent(s.id)}`, { method: 'PATCH', body: { enabled: value } })
          .catch((err) => { toast(`Fehler: ${err.detail || err.message}`); err.toasted = true; throw err; });
        Object.assign(s, updated || { enabled: value });
        const hadFocus = document.activeElement && document.activeElement.getAttribute('aria-label');
        paintHead();
        build();
        if (hadFocus) [...listEl.querySelectorAll('.switch')].find((b) => b.getAttribute('aria-label') === hadFocus)?.focus();
        loadBar().catch(() => {});
      }));
  }

  function build() {
    replace(chipsEl, chipGroup(FILTERS, filter, (v) => { filter = v; build(); }));
    const all = state.sources || [];
    const groups = groupByCountry(all);
    const out = [];
    for (const [cc, list] of groups) {
      const shown = list.filter(matches);
      if (!shown.length) continue;
      const active = list.filter((s) => s.enabled).length;
      out.push(h('div', { class: 'sec-row' },
        h('h2', { class: 't-section', text: countryName(cc).toUpperCase() }),
        h('span', { class: 't-caption', text: `${active} von ${list.length}` })));
      out.push(...shown.map(sourceRow));
    }
    if (!out.length) out.push(hint(filter === 'error' ? 'Keine Zeitung mit Fehler.' : 'Keine Zeitung passt.'));
    replace(listEl, out);
  }

  paintHead();
  build();
  loadSources().then(() => { paintHead(); build(); }).catch((err) => replace(listEl, hint(`Fehler: ${err.detail || err.message}`)));

  return { el, layout: 'page', bar: false, title: 'Zeitungen' };
}
