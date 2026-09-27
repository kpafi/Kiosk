// Einstellungen (E2Einstellungen): Konto, Erscheinungsbild, Textgröße, Am PC (nur Desktop,
// E3DesktopEinstellungen), Lesen, Quellen, Abmelden.
import { h, replace, toast } from '../ui/dom.js';
import { chevronRight } from '../ui/icons.js';
import { subHeader } from '../ui/header.js';
import { chipGroup } from '../ui/chips.js';
import { toggle } from '../ui/toggle.js';
import { api } from '../api.js';
import { formatCount, formatMB } from '../format.js';
import { focusSwitchChange } from './focus.js';
import {
  emit, isAdmin, loadSettings, loadStatus, resetUserState, saveSettings, state,
} from '../store.js';

function section(title, extra = '') {
  return h('h2', { class: `sec ${extra} t-section`.trim(), text: title });
}

/** Chipgruppe: Auswahl an Ort und Stelle umschalten (kein Neuaufbau, der Fokus bleibt). */
function selectGroup(label, options, current, onSelect) {
  const group = h('div', { class: 'chips chips--flush', role: 'group', 'aria-label': label });
  group.append(...chipGroup(options, current, (v) => {
    options.forEach((o, i) => {
      const btn = group.children[i];
      btn.classList.toggle('is-on', o.value === v);
      btn.setAttribute('aria-pressed', String(o.value === v));
    });
    onSelect(v);
  }));
  return group;
}

function row(title, sub, ...right) {
  return h('div', { class: 'set-row' },
    h('span', { class: 'set-row-text' },
      h('span', { class: 't-ui', text: title }),
      h('span', { class: 't-caption', text: sub })),
    ...right);
}

export function settingsView() {
  const page = h('div', { class: 'page' });
  const el = h('div', { class: 'view-page' }, subHeader({ title: 'EINSTELLUNGEN', fallback: '#/' }), page);

  async function save(patch) {
    try {
      await saveSettings(patch);
      if ('show_images' in patch) emit('reload-list');
    } catch (err) {
      toast(`Fehler: ${err.detail || err.message}`);
      await loadSettings().catch(() => {});
      build();
      err.toasted = true;
      throw err;
    }
  }

  function build() {
    const s = state.settings;
    const st = state.status || {};
    const conf = st.settings || {};
    const db = st.db || {};
    const interval = conf.fetch_interval ? Math.round(conf.fetch_interval / 60) : null;

    const themeChips = selectGroup('Erscheinungsbild', [
      { value: 'system', label: 'System' }, { value: 'dark', label: 'Dunkel' }, { value: 'light', label: 'Hell' },
    ], s.theme, (v) => { save({ theme: v }).catch(() => {}); });
    const sizeChips = selectGroup('Textgröße', [
      { value: 's', label: 'Klein' }, { value: 'm', label: 'Normal' }, { value: 'l', label: 'Groß' },
    ], s.text_size, (v) => { save({ text_size: v }).catch(() => {}); });

    const emptyChips = selectGroup('Rechts, wenn kein Artikel offen ist', [
      { value: 'overview', label: 'Tagesübersicht' }, { value: 'frontpage', label: 'Titelseite' },
    ], s.desktop_empty === 'frontpage' ? 'frontpage' : 'overview', (v) => { save({ desktop_empty: v }).catch(() => {}); });
    emptyChips.removeAttribute('aria-label');
    emptyChips.setAttribute('aria-labelledby', 'set-desktop-empty');

    const admin = isAdmin();
    const user = state.user;
    const build_ = st.build ? String(st.build).slice(0, 8) : 'lokal';
    // Artikelzahl und DB-Größe nur für den Admin (Nicht-Admins bekommen sie gar nicht).
    const footer = admin
      ? `Kiosk · Build ${build_} · ${formatCount(db.total)} Artikel · ${formatMB(db.db_bytes)}`
      : `Kiosk · Build ${build_}`;
    // Zähler aus /api/status (nur Admin), sonst aus der Zeitungsliste.
    const sources = state.sources || [];
    const total = conf.sources_total || sources.length;
    const enabled = conf.sources_total ? conf.sources_enabled : sources.filter((x) => x.enabled).length;

    replace(page,
      user ? [
        section('KONTO', 'sec--first'),
        row(`Angemeldet als ${user.display_name || user.username}`,
          admin ? 'Admin · verwaltet Zeitungen und Server' : 'Nutzer · eigene Profile und Merklisten'),
      ] : null,
      section('ERSCHEINUNGSBILD', user ? '' : 'sec--first'),
      themeChips,
      section('TEXTGRÖSSE'),
      sizeChips,
      // Nur am Desktop (ab 900 px): rechte Seite ohne Artikel und Fokusmodus.
      h('div', { class: 'set-pc only-desktop' },
        h('div', { class: 'sec-pc' },
          h('h2', { class: 't-section', text: 'AM PC' }),
          h('span', { class: 'badge-signal t-caption', text: 'Nur am PC' })),
        h('span', { class: 'set-label t-ui', id: 'set-desktop-empty', text: 'Rechts, wenn kein Artikel offen ist' }),
        emptyChips,
        row('Fokusmodus', 'Liste einklappen, Text mittig · Taste F · Esc beendet',
          toggle(s.focus_mode === true, 'Fokusmodus', focusSwitchChange))),
      section('LESEN'),
      row('Gelesen beim Öffnen', 'Beim Öffnen als gelesen markieren',
        toggle(s.mark_read_on_open, 'Gelesen beim Öffnen', (v) => save({ mark_read_on_open: v }))),
      row('Bilder in der Liste', 'Vorschaubild rechts vom Titel',
        toggle(s.show_images, 'Bilder in der Liste', (v) => save({ show_images: v }))),
      section('QUELLEN', 'sec--gap'),
      // Nur Anzeige und nur für den Admin: beides legt der Server fest (kein Pfeil, nicht antippbar).
      admin ? [
        row('Abrufintervall', interval ? `Alle ${interval} Minuten · auf dem Server festgelegt` : 'Auf dem Server festgelegt'),
        row('Aufbewahrung', conf.retention_days ? `${conf.retention_days} Tage · auf dem Server festgelegt` : 'Auf dem Server festgelegt'),
      ] : null,
      h('a', { class: 'set-row', href: '#/quellen' },
        h('span', { class: 'set-row-text' },
          h('span', { class: 't-ui', text: 'Alle Zeitungen' }),
          h('span', { class: 't-caption', text: total ? `${enabled} von ${total} aktiv` : ' ' })),
        h('span', { class: 'chev' }, chevronRight(18))),
      h('div', { class: 'foot' },
        h('button', {
          type: 'button', class: 'pill pill--block', text: 'Abmelden',
          on: {
            click: async () => {
              try { await api('logout', { method: 'POST' }); } catch { /* trotzdem abmelden */ }
              state.authed = false;
              resetUserState();
              location.hash = '#/login';
            },
          },
        }),
        h('div', { class: 't-caption', text: footer })));
  }

  build();
  Promise.allSettled([loadSettings(), loadStatus()]).then(build);

  return { el, layout: 'page', bar: false, title: 'Einstellungen' };
}
