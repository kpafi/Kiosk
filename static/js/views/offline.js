// Keine Verbindung zum Server beim Start: Hinweis statt Anmeldung.
import { h } from '../ui/dom.js';
import { matrix } from '../ui/empty.js';

export function offlineView({ onRetry, status = 0 }) {
  async function retry() {
    if (button.disabled) return;
    button.disabled = true;
    try { await onRetry(); } finally { button.disabled = false; }
  }
  const button = h('button', {
    type: 'button', class: 'pill pill--primary', text: 'Erneut versuchen', on: { click: retry },
  });
  // Verbindung wieder da (Browser meldet online): selbst erneut versuchen.
  window.addEventListener('online', retry);
  const el = h('div', { class: 'login offline' },
    h('div', {},
      matrix(),
      h('h1', { class: 'login-mark', text: 'OFFLINE' }),
      h('p', { class: 't-teaser', text: status
        ? `Der Server antwortet gerade nicht (${status}). Versuche es gleich noch einmal.`
        : 'Der Kiosk ist gerade nicht erreichbar. Prüfe die Verbindung und versuche es gleich noch einmal.' })),
    button,
    h('div', { class: 'login-foot t-caption', text: `Privat · ${location.host}` }));
  return {
    el, layout: 'page', bar: false, title: 'Offline', onShow: () => button.focus(),
    destroy() { window.removeEventListener('online', retry); },
  };
}
