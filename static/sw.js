// Kiosk Service Worker: cached nur die App-Shell (HTML, CSS, JS, Schriften, Icons).
// API-Antworten werden nie gecacht. Version kommt als ?v=<KIOSK_BUILD> aus der Registrierung.

const VERSION = new URL(self.location.href).searchParams.get('v') || 'dev';
const CACHE = `kiosk-shell-${VERSION}`;

const SHELL = [
  '/',
  '/index.html',
  '/manifest.webmanifest',
  '/css/tokens.css',
  '/css/app.css',
  '/js/theme-boot.js',
  '/js/app.js',
  '/js/api.js',
  '/js/store.js',
  '/js/format.js',
  '/js/route.js',
  '/js/layout.js',
  '/js/saver.js',
  '/js/ui/dom.js',
  '/js/ui/icons.js',
  '/js/ui/row.js',
  '/js/ui/feed.js',
  '/js/ui/bar.js',
  '/js/ui/chips.js',
  '/js/ui/toggle.js',
  '/js/ui/sheet.js',
  '/js/ui/header.js',
  '/js/ui/empty.js',
  '/js/views/login.js',
  '/js/views/offline.js',
  '/js/views/list.js',
  '/js/views/article.js',
  '/js/views/menu.js',
  '/js/views/saved.js',
  '/js/views/settings.js',
  '/js/views/sources.js',
  '/js/views/profiles.js',
  '/js/views/profileEditor.js',
  '/js/views/search.js',
  '/js/views/sourceView.js',
  '/js/views/desktop.js',
  '/js/views/focus.js',
  '/js/views/related.js',
  '/js/views/home.js',
  '/js/views/overview.js',
  '/js/views/frontpage.js',
  '/fonts/doto-700-latin.woff2',
  '/fonts/space-grotesk-latin.woff2',
  '/fonts/space-grotesk-latin-ext.woff2',
  '/fonts/space-mono-400-latin.woff2',
  '/fonts/space-mono-400-latin-ext.woff2',
  '/icons/icon.svg',
  '/icons/icon-192.png',
  '/favicon.ico',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      // Erst nach vollständigem Cache übernehmen. Scheitert addAll, schlägt die Installation fehl
      // und der bisherige Worker (mit seinem vollständigen Cache) bleibt aktiv.
      .then(() => self.skipWaiting())
      .catch((err) => {
        console.warn('Kiosk SW: App-Shell nicht vollständig gecacht, Installation abgebrochen', err);
        throw err;
      }),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith('kiosk-shell-') && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

// Gecachte Kopie; Seitenaufrufe fallen auf /index.html zurück (Hash-Router).
function fromCache(req) {
  return caches.match(req, { ignoreSearch: true })
    .then((hit) => hit || (req.mode === 'navigate' ? caches.match('/index.html') : undefined));
}

// Netzwerk zuerst, Cache nur als Rückfall: offline und bei Serverfehlern (5xx, etwa 502/503 von
// Caddy während eines Deploys). Gibt es keine Kopie, geht die Fehlerseite durch. Gecacht werden nur
// erfolgreiche Antworten. /api/, /healthz, API-Doku und fremde Hosts gehen direkt durch.
self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  const p = url.pathname;
  if (p.startsWith('/api/') || p === '/healthz' || p === '/openapi.json' || p === '/docs' || p.startsWith('/docs/')) return;
  event.respondWith(
    fetch(req)
      .then((res) => {
        if (res.status >= 500) return fromCache(req).then((hit) => hit || res);
        if (res.ok && res.type === 'basic') {
          const copy = res.clone();
          caches.open(CACHE).then((cache) => cache.put(req, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() => fromCache(req).then((hit) => hit || Response.error())),
  );
});
