// Neue Version nach einem Deploy erkennen (reine Logik ohne DOM; auch in tests/test_frontend_js.py
// geprüft). Die Version ist der Build-Zeitstempel aus /api/build; lokal ist er leer, dann meldet
// sich nie etwas.

/** Ist `fresh` eine andere Version als die geladene (`known`)? Leere/fehlende Werte zählen nie. */
export function buildChanged(known, fresh) {
  return typeof known === 'string' && typeof fresh === 'string' && known !== '' && fresh !== ''
    && known !== fresh;
}

/**
 * Fragt /api/build nach (höchstens alle minGapMs, force übergeht das) und meldet eine neue Version
 * über onNew(build): einmal, und nach remindMs noch einmal (wer „Später“ getippt hat, sieht den
 * Hinweis am nächsten Tag wieder). Fehler (offline) bleiben still.
 * @param {object} opts  {fetchBuild(): Promise<string>, onNew(build), minGapMs, remindMs, now()}
 * @returns {{setKnown(build): void, check({force}?): Promise<boolean>}}
 *   setKnown: Version der geladenen Seite (beim Start); fehlt sie, gilt die erste Antwort als bekannt.
 */
export function buildWatcher({
  fetchBuild, onNew, minGapMs = 60 * 1000, remindMs = 24 * 60 * 60 * 1000, now = () => Date.now(),
}) {
  let known = null;
  let announced = null;
  let announcedAt = -Infinity;
  let lastCheck = -Infinity;
  let running = null;

  function setKnown(build) {
    if (known === null && typeof build === 'string') known = build;
    lastCheck = now();
  }

  function check({ force = false } = {}) {
    if (running) return running;
    const t = now();
    if (!force && t - lastCheck < minGapMs) return Promise.resolve(false);
    lastCheck = t;
    running = (async () => {
      try {
        const build = await fetchBuild();
        if (typeof build !== 'string') return false;
        if (known === null) { known = build; return false; }
        if (!buildChanged(known, build)) return false;
        if (build === announced && now() - announcedAt < remindMs) return false;
        announced = build;
        announcedAt = now();
        onNew(build);
        return true;
      } catch {
        return false;
      } finally {
        running = null;
      }
    })();
    return running;
  }

  return { setKnown, check };
}
