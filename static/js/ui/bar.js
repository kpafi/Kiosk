// Schnellzugriffsleiste (E2Liste unten): ALLE links, Chips scrollen, Plus rechts.
import { h } from './dom.js';
import { plus } from './icons.js';
import { navigate, on, state } from '../store.js';

const barEl = () => document.getElementById('bar');

function hrefFor(item) {
  if (item.type === 'source') return `#/s/${encodeURIComponent(item.id)}`;
  if (item.type === 'profile') return `#/?p=${encodeURIComponent(item.id)}`;
  return '#/';
}

function isActive(item) {
  const ctx = state.ctx;
  if (item.type === 'all') return ctx.type === 'all';
  return item.type === ctx.type && String(item.id) === String(ctx.id);
}

function shortLabel(text) {
  return String(text || '').toUpperCase().slice(0, 12);
}

/** Langes Drücken auf einen Chip öffnet das Sortieren der Leiste. */
function withLongPress(el) {
  let timer = 0, fired = false;
  const cancel = () => clearTimeout(timer);
  el.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    fired = false;
    timer = setTimeout(() => { fired = true; navigate('#/profile?sortieren=1'); }, 600);
  });
  el.addEventListener('pointerup', cancel);
  el.addEventListener('pointerleave', cancel);
  el.addEventListener('pointercancel', cancel);
  el.addEventListener('click', (e) => { if (fired) { e.preventDefault(); fired = false; } });
  el.addEventListener('contextmenu', (e) => { e.preventDefault(); cancel(); navigate('#/profile?sortieren=1'); });
  return el;
}

function barChip(item) {
  const active = isActive(item);
  const showCount = item.type !== 'all' && item.unread > 0;
  return withLongPress(h('a', {
    class: `bar-chip${item.type === 'all' ? ' bar-chip--all' : ''}${active ? ' is-on' : ''}`,
    href: hrefFor(item),
    'aria-current': active ? 'page' : null,
    // Vorleser: voller Name samt Zahl („DEUTSCHLAND, 622 ungelesen“), der Zähler selbst ist stumm.
    'aria-label': showCount ? `${item.label}, ${item.unread} ungelesen` : null,
    title: item.label,
    draggable: 'false',
  }, h('span', { class: 'bar-label', text: item.label }),
  showCount ? h('span', { class: 'bar-count t-counter', 'aria-hidden': 'true', text: item.unread > 999 ? '999+' : item.unread }) : null));
}

/** Rechter Rand ausblenden, solange rechts noch Chips versteckt sind. */
function paintFade(scroller) {
  scroller.classList.toggle('fade-end', scroller.scrollLeft + scroller.clientWidth < scroller.scrollWidth - 1);
}

export function renderBar() {
  const el = barEl();
  const items = [...(state.bar || [{ type: 'all', label: 'ALLE' }])];
  if (!items.some((i) => i.type === 'all')) items.unshift({ type: 'all', label: 'ALLE' });
  // Zeitungsansicht einer Quelle, die nicht in der Leiste ist: temporär einblenden.
  if (state.ctx.type === 'source' && !items.some(isActive)) {
    const s = state.sourceMap.get(state.ctx.id);
    items.splice(1, 0, {
      type: 'source', id: state.ctx.id, label: shortLabel(s ? s.short || s.name : state.ctx.id),
      unread: s ? s.unread : 0,
    });
  }
  const [all, ...rest] = [items.find((i) => i.type === 'all'), ...items.filter((i) => i.type !== 'all')];
  const scroller = h('div', { class: 'bar-scroll' }, rest.map(barChip));
  scroller.addEventListener('scroll', () => paintFade(scroller), { passive: true });
  el.replaceChildren(
    barChip(all),
    scroller,
    h('a', { class: 'icon-btn', href: '#/profile', 'aria-label': 'Zur Leiste hinzufügen' }, plus(20)),
  );
  // Aktiven Chip sichtbar machen (nur horizontal, die Seite scrollt nicht mit).
  const active = scroller.querySelector('.is-on');
  requestAnimationFrame(() => {
    if (active) {
      const left = active.offsetLeft - scroller.offsetLeft;
      if (left + active.offsetWidth > scroller.scrollLeft + scroller.clientWidth || left < scroller.scrollLeft) {
        scroller.scrollLeft = Math.max(0, left - 8);
      }
    }
    paintFade(scroller);
  });
}

export function showBar(visible) {
  const el = barEl();
  el.hidden = !visible;
  document.getElementById('shell').classList.toggle('has-bar', visible);
  if (visible) renderBar();
}

on('bar', () => { if (!barEl().hidden) renderBar(); });
window.addEventListener('resize', () => {
  const scroller = barEl().querySelector('.bar-scroll');
  if (scroller) paintFade(scroller);
}, { passive: true });
