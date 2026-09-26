// Anmelden (E2Login): Wortmarke, Tagline, Passwort als Pille, Fehlerzeile mit blauem Punkt.
import { h } from '../ui/dom.js';
import { api } from '../api.js';

export function loginView({ onSuccess }) {
  const input = h('input', {
    class: 'input', type: 'password', name: 'password', autocomplete: 'current-password',
    'aria-label': 'Passwort', required: true, maxlength: 500, id: 'pw',
  });
  const button = h('button', { type: 'submit', class: 'pill pill--primary', text: 'Anmelden' });
  const error = h('div', { class: 'err-line t-caption', role: 'alert', hidden: true });
  const form = h('form', { novalidate: true },
    h('label', { class: 'field', for: 'pw' },
      h('span', { class: 'field-label t-caption', text: 'Passwort' }), input),
    button, error);

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!input.value) { input.focus(); return; }
    button.disabled = true;
    error.hidden = true;
    try {
      await api('login', { method: 'POST', body: { password: input.value } });
      input.value = '';
      onSuccess();
    } catch (err) {
      error.textContent = err.detail || 'Anmelden fehlgeschlagen';
      error.hidden = false;
      input.select();
    } finally {
      button.disabled = false;
    }
  });

  const el = h('div', { class: 'login' },
    h('div', {},
      h('h1', { class: 'login-mark', text: 'KIOSK' }),
      h('p', { class: 'login-tag t-meta', text: 'Die Welt, ein Stapel Zeitungen.' })),
    form,
    h('div', { class: 'login-foot t-caption', text: `Privat · ${location.host}` }));

  return { el, layout: 'page', bar: false, title: 'Anmelden', onShow: () => input.focus() };
}
