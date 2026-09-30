// Layout-Regeln als reine Funktionen (ohne DOM; auch in tests/test_frontend_js.py geprüft).

/**
 * Leiste unten zeigen? Nur zu Listen-Ansichten mit Leiste; mit offenem Artikel nur am Desktop und
 * nicht im Fokusmodus.
 * @param {object} s  {viewLayout, viewBar, layout, desktop, focus}
 */
export function barWanted({ viewLayout, viewBar, layout, desktop, focus }) {
  if (viewLayout !== 'list' || !viewBar) return false;
  if (layout === 'list') return true;
  return layout === 'article' && !!desktop && !focus;
}

/** Tagesübersicht/Titelseite rechts sichtbar? Desktop, Listen-Layout, angemeldet, kein Artikel offen. */
export function homeVisible({ authed, readerOpen, layout, desktop }) {
  return authed === true && !readerOpen && layout === 'list' && !!desktop;
}

/** Startseite nach dem Anmelden: am Handy die Titelseite, am Desktop die Liste (rechts daneben
 *  Übersicht oder Titelseite). */
export function startHash(desktop) {
  return desktop ? '#/' : '#/titelseite';
}

/**
 * Beim Öffnen der App am Handy mit der Titelseite beginnen? Nur ohne eigenes Ziel in der Adresse
 * ('', '#', '#/') und nur beim echten Öffnen: Neuladen und Zurück/Vor bleiben, wo man war (etwa auf
 * der Liste ALLE).
 * @param {object} s  {hash, desktop, navType}  navType aus performance.getEntriesByType('navigation')
 */
export function opensOnFront({ hash, desktop, navType }) {
  if (desktop || !['', '#', '#/'].includes(hash || '')) return false;
  return navType !== 'reload' && navType !== 'back_forward';
}
