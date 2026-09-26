// fetch-Hülle für /api/. 401 → Login-Screen (über setUnauthorizedHandler).
// Schreibende Aufrufe schicken immer Content-Type: application/json.

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.status = status;
    this.detail = detail || `HTTP ${status}`;
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

/**
 * @param {string} path    ohne /api/, z. B. 'articles' oder 'articles/12/read'
 * @param {object} opts    {method, body, params, signal}
 */
export async function api(path, { method = 'GET', body, params, signal } = {}) {
  const url = new URL(`/api/${path}`, location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === '') continue;
      url.searchParams.set(k, String(v));
    }
  }
  const init = { method, credentials: 'same-origin', signal, headers: { Accept: 'application/json' } };
  if (method !== 'GET' && method !== 'HEAD') {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body ?? {});
  }
  let res;
  try {
    res = await fetch(url, init);
  } catch (err) {
    if (err.name === 'AbortError') throw err;
    throw new ApiError(0, 'Keine Verbindung');
  }
  if (res.status === 204) return null;
  let data = null;
  const type = res.headers.get('content-type') || '';
  if (type.includes('application/json')) {
    try { data = await res.json(); } catch { data = null; }
  }
  if (!res.ok) {
    const err = new ApiError(res.status, detailText(data && data.detail));
    if (res.status === 401 && path !== 'login') onUnauthorized(err);
    throw err;
  }
  return data;
}
