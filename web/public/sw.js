/* Vocalis service worker — installability and a usable offline failure mode.
 *
 * The caching rules here are deliberately conservative, because the failure
 * this app can least afford is showing stale state. A book takes hours; a
 * cached "Chapter 5 of 39" or a cached "narrator offline" would be indefensible
 * — the user would be reading fiction about their own machine. So:
 *
 *   /api/*        never cached. Job progress, worker heartbeat, downloads and
 *                 voice previews are always live, offline or not.
 *   navigations   network first. A rebuilt UI lands on the next load rather
 *                 than whenever a cache happens to expire (the same reason
 *                 nginx sends no-cache for index.html).
 *   /assets/*     cache first. Vite fingerprints these, so a given URL's bytes
 *                 never change and a stale hit is impossible.
 *   everything    stale-while-revalidate — icons and the manifest, where a
 *   else          slightly old copy is harmless and a fast paint is worth it.
 *
 * Offline is a graceful failure, not a feature: every useful action needs the
 * API and the narrator, so the offline page says so plainly instead of
 * pretending the app works.
 */

const VERSION = "v1";
const SHELL = `vocalis-shell-${VERSION}`;
const ASSETS = `vocalis-assets-${VERSION}`;
const OFFLINE_URL = "/offline.html";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(SHELL)
      .then((cache) => cache.addAll([OFFLINE_URL, "/icon.svg"]))
      // Take over immediately. Safe here because assets are content-addressed,
      // so a new worker cannot hand an old page mismatched JavaScript.
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== SHELL && key !== ASSETS)
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

/** Let the page trigger an immediate update after it sees a new worker. */
self.addEventListener("message", (event) => {
  if (event.data === "skip-waiting") self.skipWaiting();
});

async function networkFirst(request) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(SHELL);
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    const cached = await caches.match(request);
    return cached || (await caches.match(OFFLINE_URL));
  }
}

async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) {
    const cache = await caches.open(ASSETS);
    cache.put(request, response.clone());
  }
  return response;
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(ASSETS);
  const cached = await cache.match(request);
  const network = fetch(request)
    .then((response) => {
      if (response.ok) cache.put(request, response.clone());
      return response;
    })
    .catch(() => cached);
  return cached || network;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Only same-origin GETs are ours to reason about. Range requests are how the
  // browser streams audio previews and M4B downloads; caching a partial
  // response would corrupt playback, so they pass straight through.
  if (
    request.method !== "GET" ||
    url.origin !== self.location.origin ||
    request.headers.has("range")
  ) {
    return;
  }

  if (url.pathname.startsWith("/api/")) return; // always live

  if (request.mode === "navigate") {
    event.respondWith(networkFirst(request));
    return;
  }

  if (url.pathname.startsWith("/assets/")) {
    event.respondWith(cacheFirst(request));
    return;
  }

  event.respondWith(staleWhileRevalidate(request));
});
