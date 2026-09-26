// Tastatur für Liste und Leseansicht: j/k blättern, Enter öffnen, m/s/l Flags, / Suche.
import { isTyping, toast } from '../ui/dom.js';
import { isSheetOpen } from '../ui/sheet.js';
import { navigate, setFlag, state } from '../store.js';

function rows() {
  return [...document.querySelectorAll('#view .row')];
}

function focusedRow() {
  const active = document.activeElement;
  return active && active.classList && active.classList.contains('row') ? active : null;
}

function moveFocus(delta) {
  const list = rows();
  if (!list.length) return;
  const current = focusedRow() || document.querySelector('#view .row.is-open');
  let index = current ? list.indexOf(current) + delta : delta > 0 ? 0 : list.length - 1;
  index = Math.max(0, Math.min(list.length - 1, index));
  const el = list[index];
  el.focus({ preventScroll: true });
  el.scrollIntoView({ block: 'nearest' });
}

/** Ziel für m/s/l: fokussierte Zeile, sonst der geöffnete Artikel. */
function targetItem() {
  const row = focusedRow();
  if (row && row._item) return row._item;
  if (state.openArticleId) {
    return state.openArticle || state.listItems.find((it) => it.id === state.openArticleId) || null;
  }
  return null;
}

const FLAG_KEYS = { m: ['read', 'read'], s: ['star', 'starred'], l: ['later', 'later'] };

export function installKeyboard() {
  document.addEventListener('keydown', (e) => {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
    if (isTyping(e.target) || isSheetOpen()) return;
    if (!state.authed) return;
    const key = e.key;
    if (key === 'j' || key === 'k') {
      e.preventDefault();
      moveFocus(key === 'j' ? 1 : -1);
    } else if (key === '/') {
      e.preventDefault();
      navigate('#/suche');
    } else if (FLAG_KEYS[key]) {
      const item = targetItem();
      if (!item) return;
      e.preventDefault();
      const [flag, field] = FLAG_KEYS[key];
      setFlag(item.id, flag, !item[field]).catch((err) => toast(`Fehler: ${err.detail || err.message}`));
    }
  });
}
