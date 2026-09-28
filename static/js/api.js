// fetch-Hülle für /api/. 401 → Login-Screen (über setUnauthorizedHandler).
// Schreibende Aufrufe schicken immer Content-Type: application/json.

export class ApiError extends Error {
  constructor(status, detail, { timedOut = false } = {}) {
    super(detail || `HTTP ${status}`);
    this.status = status;
    this.detail = detail || `HTTP ${status}`;
    this.timedOut = timedOut;   // Frist abgelaufen (Server evtl. nur langsam), kein Netzfehler
  }
}

let onUnauthorized = () => {};
export function setUnauthorizedHandler(fn) {
  onUnauthorized = fn;
}

function detailText(detail) {
  if (!detail) return '';
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg || JSON.stringify(d)).join(' · ');
  return JSON.stringify(detail);
}

export const API_TIMEOUT_MS = 15000;         // länger wartet kein Lesen (hängende Verbindung, Funkloch)
// Schreiben (read-all, refresh, login …) darf länger dauern: Bricht der Browser zu früh ab, hat der
// Server es womöglich trotzdem getan, die Oberfläche meldet aber „Keine Verbindung“.
export const API_WRITE_TIMEOUT_MS = 45000;

/** Frist für eine Anfrage ohne eigene Angabe: Lesen 15 s, Schreiben 45 s. */
export function deadlineFor(method = 'GET') {
  return method === 'GET' || method === 'HEAD' ? API_TIMEOUT_MS : API_WRITE_TIMEOUT_MS;
}

/**
 * Signal des Aufrufers mit einer Frist verbinden. Eigener AbortController statt AbortSignal.any/
 * AbortSignal.timeout, damit es auch in Browsern ohne diese beiden geht und ein Abbruch durch die
 * Frist (timedOut() true) vom Abbruch durch den Aufrufer unterscheidbar bleibt.
 * @returns {{signal: AbortSignal, timedOut(): boolean, done(): void}}  done() nach dem Lesen der Antwort
 */
export function deadlineSignal(callerSignal, ms = API_TIMEOUT_MS) {
  const ctrl = new AbortController();
  let expired = false;
  const onAbort = () => ctrl.abort(callerSignal.reason);
  if (callerSignal) {
    if (callerSignal.aborted) ctrl.abort(callerSignal.reason);
    else callerSignal.addEventListener('abort', onAbort, { once: true });
  }
  const timer = setTimeout(() => { expired = true; ctrl.abort(); }, ms);
  return {
    signal: ctrl.signal,
    timedOut: () => expired && !(callerSignal && callerSignal.aborted),
    done() {
      clearTimeout(timer);
      callerSignal?.removeEventListener('abort', onAbort);
    },
  };
}

/** Meldung des Servers (JSON detail), oder '' wenn es keine gab (etwa 502/503 von Caddy beim Deploy). */
export function serverDetail(err) {
  return err && err.detail && err.detail !== `HTTP ${err.status}` ? err.detail : '';
}

/** Fehler von „Jetzt abrufen“: 503 mit Meldung des Servers („Der Sammler ist ausgeschaltet“) als
 *  solche; ohne Meldung (Caddy 502/503 beim Deploy) und sonst „Fehler: …“. */
export function refreshError(err) {
  const detail = serverDetail(err);
  return err.status === 503 && detail ? detail : `Fehler: ${err.detail || err.message}`;
}

/** Fehler aus fetch/Antwort lesen → was api() wirft: Abbruch durch den Aufrufer bleibt AbortError
 *  (der Aufrufer will nichts mehr), Frist abgelaufen oder Netzfehler → ApiError(0, 'Keine Verbindung'). */
export function fetchFailure(err, timedOut) {
  if (!timedOut && err && err.name === 'AbortError') return err;
  return new ApiError(0, 'Keine Verbindung', { timedOut: !!timedOut });
}

/**
 * @param {string} path    ohne /api/, z. B. 'articles' oder 'articles/12/read'
 * @param {object} opts    {method, body, params, signal, timeout}; timeout ohne Angabe: 15 s für
 *                         GET/HEAD, 45 s für schreibende Aufrufe
 */
export async function api(path, {
  method = 'GET', body, params, signal,
  timeout = deadlineFor(method),
} = {}) {
  const url = new URL(`/api/${path}`, location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === '') continue;
      url.searchParams.set(k, String(v));
    }
  }
  const deadline = deadlineSignal(signal, timeout);
  const init = {
    method, credentials: 'same-origin', signal: deadline.signal, headers: { Accept: 'application/json' },
  };
  if (method !== 'GET' && method !== 'HEAD') {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body ?? {});
  }
  let res;
  let data = null;
  try {
    try {
      res = await fetch(url, init);
    } catch (err) {
      throw fetchFailure(err, deadline.timedOut());
    }
    if (res.status === 204) return null;
    const type = res.headers.get('content-type') || '';
    if (type.includes('application/json')) {
      // Die Frist gilt auch für den Rumpf; bricht der Aufrufer oder die Frist ab, zählt das wie oben.
      try {
        data = await res.json();
      } catch (err) {
        if (deadline.signal.aborted) throw fetchFailure(err, deadline.timedOut());
        data = null;
      }
    }
  } finally {
    deadline.done();
  }
  if (!res.ok) {
    const err = new ApiError(res.status, detailText(data && data.detail));
    if (res.status === 401 && path !== 'login') onUnauthorized(err);
    throw err;
  }
  return data;
}
