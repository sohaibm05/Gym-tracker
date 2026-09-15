/* Service worker: make the app open in a gym basement.
 *
 * Scope
 * -----
 * Served from /sw.js, not /static/sw.js, and that matters: a worker can only
 * control pages at or below its own path, so one under /static/ could never
 * control /workout.
 *
 * Strategy
 * --------
 * Two different problems, two different strategies:
 *
 *   The shell (HTML, CSS, JS, icons) — cache first. It changes only when the
 *   app is redeployed, and serving it from cache is what makes the app open
 *   instantly and open at all on a dead connection.
 *
 *   The API — network only, never cached. A cached workout is a wrong workout:
 *   showing yesterday's sets as if they were today's, or a stale "no session in
 *   progress" while one is live, is worse than an honest failure. The app has
 *   its own outbox for writes made offline; reads simply fail and the UI keeps
 *   what it already had in memory.
 *
 * What this deliberately does NOT do
 * ----------------------------------
 * No Background Sync. It would be the textbook answer for flushing the outbox,
 * but support is Chromium-only and the app already retries on the `online`
 * event and on launch, which covers the same case everywhere.
 */

// Bump to invalidate the shell cache. The version is in the name rather than in
// the entries, so activating a new worker drops the whole old cache at once and
// cannot leave a half-updated mix of old and new assets.
const CACHE = 'gym-shell-v1';

// /workout is deliberately NOT pre-cached at install time.
//
// addAll() follows redirects like any other fetch, so installing while signed
// out — or with an expired session — would cache the login page under the
// /workout key before the app had ever run. It is cached on first successful
// load instead, by the fetch handler below, which checks what it actually got.
const SHELL = [
  '/static/app.css',
  '/static/app.js',
  '/static/icon.svg',
  '/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      // addAll is atomic: one 404 fails the install and the old worker stays
      // active, which is the right outcome — a half-cached shell is a broken app.
      .then((cache) => cache.addAll(SHELL))
      // Take over on next load rather than waiting for every tab to close.
      .then(() => self.skipWaiting())
      .catch(() => { /* offline at install: the next load tries again */ }),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names.filter((name) => name !== CACHE).map((name) => caches.delete(name)),
      ))
      .then(() => self.clients.claim()),
  );
});

/**
 * Whether a response may be written into the shell cache.
 *
 * `response.ok` alone is not enough for /workout. That route is behind the
 * session cookie, so once a session expires the server answers it with a 303 to
 * /login; fetch follows the redirect and hands back a perfectly valid 200 —
 * containing the login form. Caching that under the /workout key poisons the
 * installed app: it opens on a login page served from disk, and keeps doing so
 * after the person has signed back in, because the cache entry only changes on
 * a later successful revalidation.
 *
 * `response.redirected` is how a followed redirect is distinguished from a real
 * answer. A cross-origin `opaque` response has status 0 and is excluded too.
 */
function isCacheable(request, response) {
  if (!response || !response.ok || response.type === 'opaque') return false;
  if (response.redirected) return false;
  // Belt and braces for the same case: whatever the redirect flag says, a
  // response that did not come back from the URL we asked for is not that URL's
  // content.
  if (response.url && new URL(response.url).pathname !== new URL(request.url).pathname) {
    return false;
  }
  return true;
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Never cache the API, and never serve a stale answer for it.
  if (url.pathname.startsWith('/api/')) return;

  // The app shell, and the page itself.
  if (url.pathname === '/workout' || url.pathname.startsWith('/static/')
      || url.pathname === '/manifest.webmanifest') {
    event.respondWith(
      caches.match(request).then((hit) => {
        // Revalidate in the background so a redeploy is picked up on the next
        // launch without ever making this one wait for the network.
        const network = fetch(request)
          .then((response) => {
            if (isCacheable(request, response)) {
              const copy = response.clone();
              caches.open(CACHE).then((cache) => cache.put(request, copy));
            }
            return response;
          })
          .catch(() => hit);
        return hit || network;
      }),
    );
    return;
  }

  // Everything else (the server-rendered pages, /login, /progress) is left to
  // the browser. Caching them here would serve a signed-out person somebody
  // else's rendered HTML from disk.
});
