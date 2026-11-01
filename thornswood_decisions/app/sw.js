// The Thornswood app's service worker. It keeps the app shell for offline
// opening and nothing else: never a card, an answer or any /api/ reply, and
// never an offline queue of decisions (07 section 5). Push comes in step 5.
const SHELL = 'tw-shell-v1'
const FILES = ['/m/', '/m/app.js', '/m/app.css', '/m/manifest.webmanifest', '/m/icon.svg',
  '/m/icon-192.png', '/m/icon-512.png']

self.addEventListener('install', event => {
  event.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).then(() => self.skipWaiting()))
})

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== SHELL).map(k => caches.delete(k))))
    .then(() => self.clients.claim()))
})

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url)
  if (url.origin !== self.location.origin || !url.pathname.startsWith('/m/') || event.request.method !== 'GET') {
    return // the API and everything else go straight to the network
  }
  if (event.request.mode === 'navigate') {
    // Network first; offline, the shell opens and says it is offline.
    event.respondWith(fetch(event.request).catch(() => caches.match('/m/')))
    return
  }
  event.respondWith(fetch(event.request).catch(() => caches.match(url.pathname)))
})
