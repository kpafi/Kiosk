// Eine Einstellung nacheinander speichern (reine Logik ohne DOM; auch in tests/test_frontend_js.py
// geprüft). Höchstens eine Anfrage zugleich; ändert sich der Wert, während sie läuft, folgt danach
// genau eine mit dem letzten Wert, so dass der Server am Ende den Stand des Browsers hat. Scheitert
// eine Anfrage, gilt wieder der zuletzt vom Server bestätigte Wert (nicht der vorige Zwischenstand).
// reset() (Abmelden, Benutzerwechsel) beendet ein laufendes Speichern, ohne danach noch etwas zu setzen.

/**
 * @param {object} opts
 *   get()          aktueller lokaler Wert
 *   send(value)    speichert, liefert den vom Server bestätigten Wert (Promise)
 *   apply(value, err)  lokalen Wert setzen (Zurückrollen; err bei Fehler, sonst undefined)
 * @returns {{remember(): void, flush(): Promise<boolean>, current(): Promise<boolean>|null, reset(): void}}
 *   remember() vor jeder lokalen Änderung aufrufen (merkt den bestätigten Ausgangswert),
 *   flush() danach; true, wenn der Server am Ende den lokalen Wert hat. current() ist das laufende
 *   Speichern (oder null).
 */
export function serialSaver({ get, send, apply }) {
  let confirmed = null;   // bestätigter Wert, solange gespeichert wird
  let known = false;
  let running = null;
  let gen = 0;            // Generation: reset() macht laufende Durchgänge wirkungslos

  function forget() {
    known = false;
    confirmed = null;
  }

  function remember() {
    if (running || known) return;
    confirmed = get();
    known = true;
  }

  function flush() {
    if (running) return running;
    if (!known || get() === confirmed) {   // nichts zu senden
      forget();
      return Promise.resolve(true);
    }
    const mine = gen;
    const job = (async () => {
      try {
        while (get() !== confirmed) {
          const want = get();
          let got;
          try {
            got = await Promise.resolve().then(() => send(want));   // immer asynchron
          } catch (err) {
            if (mine !== gen) return false;   // inzwischen abgemeldet: nichts mehr anfassen
            apply(confirmed, err);
            return false;
          }
          if (mine !== gen) return false;
          confirmed = got === undefined ? want : got;
          if (confirmed !== want && get() === want) {   // Server hat etwas anderes gespeichert
            apply(confirmed);
            return false;
          }
        }
        return true;
      } finally {
        if (mine === gen) {
          running = null;
          forget();
        }
      }
    })();
    // Die Schleife wartet mindestens einmal, der finally-Block läuft also erst nach dieser Zuweisung.
    running = job;
    return job;
  }

  return {
    remember,
    flush,
    current: () => running,
    reset() {
      gen += 1;
      running = null;
      forget();
    },
  };
}
