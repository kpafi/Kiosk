// Profil-Editor (E2ProfilEditor): Name, Suchbegriffe, Sichtbar (oben/unten), Quellen nach Land.
import { h, hint, replace, toast } from '../ui/dom.js';
import { close as closeIcon, plus } from '../ui/icons.js';
import { subHeader } from '../ui/header.js';
import { toggle } from '../ui/toggle.js';
import { api } from '../api.js';
import { countryName, groupByCountry } from '../format.js';
import { parseHash } from '../route.js';
import {
  emit, goBack, loadBar, loadProfiles, loadSources, replaceHash, state,
} from '../store.js';

const MAX_TERMS = 20;

export function profileEditorView(route) {
  const isNew = route.id === 'neu';
  const page = h('div', { class: 'page editor' });
  const saveBtn = h('button', { type: 'button', class: 'pill pill--primary', text: 'Speichern', on: { click: save } });
  const header = subHeader({ title: 'PROFIL', fallback: '#/profile', actions: [saveBtn] });
  const el = h('div', { class: 'view-page' }, header, page);

  // Arbeitskopie
  let profile = null;
  // Neu: oben bei den Filtern an, unten in der Leiste aus.
  let draft = { name: '', terms: [], in_bar: false, in_chips: true, sources: null };
  let addingTerm = false;
  let confirmDelete = false;
  let error = '';
  let busy = false;    // Speichern oder Löschen läuft (Enter im Namensfeld umgeht den gesperrten Knopf)
  let alive = true;    // false nach destroy(): nach spätem Erfolg nicht mehr zurücknavigieren

  function load() {
    Promise.allSettled([state.sources ? null : loadSources(), isNew ? null : loadProfiles()]).then(([, loaded]) => {
      if (!isNew) {
        profile = (state.profiles || []).find((p) => String(p.id) === String(route.id)) || null;
        if (!profile) {
          const err = loaded.status === 'rejected' ? loaded.reason : null;
          replace(page, hint(err ? `Fehler: ${err.detail || err.message}` : 'Profil nicht gefunden.'));
          saveBtn.disabled = true;
          return;
        }
        draft = {
          name: profile.name,
          terms: [...(profile.terms || [])],
          in_bar: !!profile.in_bar,
          in_chips: profile.in_chips !== false,
          sources: profile.sources ? [...profile.sources] : null,
        };
      }
      build();
    });
  }

  function validate() {
    const name = draft.name.trim();
    if (!name || name.length > 40) return 'Name: 1 bis 40 Zeichen';
    if (draft.terms.length > MAX_TERMS) return `Höchstens ${MAX_TERMS} Begriffe`;
    if (Array.isArray(draft.sources) && !draft.sources.length) return 'Wähle mindestens eine Zeitung';
    return '';
  }

  async function save() {
    if (busy || (!isNew && !profile)) return;
    error = validate();
    if (error) { build(); return; }
    busy = true;
    saveBtn.disabled = true;
    const body = {
      name: draft.name.trim(), terms: draft.terms, in_bar: draft.in_bar, in_chips: draft.in_chips,
      sources: draft.sources,
    };
    try {
      if (isNew) await api('profiles', { method: 'POST', body });
      else {
        // Nur Geändertes schicken: Ein Schalter, der inzwischen anderswo umgelegt wurde (Profil-Blatt,
        // anderes Gerät), bleibt sonst nicht stehen.
        const changes = changedFields(body);
        if (Object.keys(changes).length) await api(`profiles/${profile.id}`, { method: 'PATCH', body: changes });
      }
      await Promise.allSettled([loadProfiles(), loadBar()]);
      emit('profiles');
      toast('Profil gespeichert');
      // Zurück wie mit der Zurück-Taste (zum Profil-Blatt), ohne doppelten #/profile-Eintrag. busy
      // bleibt gesetzt: history.back() wechselt die Ansicht erst später, bis dahin kein zweites Speichern.
      if (alive) goBack('#/profile');
    } catch (err) {
      busy = false;
      saveBtn.disabled = false;
      error = err.detail || err.message;
      build();
    }
  }

  /** Felder von `body`, die sich gegenüber dem geladenen Profil geändert haben. */
  function changedFields(body) {
    // Quellen ohne Reihenfolge vergleichen (Auswahl per Schalter ordnet sie um); null = alle.
    const srcKey = (s) => (s ? JSON.stringify([...s].sort()) : null);
    const loaded = {
      name: profile.name, terms: JSON.stringify(profile.terms || []), in_bar: !!profile.in_bar,
      in_chips: profile.in_chips !== false, sources: srcKey(profile.sources),
    };
    const now = { ...body, terms: JSON.stringify(body.terms), sources: srcKey(body.sources) };
    return Object.fromEntries(Object.keys(loaded).filter((k) => now[k] !== loaded[k]).map((k) => [k, body[k]]));
  }

  async function remove(e) {
    if (busy) return;
    busy = true;
    saveBtn.disabled = true;
    if (e && e.currentTarget) e.currentTarget.disabled = true;
    try {
      await api(`profiles/${profile.id}`, { method: 'DELETE' });
      await Promise.allSettled([loadProfiles(), loadBar()]);
      emit('profiles');
      if (state.ctx.type === 'profile' && String(state.ctx.id) === String(profile.id)) state.lastListHash = '#/';
      toast('Profil gelöscht');
      // Aus dem Profil-Blatt geöffnet: zurück wie beim Speichern (sonst stünde das Blatt zweimal
      // im Verlauf). Sonst nicht zurück: der vorige Eintrag kann eine Liste des gelöschten Profils sein.
      // busy bleibt gesetzt (wie beim Speichern): Das Profil gibt es nicht mehr.
      if (!alive) return;
      if (parseHash(state.prevHash || '').name === 'profiles') goBack('#/profile');
      else replaceHash('#/profile');
    } catch (err) {
      busy = false;
      saveBtn.disabled = false;
      error = err.status === 409 ? 'Vorgegebene Profile lassen sich nicht löschen.' : err.detail || err.message;
      confirmDelete = false;
      build();
    }
  }

  function addTerm(raw) {
    const term = raw.trim().replace(/"/g, '');
    if (!term) return;
    if (term.length > 40) { error = 'Begriffe: höchstens 40 Zeichen'; return; }
    if (draft.terms.length >= MAX_TERMS) { error = `Höchstens ${MAX_TERMS} Begriffe`; return; }
    if (!draft.terms.some((t) => t.toLowerCase() === term.toLowerCase())) draft.terms.push(term);
  }

  function termChips() {
    return draft.terms.map((t, i) => h('span', { class: 'term' },
      h('span', { text: t }),
      h('button', {
        type: 'button', class: 'term-x', 'aria-label': `${t} entfernen`,
        on: { click: () => { draft.terms.splice(i, 1); build(); } },
      }, closeIcon(14))));
  }

  function termsBlock() {
    const chips = termChips();
    let adder;
    if (addingTerm) {
      adder = h('input', {
        class: 'input term-input', 'aria-label': 'Neuer Suchbegriff', placeholder: 'Begriff', maxlength: 40,
        enterkeyhint: 'done', autocomplete: 'off',
      });
      adder.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); addTerm(adder.value); build(); focusAdder(); }
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); addingTerm = false; build(); }
      });
      adder.addEventListener('blur', () => {
        if (adder.value.trim()) {
          // Übernehmen und Feld leeren; nur die Begriffe neu zeichnen, der Fokus bleibt, wo er ist.
          addTerm(adder.value);
          adder.value = '';
          const wrap = adder.parentElement;
          if (wrap) {
            wrap.querySelectorAll('.term').forEach((n) => n.remove());
            wrap.prepend(...termChips());
          }
        }
        setTimeout(() => { if (addingTerm && !page.contains(document.activeElement)) { addingTerm = false; build(); } }, 150);
      });
    } else {
      adder = h('button', {
        type: 'button', class: 'icon-btn term-add', 'aria-label': 'Begriff hinzufügen',
        on: { click: () => { addingTerm = true; build(); focusAdder(); } },
      }, plus(20));
    }
    return h('div', { class: 'field' },
      h('span', { class: 'field-label t-caption', text: 'Suchbegriffe' }),
      h('div', { class: 'terms' }, chips, adder));
  }

  function focusAdder() {
    page.querySelector('.term-input')?.focus();
  }

  function sourcesBlock() {
    const enabled = (state.sources || []);
    const all = draft.sources === null;
    // Eigene Beschriftung, damit der Schalter nicht zur Gruppe „Sichtbar“ darüber zu gehören scheint.
    const out = [
      h('div', { class: 'field', role: 'group', 'aria-label': 'Zeitungen' },
        h('span', { class: 'field-label t-caption', 'aria-hidden': 'true', text: 'Zeitungen' }),
        h('div', { class: 'inline-row' },
          h('span', { class: 't-ui', text: 'Alle aktiven Zeitungen' }),
          toggle(all, 'Alle aktiven Zeitungen', (v) => {
            // Wie „alle“: ohne Quellen mit in_all: false (z. B. heise Security); einzeln wählbar.
            draft.sources = v ? null : enabled.filter((s) => s.enabled && s.in_all !== false).map((s) => s.id);
            build();
          }))),
    ];
    if (all) {
      out.push(h('p', { class: 't-caption muted', text: 'Auch Zeitungen, die du später einschaltest. Ohne Quellen, die nicht in ALLE stehen (heise Security).' }));
      return out;
    }
    const chosen = new Set(draft.sources);
    for (const [cc, list] of groupByCountry(enabled)) {
      const active = list.filter((s) => s.enabled);
      // „Alle wählen“ nimmt Quellen mit in_all: false nicht mit; sie lassen sich einzeln einschalten.
      const bulk = active.filter((s) => s.in_all !== false);
      const allOn = () => bulk.length > 0 && bulk.every((s) => chosen.has(s.id));
      // Beschriftung folgt dem Zustand, auch nach einzelnen Schaltern.
      const countryBtn = bulk.length ? h('button', {
        type: 'button', class: 'btn-sm', text: allOn() ? 'Keine' : 'Alle wählen',
        on: {
          click: () => {
            const on = allOn();
            for (const s of bulk) { if (on) chosen.delete(s.id); else chosen.add(s.id); }
            draft.sources = [...chosen];
            build();
          },
        },
      }) : null;
      const paintCountry = () => { if (countryBtn) countryBtn.textContent = allOn() ? 'Keine' : 'Alle wählen'; };
      out.push(h('div', { class: 'sec-row', style: { paddingTop: '12px' } },
        h('h2', { class: 't-section', text: countryName(cc).toUpperCase() }),
        countryBtn));
      for (const s of list) {
        out.push(h('div', { class: `set-row set-row--52${s.enabled ? '' : ' is-off'}` },
          h('span', { class: 'set-row-text' }, h('span', { class: 't-ui', text: s.name })),
          h('span', { class: 't-caption muted', text: s.enabled ? s.country : `${s.country} · aus` }),
          s.enabled
            ? toggle(chosen.has(s.id), `${s.name} im Profil`, (v) => {
              if (v) chosen.add(s.id); else chosen.delete(s.id);
              draft.sources = [...chosen];
              paintCountry();
            })
            : toggle(false, `${s.name} ist aus`, null, { disabled: true })));
      }
    }
    return out;
  }

  // Wo das Profil erscheint: oben als Filterchip der Liste, unten in der Schnellzugriffsleiste.
  function showBlock() {
    const row = (label, key) => h('div', { class: 'inline-row' },
      h('span', { class: 't-ui', text: label }),
      toggle(draft[key], `${label} anzeigen`, (v) => { draft[key] = v; }));
    return h('div', { class: 'field', role: 'group', 'aria-label': 'Sichtbar' },
      h('span', { class: 'field-label t-caption', 'aria-hidden': 'true', text: 'Sichtbar' }),
      h('div', {},
        row('Oben bei den Filtern', 'in_chips'),
        row('Unten in der Leiste', 'in_bar')));
  }

  function build() {
    const nameInput = h('input', {
      class: 'input', value: draft.name, maxlength: 40, autocomplete: 'off', 'aria-label': 'Name',
    });
    nameInput.addEventListener('input', () => { draft.name = nameInput.value; });
    nameInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); save(); } });

    const deleteBlock = !isNew && profile && !profile.builtin
      ? (confirmDelete
        ? h('div', { class: 'confirm', style: { marginTop: '24px' } },
          h('span', { class: 'confirm-text t-caption', text: 'Profil wirklich löschen?' }),
          h('button', { type: 'button', class: 'pill', text: 'Abbrechen', on: { click: () => { confirmDelete = false; build(); } } }),
          h('button', { type: 'button', class: 'pill pill--primary', text: 'Löschen', on: { click: remove } }))
        : h('button', {
          type: 'button', class: 'pill pill--block', style: { marginTop: '24px' }, text: 'Profil löschen',
          on: { click: () => { confirmDelete = true; build(); } },
        }))
      : null;

    replace(page,
      h('label', { class: 'field' }, h('span', { class: 'field-label t-caption', text: 'Name' }), nameInput),
      termsBlock(),
      showBlock(),
      error ? h('div', { class: 'err-line t-caption', role: 'alert', text: error }) : null,
      sourcesBlock(),
      deleteBlock);
    error = '';
    if (isNew && !draft.name) nameInput.focus({ preventScroll: true });
  }

  load();
  return {
    el, layout: 'page', bar: false, title: 'Profil',
    destroy() { alive = false; },
  };
}
