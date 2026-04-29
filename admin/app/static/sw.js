// pawcorder admin service worker.
// Strategy:
//   - Pre-cache the icon + manifest at install
//   - Network-first for navigation requests (HTML) so the admin panel
//     stays responsive when online; when offline we serve a tiny fallback
//   - Cache-first for /static/* (Tailwind/Alpine come from CDN, not us)
//   - Never cache /api/*, /login, or any POST — those need to be live

const CACHE_VERSION = 'pawcorder-v1';
const PRECACHE = [
  '/static/icon.svg',
  '/static/manifest.json',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

const OFFLINE_HTML = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Offline — pawcorder</title><style>body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;font-family:system-ui,sans-serif;background:#f8fafc;color:#475569}main{text-align:center;padding:1.5rem}h1{font-size:1.25rem;margin:0 0 .5rem}p{margin:0;color:#94a3b8}</style></head><body><main><h1>pawcorder</h1><p>You're offline. Reconnect and refresh.</p></main></body></html>`;

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  // Never cache live data
  if (url.pathname.startsWith('/api/') || url.pathname === '/login') return;

  // Static: cache-first, fall through to network
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(req).then((cached) => cached || fetch(req).then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE_VERSION).then((c) => c.put(req, copy));
        return resp;
      }))
    );
    return;
  }

  // Navigation: network-first with offline fallback
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() => new Response(OFFLINE_HTML, {
        headers: { 'Content-Type': 'text/html; charset=utf-8' },
        status: 503,
        statusText: 'Offline',
      }))
    );
  }
});

// Web Push — show a notification when the server pushes a payload.
// Payload shape (JSON): { title, body, url }
self.addEventListener('push', (event) => {
  let payload = { title: 'pawcorder', body: '', url: '/' };
  try {
    if (event.data) {
      payload = Object.assign(payload, event.data.json());
    }
  } catch (_) {
    if (event.data) payload.body = event.data.text();
  }
  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      icon: '/static/icon.svg',
      badge: '/static/icon.svg',
      data: { url: payload.url || '/' },
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window' }).then((all) => {
      // Reuse an existing tab on the same origin if possible.
      for (const c of all) {
        if (c.url.includes(self.location.origin) && 'focus' in c) {
          c.navigate(url).catch(() => {});
          return c.focus();
        }
      }
      return clients.openWindow(url);
    })
  );
});
