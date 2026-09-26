// Klassisches Skript (kein Modul), lädt als erstes im <head>: setzt Farbschema und Textgröße aus
// der lokalen Kopie der Einstellungen, bevor das CSS malt (kein Aufblitzen des falschen Themas).
// Schlüssel und Standard wie in store.js (LS_SETTINGS, DEFAULT_SETTINGS).
(function () {
  var s = null;
  try { s = JSON.parse(localStorage.getItem('kiosk.settings')); } catch (e) { s = null; }
  s = s || {};
  var theme = s.theme === 'light' || s.theme === 'system' ? s.theme : 'dark';
  if (theme === 'system') {
    theme = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }
  var root = document.documentElement;
  root.setAttribute('data-theme', theme);
  root.setAttribute('data-text', s.text_size === 's' || s.text_size === 'l' ? s.text_size : 'm');
  var meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', theme === 'light' ? '#f2f2f2' : '#000000');
}());
