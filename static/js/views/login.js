// Anmelden (E2Login): Wortmarke, Tagline, Benutzername und Passwort als Pillen, Fehlerzeile mit
// blauem Punkt.
import { h } from '../ui/dom.js';
import { api } from '../api.js';

export function loginView({ onSuccess }) {
  const user = h('input', {
    class: 'input', type: 'text', name: 'username', autocomplete: 'username',
    autocapitalize: 'off', autocorrect: 'off', spellcheck: 'false',
    'aria-label': 'Benutzername', required: true, maxlength: 100, id: 'user',
  });
  const input = h('input', {
    class: 'input', type: 'password', name: 'password', autocomplete: 'current-password',
    'aria-label': 'Passwort', required: true, maxlength: 500, id: 'pw',
  });
  const button = h('button', { type: 'submit', class: 'pill pill--primary', text: 'Anmelden' });
  // Fehlerzeile hat immer ihre Höhe (nur unsichtbar), damit das Formular beim Fehler nicht springt.
  const error = h('div', { class: 'err-line err-line--idle t-caption', role: 'alert' });
  const form = h('form', { novalidate: true },
    h('label', { class: 'field', for: 'user' },
      h('span', { class: 'field-label t-caption', text: 'Benutzername' }), user),
    h('label', { class: 'field', for: 'pw' },
      h('span', { class: 'field-label t-caption', text: 'Passwort' }), input),
    button, error);

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const username = user.value.trim();
    if (!username) { user.focus(); return; }
    if (!input.value) { input.focus(); return; }
    button.disabled = true;
    error.classList.add('err-line--idle');
    error.textContent = '';
    try {
      await api('login', { method: 'POST', body: { username, password: input.value } });
      input.value = '';
      onSuccess();
    } catch (err) {
      error.textContent = err.detail || 'Anmelden fehlgeschlagen';
      error.classList.remove('err-line--idle');
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

  // Passwort-Manager füllen oft den Benutzernamen: dann gleich ins Passwortfeld.
  return {
    el, layout: 'page', bar: false, title: 'Anmelden',
    onShow: () => (user.value ? input : user).focus(),
  };
}
