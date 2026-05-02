// Pawcorder admin service worker.
// Strategy:
//   - Pre-cache icon set + manifest at install
//   - Network-first for navigation requests (HTML) so the admin panel
//     stays responsive when online; when offline we serve a small
//     bilingual fallback (zh-TW + en).
//   - Cache-first for /static/* (Tailwind/Alpine come from CDN, not us)
//   - Never cache /api/*, /login, or any POST — those need to be live

// Bump on every static-asset change so old clients pick up new files.
const CACHE_VERSION = 'pawcorder-v2';
const PRECACHE = [
  '/static/icon.svg',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/icon-maskable-512.png',
  '/static/apple-touch-icon-180.png',
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

// Bilingual offline fallback. 100dvh (dynamic viewport) so it sits flush
// even when Android Chrome's URL bar is showing — 100vh on Android leaves
// a scrollable gap because vh is locked to the largest viewport size.
const OFFLINE_HTML = `<!doctype html><html lang="zh-TW"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>離線 — Pawcorder</title><style>:root{color-scheme:light dark}body{margin:0;height:100dvh;display:flex;align-items:center;justify-content:center;font-family:system-ui,-apple-system,"Noto Sans CJK TC",sans-serif;background:#FBF8F3;color:#1A1410}@media (prefers-color-scheme:dark){body{background:#0f172a;color:#e2e8f0}}main{text-align:center;padding:1.5rem;max-width:24rem}h1{font-size:1.5rem;margin:0 0 .5rem;font-weight:600}p{margin:.25rem 0;color:#6F665C;line-height:1.5}@media (prefers-color-scheme:dark){p{color:#94a3b8}}</style></head><body><main><h1>Pawcorder</h1><p>目前離線中,請重新連線後重新整理頁面。</p><p style="margin-top:1rem;font-size:.85rem">You're offline. Reconnect and refresh.</p></main></body></html>`;

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
  let payload = { title: 'Pawcorder', body: '', url: '/' };
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
      icon: '/static/icon-192.png',
      badge: '/static/icon-192.png',
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
