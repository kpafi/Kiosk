// Kleine DOM-Helfer. Texte werden immer als Textknoten gesetzt (nie als HTML).

/**
 * Element bauen: h('a', {class: 'x', href: '#/', text: 'Titel', on: {click: fn}}, kind1, kind2)
 * Attribute mit Wert null/undefined/false werden weggelassen, true setzt ein leeres Attribut.
 */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'text') el.textContent = value;
    else if (key === 'on') for (const [ev, fn] of Object.entries(value)) el.addEventListener(ev, fn);
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key === 'style' && typeof value === 'object') Object.assign(el.style, value);
    else if (key === 'value' || key === 'checked' || key === 'disabled' || key === 'hidden') el[key] = value;
    else el.setAttribute(key, value === true ? '' : String(value));
  }
  append(el, ...children);
  return el;
}

/** Kinder anhängen: Strings/Zahlen als Textknoten, Arrays flach, null/false überspringen. */
export function append(el, ...children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

/** Inhalt ersetzen. */
export function replace(el, ...children) {
  el.replaceChildren();
  return append(el, ...children);
}

/** Kurzer Hinweis unten (Status-Rückmeldung). */
let toastTimer = 0;
export function toast(message) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
}

/** Mono-Hinweis, z. B. wenn eine Backend-Route (noch) fehlt. */
export function hint(text, extraClass = '') {
  return h('p', { class: `hint t-caption ${extraClass}`.trim(), text });
}

export function isDesktop() {
  return window.matchMedia('(min-width: 900px)').matches;
}

/** Eingabefelder, in denen Tastenkürzel nicht greifen dürfen. */
export function isTyping(target) {
  if (!(target instanceof Element)) return false;
  return !!target.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"]');
}
