// Schalter: Pille aus rule-Kontur mit ink-Kreis; an: ink-Fläche mit on-ink-Kreis. Kein Blau.
import { h, toast } from './dom.js';

/**
 * @param {boolean} checked
 * @param {string} label            aria-label
 * @param {(value:boolean, el:HTMLElement)=>Promise|void} onChange
 *        Wirft onChange, springt der Schalter zurück und ein Hinweis erscheint.
 */
export function toggle(checked, label, onChange, { disabled = false } = {}) {
  const el = h('button', {
    type: 'button', class: 'switch', role: 'switch', 'aria-label': label,
    'aria-checked': checked ? 'true' : 'false', disabled,
  }, h('span', { class: 'switch-track' }, h('span', { class: 'switch-knob' })));
  el.addEventListener('click', async (e) => {
    e.preventDefault();
    e.stopPropagation();
    const next = el.getAttribute('aria-checked') !== 'true';
    setToggle(el, next);
    try {
      await onChange?.(next, el);
    } catch (err) {
      setToggle(el, !next);
      // Nicht als unbehandelte Ablehnung durchreichen; die meisten onChange melden selbst.
      if (!err || !err.toasted) toast(`Fehler: ${(err && (err.detail || err.message)) || 'unbekannt'}`);
    }
  });
  return el;
}

export function setToggle(el, value) {
  el.setAttribute('aria-checked', value ? 'true' : 'false');
}
