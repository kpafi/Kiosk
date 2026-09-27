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
