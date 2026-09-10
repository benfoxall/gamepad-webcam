// Service worker for the gamepad camera page.
//
// The page is a single self-contained HTML file, so "offline" only needs the
// shell cached. Strategy is stale-while-revalidate: serve the cached copy
// straight away, then refresh it in the background, so a reload after the page
// changes on GitHub Pages picks the new version up without bumping VERSION.
// VERSION only has to change when this file itself needs to invalidate old
// caches (the browser installs a new worker when sw.js bytes change).

const VERSION = 'v1';
const CACHE = `gamepad-camera-${VERSION}`;

// Relative so it works from a project page (/gamepad-webcam/) and localhost.
const SHELL = ['./', './index.html', './manifest.webmanifest', './icon.svg'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k.startsWith('gamepad-camera-') && k !== CACHE).map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

// The page calls this when the user accepts an update.
self.addEventListener('message', (e) => {
  if (e.data && e.data.type === 'skip-waiting') self.skipWaiting();
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const cached = await cache.match(req, { ignoreSearch: true });

    const network = fetch(req).then((res) => {
      if (res && res.ok && res.type === 'basic') cache.put(req, res.clone());
      return res;
    }).catch(() => null);

    if (cached) {
      e.waitUntil(network);
      return cached;
    }

    const res = await network;
    if (res) return res;

    // Offline and never cached: any navigation still gets the app shell.
    if (req.mode === 'navigate') {
      const shell = await cache.match('./index.html');
      if (shell) return shell;
    }
    return new Response('Offline and not cached.', { status: 503, statusText: 'Offline' });
  })());
});
