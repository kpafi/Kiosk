// Chips (Filter, Reiter) und die Chipzeile.
import { h } from './dom.js';

export function chip(label, { on = false, onClick, href, ariaPressed = true } = {}) {
  const attrs = { class: `chip${on ? ' is-on' : ''}`, text: label };
  if (href) return h('a', { ...attrs, href, 'aria-current': on ? 'true' : null });
  return h('button', {
    ...attrs, type: 'button', 'aria-pressed': ariaPressed ? String(on) : null, on: { click: onClick },
  });
}

export function chipRow(...children) {
  return h('div', { class: 'chips' }, ...children);
}

/** Gruppe sich gegenseitig ausschließender Chips. options: [{value, label}] */
export function chipGroup(options, current, onSelect) {
  return options.map((o) => chip(o.label, { on: o.value === current, onClick: () => onSelect(o.value) }));
}
