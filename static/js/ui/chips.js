// Chips (Filter, Reiter).
import { h } from './dom.js';

export function chip(label, { on = false, onClick, href, ariaPressed = true } = {}) {
  const attrs = { class: `chip${on ? ' is-on' : ''}`, text: label };
  if (href) return h('a', { ...attrs, href, 'aria-current': on ? 'true' : null });
  return h('button', {
    ...attrs, type: 'button', 'aria-pressed': ariaPressed ? String(on) : null, on: { click: onClick },
  });
}

/** Gruppe sich gegenseitig ausschließender Chips. options: [{value, label}] */
export function chipGroup(options, current, onSelect) {
  return options.map((o) => chip(o.label, { on: o.value === current, onClick: () => onSelect(o.value) }));
}
