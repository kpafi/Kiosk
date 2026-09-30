// Titelseite am Handy (#/titelseite): Kopf wie die Liste, darunter dieselbe Titelseite wie rechts am
// Desktop (Aufmacher, je Profil bis zu drei Geschichten). Laden, Neuladen und Uhr wie dort (readerHome
// in home.js). Die App beginnt am Handy hier; am Desktop leitet app.js zur Liste um.
import { h } from '../ui/dom.js';
import { on, state } from '../store.js';
import { homeHeader } from './list.js';
import { readerHome } from './home.js';
import { frontpageArticles } from './frontpage.js';

export function frontView() {
  // Kein Chip der Leiste ist aktiv. Keine Kontext-Liste: „Alles als gelesen markieren“ fehlt im Menü,
  // „Nächster“ nimmt den nächsten ungelesenen Artikel dieser Seite.
  state.ctx = { type: 'front' };
  state.nextCtx = 'local';
  state.listItems = [];
  state.listIsCtx = false;

  const home = readerHome({
    kind: 'frontpage',
    desktopOnly: false,
    // Artikel der Seite in Anzeigereihenfolge: „Nächster“ und Sofort-Vorschau der Leseansicht.
    onData: (data) => { state.listItems = frontpageArticles(data); },
  });

  // Gelesen (Leseansicht, Taste m): Titel gleich grau, auch während die Seite verdeckt ist und nicht
  // lädt; neu geladen wird erst beim Zurückkommen (onShow).
  const offFlags = on('flags', (patch) => {
    if (!('read' in patch)) return;
    const link = `a[href="#/a/${Number(patch.id)}"]`;
    for (const t of home.el.querySelectorAll(`${link} .fp-item-title, ${link} .fp-lead-title`)) {
      t.classList.toggle('is-read', !!patch.read);
    }
  });

  // Geladen wird erst, wenn die Seite zu sehen ist: onShow (Router) oder onCover(false) (Profil-Blatt).
  const el = h('div', { class: 'view-list view-front' }, homeHeader({ current: true }), home.el);

  return {
    el,
    layout: 'list',
    bar: true,
    title: 'Titelseite',
    /** Zurück zur Seite: neu laden (ohne Ladeanzeige, Gezeigtes bleibt bis zur Antwort stehen). */
    onShow() { home.setVisible(true); },
    /** Handy: Leseansicht liegt darüber (true). Solange nicht laden (wie die Liste), keine Uhr; mit
     *  false (Profil-Blatt statt Artikel) wieder sichtbar. */
    onCover(covered) { home.setVisible(!covered); },
    destroy() {
      offFlags();
      home.destroy();
    },
  };
}
