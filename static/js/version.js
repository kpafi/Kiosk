// Neue Version nach einem Deploy erkennen (reine Logik ohne DOM; auch in tests/test_frontend_js.py
// geprüft). Die Version ist der Build-Zeitstempel aus /api/build; lokal ist er leer, dann meldet
// sich nie etwas.

/** Ist `fresh` eine andere Version als die geladene (`known`)? Leere/fehlende Werte zählen nie. */
export function buildChanged(known, fresh) {
  return typeof known === 'string' && typeof fresh === 'string' && known !== '' && fresh !== ''
    && known !== fresh;
}

/**
 * Build-Kennung der geladenen Seite: der Server setzt sie beim Ausliefern von index.html als
 * data-build an <html>. Kam die Seite aus dem Cache des Service Workers (offline, Serverfehler, Frist
 * abgelaufen), steht dort der Stand von damals. null, wenn sie fehlt (lokal leer, ältere Kopie im Cache).
 * @param {{dataset?: object}|null} root  document.documentElement
 */
export function pageBuild(root) {
  const v = root && root.dataset ? root.dataset.build : null;
  return typeof v === 'string' && v !== '' && v !== 'dev' ? v : null;
}

/**
 * Fragt /api/build nach (höchstens alle minGapMs, force übergeht das) und meldet eine neue Version
 * über onNew(build): einmal, und nach remindMs noch einmal (wer „Später“ getippt hat, sieht den
 * Hinweis am nächsten Tag wieder). Fehler (offline) bleiben still.
 * @param {object} opts  {fetchBuild(): Promise<string>, onNew(build), minGapMs, remindMs, now()}
 * @returns {{setKnown(build): void, seen(build): boolean, check({force}?): Promise<boolean>}}
 *   setKnown: Version der geladenen Seite (beim Start); fehlt sie, gilt die erste Antwort als bekannt.
 *   seen: eine anderswo geholte Antwort von /api/build auswerten wie check (meldet ggf. onNew).
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

  function seen(build) {
    if (typeof build !== 'string') return false;
    if (known === null) { known = build; return false; }
    if (!buildChanged(known, build)) return false;
    if (build === announced && now() - announcedAt < remindMs) return false;
    announced = build;
    announcedAt = now();
    onNew(build);
    return true;
  }

  function check({ force = false } = {}) {
    if (running) return running;
    const t = now();
    if (!force && t - lastCheck < minGapMs) return Promise.resolve(false);
    lastCheck = t;
    running = (async () => {
      try {
        return seen(await fetchBuild());
      } catch {
        return false;
      } finally {
        running = null;
      }
    })();
    return running;
  }

  return { setKnown, seen, check };
}

/**
 * Stand beim Start festlegen und /api/build einmal fragen (app.js, registerServiceWorker).
 * Hat die Seite eine eigene Kennung (`own`, pageBuild), gilt die: Kam sie aus dem Cache des Service
 * Workers (offline, Serverfehler oder nach seiner Frist von 6 s, während /api/build danach normal
 * antwortet), ist das der alte Stand, und die Antwort meldet den neuen sofort (onNew). Ohne eigene
 * Kennung (Kopie im Cache von vor data-build, lokal) gilt die Antwort; ohne Antwort `cachedBuild(err)`,
 * der Stand des Service Workers, falls die Seite aus seinem Cache kam (sonst null).
 * @param {{setKnown(build): void, seen(build): boolean}} watcher  aus buildWatcher
 * @param {{own: string|null, fetchBuild(): Promise<string>, cachedBuild(err): string|null}} opts
 * @returns {Promise<string|null>}  Antwort von /api/build, null ohne Antwort
 */
export async function startBuild(watcher, { own, fetchBuild, cachedBuild = () => null }) {
  if (own) watcher.setKnown(own);
  let build;
  try {
    build = await fetchBuild();
  } catch (err) {
    const cached = own ? null : cachedBuild(err);
    if (cached) watcher.setKnown(cached);
    return null;
  }
  if (own) watcher.seen(build);
  else watcher.setKnown(build);
  return build;
}
