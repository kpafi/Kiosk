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
    // Vorleser: voller Name samt Zahl („DEUTSCHLAND, 62 ungelesen heute“), der Zähler selbst ist stumm.
    'aria-label': showCount ? `${item.label}, ${item.unread} ungelesen heute` : null,
    title: item.label,
    draggable: 'false',
  }, h('span', { class: 'bar-label', text: item.label }),
  showCount ? h('span', { class: 'bar-count t-counter', 'aria-hidden': 'true', text: item.unread > 999 ? '999+' : item.unread }) : null));
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
  // Scroll-Lage über das Neuzeichnen retten, sonst springt die Leiste beim Tipp auf einen Chip rechts
  // an den Anfang zurück.
  const oldScroller = el.querySelector('.bar-scroll');
  const scrollLeft = oldScroller ? oldScroller.scrollLeft : 0;
  const scroller = h('div', { class: 'bar-scroll' }, rest.map(barChip));
  el.replaceChildren(
    barChip(all),
    scroller,
    h('a', { class: 'icon-btn', href: '#/profile', 'aria-label': 'Zur Leiste hinzufügen' }, plus(20)),
  );
  scroller.scrollLeft = scrollLeft;
  // Aktiven Chip nur dann sanft ins Bild holen (nur horizontal, die Seite scrollt nicht mit), wenn er
  // außerhalb liegt; 12 px Luft wie scroll-padding.
  const active = scroller.querySelector('.is-on');
  if (!active) return;
  requestAnimationFrame(() => {
    const left = active.offsetLeft - scroller.offsetLeft;
    if (left + active.offsetWidth > scroller.scrollLeft + scroller.clientWidth || left < scroller.scrollLeft) {
      const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
      scroller.scrollTo({ left: Math.max(0, left - 12), behavior: reduce ? 'auto' : 'smooth' });
    }
  });
}

export function showBar(visible) {
  const el = barEl();
  el.hidden = !visible;
  document.getElementById('shell').classList.toggle('has-bar', visible);
  if (visible) renderBar();
}

on('bar', () => { if (!barEl().hidden) renderBar(); });
