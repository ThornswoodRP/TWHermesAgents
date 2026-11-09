// The Thornswood app's service worker. It keeps the app shell for offline
// opening, never a card, an answer or any /api/ reply, and never an offline
// queue of decisions (07 section 5).
//
// Step 5: a push shows one quiet line, "Hermes needs you", with no body,
// no buttons and no sound, tagged by its card so a second push for the same
// card replaces the first. A tap opens that card in the app. Nothing here
// answers a card, and the payload carries no question, repo or answer: only
// the card's id, which is checked before it becomes a path.
//
// The worker shows the alerts, so it takes them off too: the app tells it which
// cards still need Patrick each time it reads Decisions, and which card was just
// opened or answered (Patrick, 1 Nov: an answered card kept showing).
//
// A chat alert (alerts.py, 9 Nov) is the same kind of line: "Rift answered" when a turn he started ended, or
// "Hermes asks you something" when his own Hermes opened a question or an approval. Its payload is who and
// the chat to open, checked before it becomes a path; never a word of the chat. None shows while that very
// chat is open in front of him, and opening the chat takes its alert off.
const SHELL = 'tw-shell-v12'
const ALERTS = 'tw-alerts'  // when each card's alert reached this phone (push, below)
const CARD_ID = /^[A-Za-z0-9_-]{1,64}$/
const CHAT_PATH = /^\/m\/(bots\/[a-z0-9][a-z0-9_-]{0,63}(\/s\/[A-Za-z0-9][A-Za-z0-9_.:-]{0,127})?|hermes\/(me|one)(\/[A-Za-z0-9][A-Za-z0-9_.:-]{0,127})?)$/
const APP_PATH = /^\/m\/(decisions|decisions\/[A-Za-z0-9_-]{1,64}|needs-you)$/
const WHO = /^[A-Za-z][A-Za-z ]{0,39}$/
const CHAT_WORDS = { answered: who => who + ' answered', asks: who => who + ' asks you something' }
const FILES = ['/m/', '/m/app.js', '/m/app.css', '/m/manifest.webmanifest', '/m/cinzel-latin.woff2',
  '/m/icon-192.png', '/m/icon-512.png', '/m/icon-maskable-512.png']

self.addEventListener('install', event => {
  event.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).then(() => self.skipWaiting()))
})

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== SHELL && k !== ALERTS).map(k => caches.delete(k))))
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

self.addEventListener('push', event => {
  let data = {}
  try { data = event.data ? event.data.json() : {} } catch (err) { data = {} }
  if (Object.prototype.hasOwnProperty.call(CHAT_WORDS, data.kind) && typeof data.path === 'string' && CHAT_PATH.test(data.path)) {
    event.waitUntil(chatAlert(data.kind, typeof data.who === 'string' && WHO.test(data.who) ? data.who : 'Hermes', data.path))
    return
  }
  const id = typeof data.decision_id === 'string' && CARD_ID.test(data.decision_id) ? data.decision_id : null
  // When this phone got the alert, kept on the phone only, so the card can say how long it took, with which
  // of the card's alerts it was: a revised card is alerted again, and a time from its first alert beside the
  // second's state read as the second reaching the phone before it was queued (Patrick, 2 Nov 4:01 PM).
  const rev = Number.isInteger(data.attention_revision) ? data.attention_revision : null
  if (id) event.waitUntil(caches.open(ALERTS).then(c => c.put(new Request('/m/alerts/' + id),
    new Response(JSON.stringify({ shown_at: new Date().toISOString(), attention_revision: rev }), { headers: { 'Content-Type': 'application/json' } }))))
  event.waitUntil(self.registration.showNotification('Hermes needs you', {
    tag: id ? 'decision:' + id : 'thornswood',
    renotify: false,
    silent: false,
    icon: '/m/icon-192.png',
    data: { path: id ? '/m/decisions/' + id : '/m/decisions' }
  }))
})

// A chat alert, unless that chat is open in front of him now.
async function chatAlert(kind, who, path) {
  const list = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
  if (list.some(c => c.visibilityState === 'visible' && c.focused && new URL(c.url).pathname === path)) return
  return self.registration.showNotification(CHAT_WORDS[kind](who), {
    tag: 'chat:' + path,
    renotify: true,
    silent: false,
    icon: '/m/icon-192.png',
    data: { path }
  })
}

self.addEventListener('notificationclick', event => {
  event.notification.close()
  const want = event.notification.data && event.notification.data.path
  const path = typeof want === 'string' && (APP_PATH.test(want) || CHAT_PATH.test(want)) ? want : '/m/decisions'
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    const open = list.find(c => new URL(c.url).origin === self.location.origin && new URL(c.url).pathname.startsWith('/m/'))
    if (open) return open.focus().then(c => (c || open).postMessage({ go: path }))
    return self.clients.openWindow(path)
  }))
})

// { pending: [ids] }: close every card alert not in the list, and the plain one.
// { close: id }: close that card's alert.
// { opened: path }: close that chat's alert.
self.addEventListener('message', event => {
  const m = event.data || {}
  let keep = null
  let only = null
  if (typeof m.opened === 'string' && CHAT_PATH.test(m.opened)) {
    event.waitUntil(self.registration.getNotifications({ tag: 'chat:' + m.opened }).then(list => list.forEach(n => n.close())))
    return
  }
  if (Array.isArray(m.pending)) keep = new Set(m.pending.filter(id => typeof id === 'string' && CARD_ID.test(id)))
  else if (typeof m.close === 'string' && CARD_ID.test(m.close)) only = m.close
  else return
  event.waitUntil(self.registration.getNotifications().then(list => {
    for (const n of list) {
      const card = /^decision:(.+)$/.exec(n.tag || '')
      if (only ? card && card[1] === only : (card ? !keep.has(card[1]) : n.tag === 'thornswood')) n.close()
    }
  }))
})
