// The Hermes phone app (07 sections 4, 5 and 8; step 3 read only, step 4
// answers and chat, step 5 alerts). Its tabs: Bots, Rooms, Board and Decisions
// (first named Room, Work and Needs you, and Bramble had a tab; those paths
// still open). The Bots tab is one list, Thornswood Bots (Patrick's mock-up
// answer, 1 Nov): his own Hermes on the VM and his Hermes on One, named Hermes
// and One, then the six Bots. The grouping is presentation only: each keeps
// its own gateway, chats, permissions and sign-in rules.
//
// Plain script, no framework, no inline code (the page's CSP allows only this
// file). Everything shown from the core is set with textContent, never as
// markup. The session is an HttpOnly cookie the script never sees; the CSRF
// token lives only in memory and is asked for again after a reload.
//
// An answer and a chat message each carry an idempotency key, kept in
// sessionStorage only so that a retry after a lost reply is the same request.
// The key is never authority: the core decides, and a failed save reads the
// card back ("checking outcome") before it offers to try again.
//
// The phone keeps a copy of the chats it has shown in this browser's storage
// (keepThread), so a chat opens at once; signing out deletes it.

'use strict'

const API = '/api/v1'
const POLL_MS = 15000
let csrf = null
let seq = 0
let pollTimer = null
let answering = false
let flash = ''        // one line the Decisions list shows once, after an answer
let alertsNote = ''   // what the last alerts check did, shown in Settings
let alertsChecked = 0
let installPrompt = null

// ── small helpers ─────────────────────────────────────────────────────────

function el(tag, attrs, ...children) {
  const node = document.createElement(tag)
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') node.className = v
    else if (k === 'onclick') node.addEventListener('click', v)
    else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v === true ? '' : v)
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue
    node.append(c instanceof Node ? c : document.createTextNode(String(c)))
  }
  return node
}

function show(...nodes) {
  showIn('view', nodes)
}

// The desktop's panes (the chat list on the left, Decisions on the right) are
// drawn by the same views as the phone's screens, each into its own pane. Only
// the middle one moves the page; a pane keeps its own place in its list.
function showIn(into, nodes) {
  const pane = document.getElementById(into)
  const keep = into === 'view' ? null : pane.scrollTop
  pane.replaceChildren(...[nodes].flat(Infinity).filter(Boolean))
  if (keep !== null) pane.scrollTop = keep
  else if (pageTop) { pageTop = false; scrollTo({ top: 0 }) }
}

function when(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return isNaN(d) ? iso : d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
}

// ── the look (Patrick's approved mock-up v2, 1 Nov) ───────────────────────
//
// One visual system for every screen, as the mock-up drew it: each agent has
// its own colour and initial (app.css .a-<id>), the same in the lists, on a
// chat's bar and on its messages; lists are full-width rows with the avatar on
// the left and the state on the right; every screen opens with a bar; the tabs
// and the bars use the mock-up's line icons.
const AGENT_INITIAL = { me: 'H', one: 'O', bramble: 'B', rift: 'R', sprocket: 'S', echo: 'E', magpie: 'M', wisp: 'W' }
const AGENT_BY_NAME = { Hermes: 'me', One: 'one', Bramble: 'bramble', Rift: 'rift', Sprocket: 'sprocket', Echo: 'echo',
  Magpie: 'magpie', Wisp: 'wisp' }

function avatar(id, label) {
  const known = AGENT_INITIAL[id]
  return el('div', { class: 'av a-' + (known ? id : 'room'), 'aria-hidden': 'true' },
    known || (label || '#').trim().charAt(0).toUpperCase() || '#')
}

// The mock-up's line icons (24 by 24, drawn with the text colour).
const ICON = {
  back: 'M15 18l-6-6 6-6', send: 'M4 12l16-8-6 16-3-7z', x: 'M6 6l12 12M18 6L6 18', plus: 'M12 5v14M5 12h14',
  search: 'M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM20 20l-4-4',
  more: 'M12 5h.01M12 12h.01M12 19h.01', down: 'M12 5v14M6 13l6 6 6-6',
  panel: 'M3 4h18v16H3zM9 4v16', file: 'M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9zM14 3v6h6',
  image: 'M4 5h16v14H4zM4 16l5-5 4 4 3-3 4 4M15 9h.01', camera: 'M4 8h4l2-3h4l2 3h4v11H4zM12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8z', dec: 'M9 11l3 3 8-8M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9'
}
function icon(name, size = 20) {
  const ns = 'http://www.w3.org/2000/svg'
  const svg = document.createElementNS(ns, 'svg')
  for (const [k, v] of Object.entries({ width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
    'stroke-width': name === 'more' ? 3 : 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' })) svg.setAttribute(k, v)
  const p = document.createElementNS(ns, 'path')
  p.setAttribute('d', ICON[name])
  svg.append(p)
  return svg
}

// A screen's bar: back (when there is somewhere to go back to), the agent's
// avatar, the title (a heading on a list screen) and a line under it, and
// whatever goes on the right.
function screenBar({ back, backLabel, agent, label, title, heading, under, right, live }) {
  return el('div', { class: 'bar' },
    back ? el('button', { class: 'ib', type: 'button', 'aria-label': backLabel, onclick: back }, icon('back')) : null,
    agent !== undefined ? (live ? liveAvatar(agent, label || title, live) : avatar(agent, label || title)) : null,
    el('div', { class: 't' }, el(heading ? 'h2' : 'strong', {}, title), under ? el('span', {}, under) : null),
    right || null)
}

// A row of a list: avatar, title with its tags, the lines under it, and what
// goes on the right (a state or a time).
function listRow(attrs, { agent, label, title, tags, lines, right, live }) {
  return el('button', { type: 'button', ...attrs, class: 'row' + (attrs.class ? ' ' + attrs.class : '') },
    agent !== undefined ? (live ? liveAvatar(agent, label || title, live) : avatar(agent, label || title)) : null,
    el('div', { class: 't' }, el('strong', {}, title), tags || null, lines || null),
    right ? el('div', { class: 'w' }, right) : null)
}

const SAID = {
  mobile_read_off: 'The phone app is off for now. The report page is still where you answer.',
  signed_out: 'You are signed out.',
  session_expired: 'Your sign-in ran out. Sign in again.',
  too_many_tries: 'Too many tries. Wait 15 minutes, then try again.',
  wrong_code: 'That code did not work.',
  sign_in_again: 'This needs a fresh sign-in with your passkey.',
  decision_write_off: 'Answering is off for now. The report page is still where you answer.',
  chat_write_off: 'Chat from the phone is off for now.',
  own_chat_off: 'Chat with your own Hermes from the phone is off for now.',
  session_manage_off: 'Renaming, hiding and deleting chats from the phone is off for now.',
  bot_chat_kept: 'Your Bot Chat is kept as it is: Bot Mode and every Bot\'s message to you find it by its name, so it is not renamed, hidden or deleted.',
  passkey_needed: 'A message to your own Hermes runs with full power, so it needs a passkey sign-in from the last 30 minutes.',
  one_off: 'One is off',
  no_gateway: 'There is no such gateway.',
  stale_revision: 'This card changed since you opened it. Read it again before you answer.',
  already_answered: 'This card was already answered.',
  expired: 'This card ran out. Bramble opens a new one if it still matters.',
  no_chat: 'There is no such chat.',
  setup_expired: 'Setting up this phone ran out: a pairing gives 5 minutes to add the first passkey. Get a new code, pair again and add the passkey straight after.',
  passkey_unavailable: 'Passkeys are not set up on the core yet.',
  no_origin: 'The core does not know its address yet, so passkeys cannot work.',
  offline: 'Offline. Nothing was sent. Turn Tailscale on and try again.'
}

function said(body) {
  return (body && (SAID[body.error] || body.message)) || 'Something went wrong.'
}

async function call(method, path, body) {
  const headers = { Accept: 'application/json' }
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json'
    if (csrf) headers['X-CSRF-Token'] = csrf
  }
  let resp
  try {
    resp = await fetch(API + path, {
      method, headers, credentials: 'same-origin', cache: 'no-store',
      body: method === 'GET' ? undefined : JSON.stringify(body || {})
    })
  } catch (err) {
    return { status: 0, body: { error: 'offline' } }
  }
  let out = {}
  try { out = await resp.json() } catch (err) { out = {} }
  return { status: resp.status, body: out }
}

// ── passkeys: JSON options to the browser's shapes and back ───────────────

function fromB64url(s) {
  const pad = '='.repeat((4 - (s.length % 4)) % 4)
  const bin = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'))
  return Uint8Array.from(bin, c => c.charCodeAt(0)).buffer
}

function toB64url(buf) {
  let bin = ''
  for (const b of new Uint8Array(buf)) bin += String.fromCharCode(b)
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function creationOptions(o) {
  if (window.PublicKeyCredential && PublicKeyCredential.parseCreationOptionsFromJSON) {
    return PublicKeyCredential.parseCreationOptionsFromJSON(o)
  }
  return { ...o, challenge: fromB64url(o.challenge), user: { ...o.user, id: fromB64url(o.user.id) },
    excludeCredentials: (o.excludeCredentials || []).map(c => ({ ...c, id: fromB64url(c.id) })) }
}

function requestOptions(o) {
  if (window.PublicKeyCredential && PublicKeyCredential.parseRequestOptionsFromJSON) {
    return PublicKeyCredential.parseRequestOptionsFromJSON(o)
  }
  return { ...o, challenge: fromB64url(o.challenge),
    allowCredentials: (o.allowCredentials || []).map(c => ({ ...c, id: fromB64url(c.id) })) }
}

function credentialJSON(cred) {
  if (typeof cred.toJSON === 'function') return cred.toJSON()
  const r = cred.response
  const response = { clientDataJSON: toB64url(r.clientDataJSON) }
  if (r.attestationObject) {
    response.attestationObject = toB64url(r.attestationObject)
    if (r.getTransports) response.transports = r.getTransports()
  } else {
    response.authenticatorData = toB64url(r.authenticatorData)
    response.signature = toB64url(r.signature)
    if (r.userHandle) response.userHandle = toB64url(r.userHandle)
  }
  return { id: cred.id, rawId: toB64url(cred.rawId), type: cred.type, response,
    clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {} }
}

// ── signing in ────────────────────────────────────────────────────────────

// The passkey prompt opens by itself, the way a bank app asks for a face or a
// fingerprint, and its button shows only when that prompt was dismissed, failed
// or was blocked (Patrick, 1 Nov). Once when the app opens: a dismissal is
// never asked again by itself, and signing out never opens it.
let askOnOpen = true

// Opens the passkey prompt now and runs then(r) on a sign-in. If the prompt was
// dismissed or failed, the button (made hidden) shows, with what happened.
async function promptNow(button, status, then) {
  button.hidden = true
  const r = await passkeySignIn()
  if (r.status === 200) return then(r)
  status.textContent = said(r.body)
  button.hidden = false
}

// ── the fingerprint on the phone (Patrick, 2 Nov) ─────────────────────────
//
// A sign-in that asks for any passkey lets Android hand it to the passkey
// provider he chose (Bitwarden), which leaves the app, unlocks and comes back.
// This phone's own key, made by its platform authenticator and named in the
// sign-in request, is answered by the phone itself: its fingerprint sheet over
// the app. The core checks it as it checks any passkey. A Desktop has no such
// key and keeps the passkey.
const PHONE_KEY = 'hermes.phoneKey.v1'

function phoneKey() {
  try {
    const k = JSON.parse(localStorage.getItem(PHONE_KEY) || 'null')
    return k && typeof k.credential_id === 'string' && typeof k.passkey_id === 'string' ? k : null
  } catch (e) { return null }
}

function keepPhoneKey(k) {
  try { localStorage.setItem(PHONE_KEY, JSON.stringify(k)); return true } catch (e) { return false }
}

function forgetPhoneKey() {
  try { localStorage.removeItem(PHONE_KEY) } catch (e) {}
}

// The word for what signs in on this device.
function signInWord() {
  return phoneKey() ? 'fingerprint' : 'passkey'
}

// Whether this device can hold its own key: a paired phone whose platform
// authenticator checks the person (fingerprint or screen lock).
// Asked once a page load: neither changes while the app is open.
let fingerprintable = null

async function canUseFingerprint() {
  if (fingerprintable !== null) return fingerprintable
  try {
    if (!window.PublicKeyCredential || !PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable ||
        !(await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable())) return (fingerprintable = false)
  } catch (e) { return (fingerprintable = false) }
  const me = await call('GET', '/me')
  if (me.status !== 200) return false
  return (fingerprintable = me.body.device_kind === 'phone')
}

// Adds this phone's own key. The core adds a key only in a fresh session, so
// when this one is older than 5 minutes the passkey signs in first.
async function addFingerprint() {
  const me = await call('GET', '/me')
  if (me.status !== 200) return { ok: false, message: said(me.body) }
  if (!me.body.fresh) {
    const s = await passkeySignIn()
    if (s.status !== 200) return { ok: false, message: said(s.body) }
  }
  const o = await call('POST', '/passkeys/register/options', { on_phone: true })
  if (o.status !== 200) return { ok: false, message: said(o.body) }
  let cred
  try {
    cred = await navigator.credentials.create({ publicKey: creationOptions(o.body) })
  } catch (err) {
    return { ok: false, message: err && err.name === 'InvalidStateError'
      ? 'This phone already holds a key for Hermes. Nothing changed.'
      : 'The fingerprint was not added. Nothing changed.' }
  }
  const done = await call('POST', '/passkeys/register', { credential: credentialJSON(cred), label: 'Fingerprint on this phone' })
  if (done.status !== 200) return { ok: false, message: said(done.body) }
  if (!keepPhoneKey({ credential_id: done.body.credential_id, passkey_id: done.body.passkey_id })) {
    return { ok: false, message: 'This browser would not keep the fingerprint\'s name, so the app cannot ask for it. Remove it in Settings.' }
  }
  return { ok: true }
}

async function passkeySignIn() {
  const key = phoneKey()
  let o = await call('POST', '/passkeys/sign-in/options', key ? { phone_key: key.credential_id } : {})
  if (key && o.status === 404 && o.body.error === 'no_phone_key') {
    // The core no longer has this phone's key (it or the phone was removed): the passkey is the way in.
    forgetPhoneKey()
    o = await call('POST', '/passkeys/sign-in/options', {})
  }
  if (o.status !== 200) return o
  const word = o.body.allowCredentials && o.body.allowCredentials.length ? 'fingerprint' : 'passkey'
  let cred
  try {
    cred = await navigator.credentials.get({ publicKey: requestOptions(o.body) })
  } catch (err) {
    return { status: -1, body: { message: `The ${word} did not sign in. Nothing changed.` } }
  }
  const r = await call('POST', '/passkeys/sign-in', { credential: credentialJSON(cred) })
  if (r.status === 200) csrf = r.body.csrf
  return r
}

function newKey() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID()
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('')
}

function keyFor(name) {
  let k = sessionStorage.getItem(name)
  if (!k) { k = newKey(); sessionStorage.setItem(name, k) }
  return k
}

// What gets a pairing code, for Patrick's Hermes to run on One.
const PAIR_COMMAND = 'ssh thornswood-bots "sudo -u tw-decide /opt/thornswood-hermes/bin/tw-decide ' +
  '--db /srv/tw/decide/core.db pair --workspace thornswood --kind phone --label \'Pixel 9a\'"'

// Registers a passkey on this phone. The core allows it only in a fresh
// session: within 5 minutes of a pairing, or 5 minutes of a passkey sign-in.
async function addPasskey(label) {
  const o = await call('POST', '/passkeys/register/options')
  if (o.status !== 200) return { ok: false, message: said(o.body) }
  let cred
  try {
    cred = await navigator.credentials.create({ publicKey: creationOptions(o.body) })
  } catch (err) {
    // InvalidStateError: this phone already holds one of the passkeys the core lists.
    return { ok: false, message: err && err.name === 'InvalidStateError'
      ? 'This phone already has a passkey for Hermes. Nothing changed.'
      : 'No passkey was added. Nothing changed.' }
  }
  const done = await call('POST', '/passkeys/register', { credential: credentialJSON(cred), label })
  return done.status === 200 ? { ok: true } : { ok: false, message: said(done.body) }
}

// Paired, but the passkey was not added: the way on, said plainly.
function pairedView(message) {
  const status = el('p', { class: 'warn', role: 'status' }, message)
  show(el('div', { class: 'card' },
    el('h2', {}, 'This phone is paired'),
    status,
    el('p', { class: 'quiet' }, 'Add the passkey within 5 minutes of pairing. After that, pair again with a new code.'),
    el('button', { class: 'primary', type: 'button', onclick: async () => {
      status.textContent = ''
      const k = await addPasskey('This phone')
      if (k.ok) return route()
      status.textContent = k.message
    } }, 'Add a passkey'),
    el('button', { class: 'secondary', type: 'button', onclick: () => route() }, 'Go on without a passkey')))
}

// The three steps on one screen, so the passkey is added inside the 5 minutes.
function setupPart(status) {
  const code = el('input', { autocomplete: 'off', inputmode: 'text', placeholder: 'XXXX-XXXX', 'aria-label': 'Code' })
  return [
    el('ol', { class: 'steps' },
      el('li', {}, 'Get a code. Your Hermes can run this:', el('pre', { class: 'artifact' }, PAIR_COMMAND)),
      el('li', {}, 'Type it here within 10 minutes.', code),
      el('li', {}, 'Add your passkey. The prompt opens by itself once the code works.')),
    el('button', { class: 'primary', type: 'button', onclick: async () => {
      status.textContent = ''
      const r = await call('POST', '/pair', { code: code.value.trim() })
      if (r.status !== 200) { status.textContent = said(r.body); return }
      csrf = r.body.csrf
      const k = await addPasskey('This phone')
      if (k.ok) return route()
      pairedView(k.message)
    } }, 'Pair and add a passkey')
  ]
}

function recoveryPart(done) {
  const recovery = el('input', { autocomplete: 'off', placeholder: 'Recovery code', 'aria-label': 'Recovery code' })
  const label = el('input', { autocomplete: 'off', placeholder: 'This phone', 'aria-label': 'Name for this phone' })
  return [
    el('p', { class: 'quiet' }, 'A recovery code signs in once and makes this phone a new device. Add a passkey right after.'),
    recovery, label,
    el('button', { class: 'secondary', type: 'button', onclick: async () => {
      done(await call('POST', '/recovery-sign-in', { code: recovery.value.trim(), label: label.value.trim() }))
    } }, 'Sign in with a recovery code')
  ]
}

// The core says only whether a passkey exists. With one: one button, the other
// ways folded away. Without one: setting up this phone.
async function signInView(note) {
  document.getElementById('tabs').hidden = true
  document.getElementById('settings').hidden = true
  const st = await call('GET', '/sign-in/state')
  const hasKey = st.status === 200 ? st.body.passkeys === true : null
  const status = el('p', { class: 'warn', role: 'status' }, note || (st.status === 200 ? '' : said(st.body)))
  async function done(r) {
    if (r.status !== 200) { status.textContent = said(r.body); return }
    csrf = r.body.csrf
    route()
  }
  if (hasKey === false) forgetPhoneKey()
  if (hasKey === false) {
    return show(
      el('h2', {}, 'Set up this phone'),
      el('p', { class: 'quiet' }, 'No passkey is set up yet, or setting one up ran out. A pairing gives 5 minutes ' +
        'to add the first passkey, so the app asks for it straight after.'),
      setupPart(status), status,
      el('details', { class: 'fold' }, el('summary', {}, 'Lost your passkey: a recovery code'),
        el('div', { class: 'card' }, recoveryPart(done))))
  }
  const button = el('button', { class: 'primary', type: 'button', onclick: async () => {
    status.textContent = ''
    done(await passkeySignIn())
  } }, 'Sign in with your ' + signInWord())
  const auto = hasKey === true && askOnOpen
  askOnOpen = false
  show(
    el('h2', {}, 'Welcome back'),
    el('p', { class: 'quiet' }, hasKey ? `Your ${signInWord()} is set up.` : 'Sign in with your passkey, or set up this phone below.'),
    button,
    status,
    el('details', { class: 'fold', open: hasKey === null },
      el('summary', {}, 'Other ways in: a pairing code, or a recovery code'),
      el('div', { class: 'card' }, el('h2', {}, 'Pair this phone'), setupPart(status)),
      el('div', { class: 'card' }, el('h2', {}, 'Lost your passkey'), recoveryPart(done))))
  if (auto) await promptNow(button, status, done)
}

// ── the views ─────────────────────────────────────────────────────────────

function setTab(name) {
  for (const a of document.querySelectorAll('#tabs a[data-tab]')) {
    if (a.dataset.tab === name) a.setAttribute('aria-current', 'page')
    else a.removeAttribute('aria-current')
  }
}

function refused(r, into = 'view') {
  if (r.status === 401) { csrf = null; signInView(said(r.body)); return true }
  if (r.status !== 200) { showIn(into, el('p', { class: 'warn' }, said(r.body))); return true }
  return false
}

// On a phone with no fingerprint key yet, the Decisions list offers it once,
// until it is added or put off; Settings always has it.
const FINGERPRINT_LATER = 'hermes.fingerprintLater.v1'

async function fingerprintOffer() {
  let later = false
  try { later = localStorage.getItem(FINGERPRINT_LATER) === '1' } catch (e) {}
  if (later || phoneKey() || !(await canUseFingerprint())) return null
  const note = el('p', { class: 'warn', role: 'status' })
  const part = el('div', { class: 'card' },
    el('p', {}, el('strong', {}, 'Sign in with your fingerprint')),
    el('p', { class: 'quiet' }, 'The fingerprint opens right here, without going out to your passkey app. ' +
      'When Android asks where to save it, pick Google Password Manager. Your passkey stays for a Desktop.'),
    el('button', { class: 'primary', type: 'button', onclick: async () => {
      note.textContent = ''
      const k = await addFingerprint()
      if (k.ok) { flash = 'Fingerprint sign-in is on for this phone.'; return route() }
      note.textContent = k.message
    } }, 'Use my fingerprint'),
    el('button', { class: 'secondary', type: 'button', onclick: () => {
      try { localStorage.setItem(FINGERPRINT_LATER, '1') } catch (e) {}
      part.remove()
    } }, 'Keep the passkey'),
    note)
  return part
}

async function decisionsView(into = 'view') {
  const main = into === 'view'
  if (main) setTab('decisions')
  const r = await call('GET', '/cards')
  if (refused(r, into)) return
  seq = r.body.seq
  badge(r.body.cards.length)
  // Every alert of a card that no longer needs Patrick goes off the phone.
  toWorker({ pending: r.body.cards.map(c => c.id) })
  const note = main ? flash : ''
  if (main) flash = ''
  showIn(into, [
    screenBar({ title: 'Decisions', heading: true,
      right: main ? null : el('button', { class: 'ib', type: 'button', 'aria-label': 'Hide Decisions', title: 'Hide Decisions',
        onclick: () => fold('side') }, icon('x', 18)) }),
    note ? el('p', { class: 'receipt', role: 'status' }, note) : null,
    main ? await fingerprintOffer() : null,
    r.body.cards.length
      ? [el('h2', { class: 'section' }, `Waiting on you (${r.body.cards.length})`),
        r.body.cards.map(c => listRow({ onclick: () => go('/m/decisions/' + c.id) }, {
          title: inlineMd(c.title, true), tags: c.risk === 'high' ? el('span', { class: 'risk' }, 'high risk') : null,
          lines: el('span', { class: 'quiet' }, `${c.kind}, answer by ${when(c.expires_at)}`) }))]
      : el('p', { class: 'quiet' }, 'Nothing needs you right now.')])
}

async function cardView(id, note) {
  setTab('decisions')
  const r = await call('GET', '/cards/' + encodeURIComponent(id))
  if (refused(r)) return
  const c = r.body
  // An opened card's alert has done its work.
  toWorker({ close: c.id })
  const a = await call('GET', '/cards/' + encodeURIComponent(id) + '/artifact')
  const status = el('p', { class: 'warn', role: 'status' }, note || '')
  const pending = c.status === 'pending'
  const parts = [
    el('h2', {}, inlineMd(c.title)),
    c.risk === 'high' ? el('span', { class: 'risk' }, 'high risk') : null,
    a.status === 200 ? el('div', { class: 'artifact' }, markdown(a.body.body)) : el('p', { class: 'warn' }, said(a.body)),
    el('p', {}, el('strong', {}, 'What yes covers: '), inlineMd(c.scope_of_yes)),
    boxLine(c),
    c.options.map(o => el('div', { class: 'option' },
      el('strong', {}, inlineMd(o.label)), o.key === c.recommendation ? el('span', { class: 'rec' }, 'recommended') : null,
      el('div', { class: 'quiet' }, inlineMd(o.consequence)),
      pending ? el('button', { class: o.key === c.recommendation ? 'primary' : 'secondary', type: 'button',
        'data-option': o.key, onclick: () => choose(c, o, status) }, 'Choose: ' + o.label) : null))
  ]
  if (c.receipt) {
    const picked = c.options.find(o => o.key === c.receipt.option_key)
    parts.push(el('p', { class: 'receipt' }, `Answered "${picked ? picked.label : c.receipt.option_key}" at ` +
      `${when(c.receipt.answered_at)} (revision ${c.receipt.revision}).`))
  }
  if (c.superseded_by) {
    parts.push(el('button', { class: 'secondary', type: 'button', onclick: () => go('/m/decisions/' + c.superseded_by) },
      'This card was replaced. Open the new one'))
  }
  parts.push(status)
  parts.push(el('button', { class: 'secondary', type: 'button', onclick: () => go('/m/decisions') }, 'Back'))
  show(el('div', { class: 'card' }, parts))
}

// A box card (a pull request that changes what runs on the live box): the
// line the core builds from the card's own binding, the pull request and its
// exact head, so what a yes lets Bramble merge never rests on a Bot's words.
function boxLine(c) {
  if (!c.box) return null
  return el('p', { class: 'warn', 'data-box': c.box.repo + '#' + c.box.pr },
    el('strong', {}, 'Box change: '), c.box.says)
}

// One option chosen: a high-risk card shows what is being approved on its own
// screen, and the passkey prompt opens over it by itself (Patrick, 1 Nov). The
// passkey is the yes: it signs in afresh, then the answer goes. If the prompt is
// dismissed or fails, its button shows; nothing is answered until it succeeds.
async function choose(c, o, status) {
  if (c.risk !== 'high') return answer(c, o, status)
  const note = el('p', { class: 'warn', role: 'status' })
  const button = el('button', { class: 'primary', type: 'button', 'data-confirm': o.key, onclick: () =>
    promptNow(button, note, () => answer(c, o, status)) }, 'Answer ' + o.label + ' with your ' + signInWord())
  show(el('div', { class: 'card' },
    el('h2', {}, 'Check this answer'),
    el('p', {}, `"${c.title}" is high risk. You are choosing:`),
    el('p', {}, el('strong', {}, inlineMd(o.label))),
    el('p', { class: 'quiet' }, inlineMd(o.consequence)),
    el('p', {}, el('strong', {}, 'What yes covers: '), inlineMd(c.scope_of_yes)),
    boxLine(c),
    el('p', { class: 'quiet' }, `Your ${signInWord()} answers it. Cancel the prompt to change your mind.`),
    button, note,
    el('button', { class: 'secondary', type: 'button', onclick: () => cardView(c.id) }, 'Go back to the card')))
  await promptNow(button, note, () => answer(c, o, status))
}

async function answer(c, o, status) {
  const name = `tw-answer:${c.id}:${c.revision}:${c.attention_revision}:${o.key}`
  const body = { option_key: o.key, revision: c.revision, attention_revision: c.attention_revision,
    artifact_sha256: c.artifact_sha256, idempotency_key: keyFor(name) }
  status.textContent = 'Saving...'
  answering = true
  const r = await call('POST', '/cards/' + encodeURIComponent(c.id) + '/answer', body)
  answering = false
  if (r.status === 200) {
    sessionStorage.removeItem(name)
    return answered(c, o)
  }
  if (r.status === 401) { csrf = null; return signInView(said(r.body)) }
  if (r.status === 403 && r.body.error === 'sign_in_again') {
    const note = el('p', { class: 'warn', role: 'status' })
    const again = el('button', { class: 'primary', type: 'button', 'data-confirm': o.key, onclick: () =>
      promptNow(again, note, () => answer(c, o, status)) }, 'Sign in again and answer')
    show(el('div', { class: 'card' },
      el('p', {}, said(r.body)),
      el('p', {}, el('strong', {}, o.label), ': ', o.consequence),
      again, note,
      el('button', { class: 'secondary', type: 'button', onclick: () => cardView(c.id) }, 'Go back to the card')))
    return promptNow(again, note, () => answer(c, o, status))
  }
  if (r.status === 0 || r.status >= 500) {
    // The save may or may not have happened. Read the card back before offering anything.
    status.textContent = 'Checking outcome...'
    const back = await call('GET', '/cards/' + encodeURIComponent(c.id))
    if (back.status === 200 && back.body.receipt) { sessionStorage.removeItem(name); return answered(c, o) }
    return cardView(c.id, back.status === 200
      ? 'Not saved yet. Choosing again sends the same answer, never a second one.'
      : 'Could not reach the core to check. Nothing is shown as answered until it says so.')
  }
  if (r.status === 409 || r.status === 410) sessionStorage.removeItem(name)
  return cardView(c.id, said(r.body))
}

// An answered card leaves Decisions at once and for good (Patrick, 2 Nov: "I only want things waiting for
// me there"): its alert closes, and one line says what was chosen. The tab lists only cards waiting on him.
function answered(c, o) {
  toWorker({ close: c.id })
  flash = `Answered "${c.title}": ${o.label}.`
  history.replaceState(null, '', '/m/decisions')
  return route()
}

// The Board: what each Bot is working on, read from GitHub (the issues it has
// claimed and the pull requests from its branches) by the feed once in each 5
// minutes, grouped by Bot in the Bots list's order. Hermes's Kanban tasks show
// under them only when there are any (the Bots keep none now). Each row opens
// the pull request while one is open, otherwise the issue, always on github.com.
const STAGE = {
  claimed: () => 'Claimed, no pull request yet',
  pr_open: i => `Pull request #${i.pr} open`,
  pr_draft: i => `Draft pull request #${i.pr}`,
  merged: i => `Pull request #${i.pr} merged`,
  closed: i => `Pull request #${i.pr} closed without merging`
}
const CHECKS = { passed: 'checks passed', failing: 'checks failing', running: 'checks running', none: 'no checks yet' }
const REPO_SHORT = { 'ThornswoodRP/Thornswood': 'Thornswood', 'ThornswoodRP/Thornswood-Ops': 'Ops', 'ThornswoodRP/skymp': 'skymp' }
const GH_REPO = /^[A-Za-z0-9-]+\/[A-Za-z0-9._-]+$/

function workLink(i) {
  const open = (i.stage === 'pr_open' || i.stage === 'pr_draft') && i.pr && GH_REPO.test(i.pr_repo)
  if (open) return `https://github.com/${i.pr_repo}/pull/${i.pr}`
  if (i.number && GH_REPO.test(i.repo)) return `https://github.com/${i.repo}/issues/${i.number}`
  if (i.pr && GH_REPO.test(i.pr_repo)) return `https://github.com/${i.pr_repo}/pull/${i.pr}`
  return null
}

function workRow(i) {
  const progress = [STAGE[i.stage] ? STAGE[i.stage](i) : i.stage]
  if (i.checks && (i.stage === 'pr_open' || i.stage === 'pr_draft')) {
    progress.push(i.checks_line ? `checks: ${i.checks_line}` : CHECKS[i.checks] || i.checks)
  }
  if (i.auto_merge && i.stage === 'pr_open') progress.push('auto-merge on')
  const href = workLink(i)
  const attrs = href ? { href, target: '_blank', rel: 'noopener noreferrer' } : {}
  const cls = 'row work' + (i.stage === 'merged' || i.stage === 'closed' ? ' done' : '')
  return el(href ? 'a' : 'div', { ...attrs, class: cls, 'data-item': `${i.repo}#${i.number || 'pr' + i.pr}` },
    el('div', { class: 't' },
      el('strong', {}, (i.number ? `#${i.number} ` : '') + i.title),
      el('span', { class: 'quiet' }, REPO_SHORT[i.repo] || i.repo),
      el('span', { class: i.checks === 'failing' ? 'warn' : 'what' }, progress.join(' · '))),
    el('div', { class: 'w' }, updated(i.updated_at)))
}

function botCount(open, done) {
  if (!open && !done) return 'Nothing claimed'
  return [open ? `${open} open` : null, done ? `${done} finished in the last day` : null].filter(Boolean).join(', ')
}

// Today's time alone; an older day with its date.
function updated(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d)) return iso
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  return d.toDateString() === new Date().toDateString() ? time
    : `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })}, ${time}`
}

async function boardView() {
  setTab('board')
  const r = await call('GET', '/work')
  if (refused(r)) return
  const work = r.body.work
  const bar = screenBar({ title: 'Board', heading: true })
  if (!work) return show(bar, el('p', { class: 'quiet' }, 'No work has come in from the Bots yet.'))
  const items = work.items
  const tasks = work.tasks || []
  const rank = b => (ORDER.indexOf(b) + 1) || ORDER.length + 1
  const bots = [...new Set([...ORDER, ...(items || []).map(i => i.bot)])].sort((a, b) => rank(a) - rank(b))
  show(bar,
    items ? bots.map(b => {
      const mine = items.filter(i => i.bot === b)
      const open = mine.filter(i => i.stage !== 'merged' && i.stage !== 'closed').length
      return [
        el('div', { class: 'row head', 'data-bot': b }, avatar(b, NAMES[b] || b),
          el('div', { class: 't' }, el('strong', {}, NAMES[b] || b),
            el('span', { class: 'quiet' }, botCount(open, mine.length - open)))),
        mine.map(workRow)]
    }) : el('p', { class: 'quiet' }, 'Nothing yet.'),
    tasks.length ? [el('h2', { class: 'section' }, "Hermes's Kanban"),
      el('table', {},
        el('tr', {}, el('th', {}, 'Task'), el('th', {}, 'Who'), el('th', {}, 'State')),
        tasks.map(t => el('tr', {}, el('td', {}, t.title), el('td', {}, t.assignee), el('td', {}, t.status))))] : null)
}

const NAMES = { bramble: 'Bramble', rift: 'Rift', sprocket: 'Sprocket', echo: 'Echo', magpie: 'Magpie', wisp: 'Wisp' }
// What each Bot does, as the Thornswood Bots list says it, in the list's order.
const WHAT = {
  bramble: 'router, merges', rift: 'collection, load order', sprocket: 'launcher, installer',
  echo: 'site, Discord, voice', magpie: 'GitHub, CI, runners', wisp: 'gamemode, game UI'
}
const ORDER = Object.keys(WHAT)
const STATE = {
  queued: 'Waiting to send',
  leased: 'Sending',
  sent: 'Sent',
  uncertain: 'Not sure this got there. Look above before you send it again.',
  dead: 'Not sent',
  quarantined: 'Held back after a restore; not sent'
}
let drafts = {}

// Patrick's own Hermes, by the names he gave them (1 Nov). The ids stay me and one.
const GATEWAY = { me: 'Hermes', one: 'One' }
const GATEWAY_WHAT = { me: 'your own Hermes on the VM', one: 'your Hermes on One' }

function at(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return isNaN(d) ? iso : d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

// The gateway's state in a few words for the list, and in a sentence for its
// own screen. One reads as off only when the tailnet says the machine is.
function stateWords(g) {
  const one = g.gateway === 'one'
  const since = g.since ? ` at ${at(g.since)}` : ''
  switch (g.state) {
    case 'ready': return ['On', 'On.']
    case 'machine_off': return ['One is off', `One is off, since ${at(g.since)}. (Tailscale says the machine is offline.)`]
    case 'connector_stopped': return ['Connector stopped', one
      ? `One is on, but its connector stopped${since}.`
      : `Its connector on the VM stopped${since}.`]
    case 'not_heard': return ['Not heard from', `One's connector has not been heard from since ${at(g.since)}, ` +
      'and the tailnet has not said yet whether One is on.']
    case 'gateway_down': return ['Gateway offline', `${one ? 'Hermes on One' : 'Hermes on the VM'} is not running. ` +
      `The connector is.${g.why ? ` (${g.why})` : ''}`]
    case 'check_failed': return ['Check failed', `The connector runs, but its check of Hermes failed: ${g.why || 'it gave no reason'}.`]
    case 'missing': return ['Connector missing', `The connector on ${one ? 'One' : 'the VM'} is not installed yet.`]
    case 'turned_off': return ['Turned off', 'Chat with your own Hermes is turned off on the core.']
    default: return [g.online ? 'On' : 'Off', g.online ? 'On.' : 'Not answering.']
  }
}

function gatewayLine(g) {
  return el('span', { class: g.state === 'ready' ? 'rec' : 'rec off' }, stateWords(g)[0])
}

// Online and offline, from what is really running (Patrick, 2 Nov: "the design had green and red circles
// to indicate bots online or offline"). Green: its gateway is running. Red: it is not (or One's machine is
// off). Amber: on its way up or down, or not heard from, so nobody is told it is up or down when that is
// not known. A Bot's word is Hermes's own state of its gateway, read by the Bots' connector every half
// minute; Hermes and One have theirs from their own connectors (stateWords).
const BOT_STATE = {
  ready: ['on', 'On'], draining: ['wait', 'Restarting'], starting: ['wait', 'Starting'],
  absent: ['off', 'Offline'], incompatible: ['off', 'Wrong version'], inaccessible: ['off', 'Cannot be reached'],
  conflict: ['off', 'Two gateways'], check_failed: ['wait', 'Check failed'], not_heard: ['wait', 'Not heard from']
}
const GATEWAY_DOT = { ready: 'on', machine_off: 'off', gateway_down: 'off', connector_stopped: 'off', missing: 'off',
  turned_off: 'off' }
function botLive(live) {
  const [cls, words] = BOT_STATE[(live && live.state) || 'not_heard'] || BOT_STATE.not_heard
  return { cls, words }
}
function gatewayLive(g) {
  return { cls: GATEWAY_DOT[g.state] || 'wait', words: stateWords(g)[0] }
}
function dot(live) {
  return el('span', { class: 'dot ' + live.cls, role: 'img', 'aria-label': live.words, title: live.words })
}
// An avatar with its dot on its corner.
function liveAvatar(id, label, live) {
  return el('div', { class: 'avwrap' }, avatar(id, label), live ? dot(live) : null)
}
function liveLine(live) {
  return el('span', { class: 'rec live ' + live.cls }, live.words)
}

async function botsView(into = 'view') {
  if (into === 'view') setTab('bots')
  const [r, own] = await Promise.all([call('GET', '/chat'), call('GET', '/own')])
  if (refused(r, into)) return
  const rank = b => (ORDER.indexOf(b.bot) + 1) || ORDER.length + 1
  const bots = r.body.bots.slice().sort((a, b) => rank(a) - rank(b))
  // Hermes, then One, then the six Bots (Patrick's mock-up answer, 1 Nov).
  const gateways = own.status === 200 ? ['me', 'one'].map(gw => own.body.gateways.find(g => g.gateway === gw)).filter(Boolean) : []
  showIn(into, [
    screenBar({ title: 'Thornswood Bots', heading: true }),
    gateways.map(g => listRow({ 'data-gateway': g.gateway, onclick: () => go('/m/hermes/' + g.gateway) }, {
      agent: g.gateway, title: GATEWAY[g.gateway], live: gatewayLive(g),
      lines: [el('span', { class: 'what' }, GATEWAY_WHAT[g.gateway]),
        g.last ? snippet(g.last.preview, 2, el('b', { class: 'sender' }, chatName(g.last) + ': '))
          : el('span', { class: 'quiet' }, g.as_of ? 'No chats yet.' : 'Its chats show here once it is turned on.')],
      right: [gatewayLine(g), g.unsettled ? el('span', { class: 'rec' }, `${g.unsettled} waiting`) : null] })),
    own.status === 200 ? null : el('p', { class: 'warn' }, said(own.body)),
    bots.map(b => listRow({ 'data-bot': b.bot, onclick: () => go('/m/bots/' + b.bot) }, {
      agent: b.bot, title: NAMES[b.bot] || b.bot, live: botLive(b.live),
      lines: [WHAT[b.bot] ? el('span', { class: 'what' }, WHAT[b.bot]) : null,
        b.last ? snippet(b.last.text) : el('span', { class: 'quiet' }, 'No messages yet.')],
      right: [liveLine(botLive(b.live)), b.unsettled ? el('span', { class: 'rec' }, `${b.unsettled} waiting`) : null] }))])
}

// Sending to his own Hermes needs a passkey sign-in from the last 30 minutes
// (Patrick, 1 Nov). This says until when, or offers that sign-in. A new chat is
// made with full power; Hermes keeps a chat's own approval setting for good, so
// in a chat started elsewhere a step that needs an approval is refused unless
// that chat has /yolo on.
// The passkey sign-in a send needs: nothing while it holds (the composer is the
// sign that he can send), and a sign-in only once it has run out. Patrick, 2 Nov:
// the paragraph that said until when he could send, on every chat, was clutter.
function signInPart(v, status) {
  if (v.sign_in_until) return null
  return el('div', { class: 'card' },
    el('p', { class: 'quiet' }, said({ error: 'passkey_needed' })),
    el('button', { class: 'secondary', type: 'button', onclick: async () => {
      const s = await passkeySignIn()
      if (s.status === 200) return route()
      status.textContent = said(s.body)
    } }, 'Sign in with your ' + signInWord()))
}

function composer(key, label, send, status, gw) {
  const draft = drafts[key] || (drafts[key] = { text: '', key: newKey() })
  const box = el('textarea', { rows: '3', 'aria-label': label, placeholder: label })
  box.value = draft.text
  box.addEventListener('input', () => {
    if (box.value !== draft.text) { draft.text = box.value; draft.key = newKey() }
  })
  const button = el('button', { class: 'primary', type: 'button', onclick: async () => {
    const text = box.value.trim()
    if (!text) return
    status.textContent = 'Sending...'
    const s = await send(text, draft.key)
    if (s.status === 200) { drafts[key] = null; return route() }
    if (s.status === 401) { csrf = null; return signInView(said(s.body)) }
    const d = s.body.detail || {}
    status.textContent = s.status === 0
      ? 'Could not reach the core. Sending again sends the same message, never a second one.'
      : (gw && d.state ? stateWords({ gateway: gw, ...d })[1] : said(s.body))
  } }, label)
  return [box, button]
}

// The state in a sentence, unless it is ready, or turned off (said by itself below).
function offLine(v) {
  if (v.state === 'ready' || v.state === 'turned_off') return connectorLine(v)
  return el('p', { class: 'warn', 'data-state': v.state }, stateWords(v)[1])
}

// Which copy a gateway's connector runs, when it is not the core's own release: updating itself,
// an update it could not do, or an old copy that does not update itself.
function connectorLine(v) {
  const c = v.connector
  if (!c || c.current) return null
  const name = GATEWAY[v.gateway] || v.name
  const short = x => (x || '').slice(0, 7)
  const words = c.updating ? `${name}'s connector is updating itself to ${short(c.updating)}.`
    : c.update_why ? `${name}'s connector ${c.update_why}.`
    : c.commit ? `${name}'s connector runs ${short(c.commit)} and updates itself to ${short(c.release)} next.`
    : `${name}'s connector is an old copy that does not update itself. It needs its one-time update.`
  return el('p', { class: c.update_why || !c.commit ? 'warn' : 'quiet', 'data-connector': c.updating ? 'updating' : (c.commit ? 'behind' : 'old') }, words)
}

// ── the chat layout and Markdown (redesign step 1, plan Part 11) ──────────
//
// Messages are bubbles: Patrick's on the right in gold, everyone else's on the
// left, with the sender's name once per run and a day line when the day
// changes. Hermes and the Bots write Markdown, so it is drawn: paragraphs,
// bold, italic, struck text, inline code, code blocks with Copy, lists,
// quotes, headings, rules, tables and links. Every node is made here and
// filled with text; nothing in a message is ever read as HTML, so a message
// cannot run script or load anything (the page's CSP allows images from this
// origin only, and no image is drawn from a message). A link opens only an
// http or https address, in a new tab, with no referrer.

const INLINE = new RegExp([
  '`([^`\\n]+)`',                                                // 1 code
  '\\[([^\\]\\n]+)\\]\\((https?:\\/\\/[^\\s)]+)\\)',              // 2 text, 3 address
  '(https?:\\/\\/[^\\s<>()]*[^\\s<>().,;:!?\'"\\]])',             // 4 a bare address
  '\\*\\*(?=\\S)([^\\n]+?)(?<=\\S)\\*\\*',                        // 5 bold
  '(?<!\\w)__(?=\\S)([^\\n]+?)(?<=\\S)__(?!\\w)',                 // 6 bold
  '~~(?=\\S)([^\\n]+?)(?<=\\S)~~',                                // 7 struck
  '(?<![\\w*])\\*(?=[^\\s*])([^*\\n]+?)(?<=\\S)\\*(?![\\w*])',    // 8 italic
  '(?<!\\w)_(?=[^\\s_])([^_\\n]+?)(?<=\\S)_(?!\\w)',              // 9 italic, never inside a_name
  '(?<![\\w/])(@(?:file|folder|url|image|tool|terminal|git):(?:"[^"\\n]+"|\'[^\'\\n]+\'|`[^`\\n]+`|\\S+))', // 10 a reference
  '(?<![\\w@/])@([A-Za-z][\\w-]{0,30})(?![\\w:@-])'               // 11 a mention
].join('|'), 'g')

// Who a mention names, as the app names them, with their colour (app.css .a-<id>).
const MENTIONED = { bramble: 'Bramble', rift: 'Rift', sprocket: 'Sprocket', echo: 'Echo', magpie: 'Magpie', wisp: 'Wisp',
  hermes: 'Hermes', one: 'One', patrick: 'Patrick' }

function link(text, href) {
  return el('a', { href, target: '_blank', rel: 'noopener noreferrer' }, text)
}

// still: a link is drawn as one but is not one, for words inside something tapped as a whole (a list's row).
function inlineMd(text, still = false) {
  const out = []
  let last = 0
  text = String(text ?? '')
  const to = (words, href) => still ? el('span', { class: 'lnk' }, words) : link(words, href)
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) out.push(text.slice(last, m.index))
    if (m[1] !== undefined) out.push(el('code', {}, m[1]))
    else if (m[2] !== undefined) out.push(to(m[2], m[3]))
    else if (m[4] !== undefined) out.push(to(m[4], m[4]))
    else if (m[5] !== undefined || m[6] !== undefined) out.push(el('strong', {}, inlineMd(m[5] ?? m[6], still)))
    else if (m[7] !== undefined) out.push(el('s', {}, inlineMd(m[7], still)))
    else if (m[8] !== undefined || m[9] !== undefined) out.push(el('em', {}, inlineMd(m[8] ?? m[9], still)))
    // A file or other reference is its name, as plain text: it never opens, loads or attaches anything.
    else if (m[10] !== undefined) out.push(el('span', { class: 'ref' }, refLabel(m[10])))
    else {
      const who = MENTIONED[m[11].toLowerCase()]
      const id = who && (AGENT_BY_NAME[who] || (who === 'Patrick' ? 'patrick' : null))
      out.push(el('span', { class: 'mention' + (id ? ' a-' + id : '') }, '@' + (who || m[11])))
    }
    last = m.index + m[0].length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

const FENCE = /^ {0,3}(`{3,}|~{3,})\s*([\w+#.-]*)/
const HEADING = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/
const RULE = /^ {0,3}([-*_])( *\1){2,} *$/
const QUOTE = /^ {0,3}>/
const ITEM = /^( *)([-*+]|\d{1,9}[.)])\s+(.*)$/
const TABLE_RULE = /^ *\|? *:?-{3,}:? *(\| *:?-{3,}:? *)*\|? *$/

function isTable(lines, i) {
  return lines[i].includes('|') && i + 1 < lines.length && lines[i + 1].includes('|') && TABLE_RULE.test(lines[i + 1])
}

function startsBlock(lines, i) {
  const line = lines[i]
  return FENCE.test(line) || HEADING.test(line) || RULE.test(line) || QUOTE.test(line) || ITEM.test(line) || isTable(lines, i)
}

function cells(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim())
}

function codeBlock(code, lang) {
  const pre = el('pre', {}, el('code', {}, code))
  const button = el('button', { type: 'button', class: 'copy', onclick: async () => {
    try {
      await navigator.clipboard.writeText(code)
      button.textContent = 'Copied'
    } catch (e) {
      // Some views refuse the clipboard: the code is selected for the phone's own Copy.
      const range = document.createRange()
      range.selectNodeContents(pre)
      const sel = getSelection()
      sel.removeAllRanges()
      sel.addRange(range)
      button.textContent = 'Selected'
    }
  } }, 'Copy')
  return el('div', { class: 'md-code' }, el('div', { class: 'md-code-bar' }, el('span', {}, lang || 'code'), button), pre)
}

function indentOf(line) { return line.match(/^ */)[0].length }

function listMd(lines, i, out) {
  const first = lines[i].match(ITEM)
  const base = first[1].length
  const ordered = /\d/.test(first[2])
  const start = ordered ? parseInt(first[2], 10) : 1
  const list = el(ordered ? 'ol' : 'ul', start !== 1 ? { start: String(start) } : {})
  let li = null, body = [], inner = 0
  const flush = () => { if (li) { li.append(...blocksMd(body)); list.append(li) } }
  while (i < lines.length) {
    const line = lines[i]
    const m = line.match(ITEM)
    if (m && m[1].length === base && /\d/.test(m[2]) === ordered) {
      flush()
      li = el('li')
      body = [m[3]]
      inner = m[1].length + m[2].length + 1
      i++
      continue
    }
    if (!line.trim()) {
      // A blank line ends the list, unless what follows is still under it.
      const next = lines[i + 1]
      const nm = next === undefined ? null : next.match(ITEM)
      if (next !== undefined && (indentOf(next) > base || (nm && nm[1].length === base && /\d/.test(nm[2]) === ordered))) {
        body.push('')
        i++
        continue
      }
      break
    }
    if (indentOf(line) > base) { body.push(line.slice(Math.min(indentOf(line), inner))); i++; continue }
    if (!startsBlock(lines, i)) { body.push(line.trim()); i++; continue }
    break
  }
  flush()
  out.push(list)
  return i
}

function blocksMd(lines) {
  const out = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    if (!line.trim()) { i++; continue }
    const fence = line.match(FENCE)
    if (fence) {
      const close = new RegExp('^ {0,3}\\' + fence[1][0] + '{' + fence[1].length + ',}\\s*$')
      const code = []
      i++
      while (i < lines.length && !close.test(lines[i])) code.push(lines[i++])
      i++
      out.push(codeBlock(code.join('\n'), fence[2]))
      continue
    }
    const head = line.match(HEADING)
    if (head) { out.push(el('p', { class: 'md-h' }, inlineMd(head[2]))); i++; continue }
    if (RULE.test(line)) { out.push(el('hr')); i++; continue }
    if (QUOTE.test(line)) {
      const quoted = []
      while (i < lines.length && QUOTE.test(lines[i])) quoted.push(lines[i++].replace(/^ {0,3}> ?/, ''))
      out.push(el('blockquote', {}, blocksMd(quoted)))
      continue
    }
    if (isTable(lines, i)) {
      const top = cells(line)
      const rows = []
      i += 2
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) rows.push(cells(lines[i++]))
      out.push(el('div', { class: 'md-table' }, el('table', {},
        el('thead', {}, el('tr', {}, top.map(c => el('th', {}, inlineMd(c))))),
        el('tbody', {}, rows.map(r => el('tr', {}, top.map((_, k) => el('td', {}, inlineMd(r[k] || '')))))))))
      continue
    }
    if (ITEM.test(line)) { i = listMd(lines, i, out); continue }
    // A paragraph keeps its line breaks, the way a chat message is written.
    const para = [line.trim()]
    i++
    while (i < lines.length && lines[i].trim() && !startsBlock(lines, i)) para.push(lines[i++].trim())
    out.push(el('p', {}, para.map((l, k) => k ? [el('br'), inlineMd(l)] : inlineMd(l))))
  }
  return out
}

function markdown(text) {
  const lines = String(text || '').replace(/\r\n?/g, '\n').replace(/\t/g, '    ').split('\n')
  return el('div', { class: 'text md' }, blocksMd(lines))
}

// What Patrick wrote, apart from what Hermes attached to it. Hermes keeps an
// @file: reference where it was typed and puts the file's text under a
// "--- Attached Context ---" line (agent/context_references.py); a long paste
// in Hermes Desktop is such a file, composer-pastes/pasted_content_<stamp>.txt.
// Hermes Desktop shows the words and a chip for each file
// (chat-messages/hydration.ts); so does the phone, and the chip opens the text.
const ATTACHED = /(?:^|\n)--- Attached Context ---[ \t]*\n/
const WARNINGS = /(?:^|\n)--- Context Warnings ---[\s\S]*$/
const REF = /@(file|folder|url|image|tool|terminal):(?:"[^"\n]+"|'[^'\n]+'|`[^`\n]+`|\S+)/g
const PASTE = /(?:^|[\\/])pasted_content_[\w-]+\.txt$/

function refLabel(raw) {
  const kind = raw.slice(1, raw.indexOf(':'))
  const target = raw.slice(raw.indexOf(':') + 1).replace(/^(["'`])(.*)\1$/, '$2')
  if (kind === 'file' && PASTE.test(target)) return 'Pasted text'
  return target.split(/[\\/]/).filter(Boolean).pop() || target
}

function authored(text) {
  text = String(text || '')
  const cut = text.match(ATTACHED)
  let words = (cut ? text.slice(0, cut.index) : text).replace(WARNINGS, '')
  const attached = cut ? text.slice(cut.index + cut[0].length) : ''
  const refs = [...new Set([...words.matchAll(REF), ...attached.matchAll(REF)].map(m => m[0]))]
  // Each attached file starts "<mark> <reference> (<n> tokens)" on a line of its own.
  const starts = refs.map(raw => {
    let at = -1, from = 0
    while ((at = attached.indexOf(' ' + raw + ' (', from)) !== -1) {
      const line = attached.lastIndexOf('\n', at) + 1
      if (!/\s/.test(attached.slice(line, at))) return { raw, at: line }
      from = at + 1
    }
    return { raw, at: -1 }
  })
  const found = starts.filter(x => x.at !== -1).sort((a, b) => a.at - b.at)
  const files = refs.map(raw => {
    const k = found.findIndex(x => x.raw === raw)
    let body = '', tokens = null
    if (k !== -1) {
      const block = attached.slice(found[k].at, k + 1 < found.length ? found[k + 1].at : undefined).trim()
      const nl = block.indexOf('\n')
      const head = nl === -1 ? block : block.slice(0, nl)
      const n = head.match(/\((\d+) tokens\)$/)
      tokens = n ? Number(n[1]) : null
      body = (nl === -1 ? '' : block.slice(nl + 1)).replace(/^(`{3,})[^\n]*\n([\s\S]*?)\n\1\s*$/, '$2')
    }
    return { raw, label: refLabel(raw), tokens, body }
  })
  if (cut || refs.length) for (const raw of refs) words = words.split(raw).join('')
  return { words: words.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim(), files }
}

// A chat's name: Hermes's title, else its first message, as Hermes Desktop names
// an untitled chat (chat-runtime.ts sessionTitle). The connector sends `first`
// only for a chat with no title.
function chatName(c) {
  return (c && (c.title || (c.first && preview(c.first, 80)))) || 'Untitled chat'
}

// One line for a list: the words, else what was attached.
// A list line is plain words. Markdown's marks are for a message shown whole; in a line cut short they
// show as stray stars and backticks. So a line drops them, then is cut at the last whole word, with "…".
// The core cuts what it sends longer than any line here (200 and 120), so this cut is the one shown.
function plain(text) {
  return String(text || '')
    .replace(/^[ \t]*(```|~~~).*$/gm, '')                                  // code fences
    .replace(/^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*$/gm, '') // a table's rule
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')                              // an image: its words
    .replace(/\[([^\]]+)\]\((?:[^()]|\([^)]*\))*\)/g, '$1')                // a link: its words
    .replace(/<(https?:[^>\s]+)>/g, '$1')
    .replace(/^[ \t]{0,3}(#{1,6}[ \t]+|>[ \t]?|[-*+][ \t]+\[[ xX]\][ \t]+|[-*+][ \t]+|\d{1,9}[.)][ \t]+)/gm, '') // heading, quote, list
    .replace(/^[ \t]{0,3}([-*_][ \t]*){3,}$/gm, '')                         // a rule
    .replace(/\*\*|__|~~|`+/g, '')                                          // bold, strike, code
    .replace(/(^|[\s(\[])[*_](?=\S)([^*_\n]*?\S)[*_](?=$|[\s).,!?:;\]])/gm, '$1$2') // italics
    .replace(/^[ \t]*\|[ \t]*|[ \t]*\|[ \t]*$/gm, '')                       // a table row's outer bars
    .replace(/[ \t]*\|[ \t]*/g, ' · ')                                     // and the bars between its cells
}

function clip(line, n) {
  if (line.length <= n) return line
  const cut = line.slice(0, n)
  const at = cut.search(/\s\S*$/)
  return (at > n / 2 ? cut.slice(0, at) : cut).replace(/[\s.,;:!?·–-]+$/, '') + '…'
}

function preview(text, n = 120) {
  const a = authored(text)
  const line = plain(a.words).replace(/\s+/g, ' ').trim() || a.files.map(f => f.label).join(', ')
  return clip(line, n)
}

// A message in a short space: a list's line, a reply's quote. It is drawn the way the message is (bold,
// italic, code, links, mentions, a file as its name), and only its blocks are laid out as one run: a heading,
// a list's bullet, a quote's mark, a code fence and a table's rule go, a table's cells are joined with " · ".
// It is cut by lines, at a whole word, with "…" (app.css .snip), never in the middle of a mark. Patrick,
// 2 Nov: the plain words were better than the raw text, "but I want the proper formatting", everywhere.
function flatMd(text) {
  return String(text || '').replace(/\r\n?/g, '\n')
    .replace(/^[ \t]*(```|~~~).*$/gm, '')
    .replace(/^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*$/gm, '')
    .replace(/^[ \t]{0,3}([-*_][ \t]*){3,}$/gm, '')
    .replace(/^[ \t]{0,3}(#{1,6}[ \t]+|>[ \t]?)/gm, '')
    .replace(/^[ \t]*[-*+][ \t]+(\[[ xX]\][ \t]+)?/gm, '')
    .replace(/^[ \t]*\|[ \t]*|[ \t]*\|[ \t]*$/gm, '')
    .replace(/[ \t]*\|[ \t]*/g, ' · ')
    .replace(/\s+/g, ' ').trim()
}

function snippet(text, lines = 2, sender) {
  const a = authored(text)
  const words = flatMd(a.words)
  // The words, else what was attached (a line the core cut can end inside the attached text).
  return el('span', { class: 'snip l' + lines },
    sender || null,
    words ? inlineMd(words, true) : a.files.map(f => el('span', { class: 'ref' }, f.label)))
}

// Who said a list's last line, the way a group chat shows it: the name, in its colour, then the words.
function sender(who) {
  const name = NAMES[who] || who
  return el('b', { class: 'sender a-' + (AGENT_BY_NAME[name] || 'room') }, name + ': ')
}

function fileChips(files) {
  return files.map(f => {
    const what = f.tokens ? `${f.label} · ${f.tokens.toLocaleString()} tokens` : f.label
    return f.body
      ? el('details', { class: 'att' }, el('summary', {}, what), el('pre', {}, f.body))
      : el('div', { class: 'att' }, el('span', {}, what))
  })
}

function clock(iso) {
  const d = new Date(iso)
  return isNaN(d) ? '' : d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

function dayWords(d) {
  const today = new Date()
  const yesterday = new Date(today)
  yesterday.setDate(today.getDate() - 1)
  if (d.toDateString() === today.toDateString()) return 'Today'
  if (d.toDateString() === yesterday.toDateString()) return 'Yesterday'
  return d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric',
    year: d.getFullYear() === today.getFullYear() ? undefined : 'numeric' })
}

// Each item: { mine, who, agent, at, text, mid, status, note, warn, reactions, react, noReact }.
// reactions: [{ emoji, by: 'patrick' or 'hermes', pending }], and agent names who 'hermes' is
// (a Bot). react(emoji or null) sets Patrick's reaction; without it, noReact says why a message
// here takes none.
function bubbles(items) {
  const out = []
  let day = null, last = null
  for (const m of items) {
    const d = new Date(m.at)
    if (!isNaN(d) && d.toDateString() !== day) {
      day = d.toDateString()
      last = null
      out.push(el('div', { class: 'day' }, dayWords(d)))
    }
    const by = m.mine ? null : m.who
    const attrs = { class: 'msg ' + (m.mine ? 'mine' : 'theirs') }
    if (m.mid !== undefined && m.mid !== null) attrs['data-mid'] = String(m.mid)
    if (m.status) attrs['data-status'] = m.status
    // A reply starts with Hermes's own reply pointer; the quote shows above what he wrote, and his own
    // words show with a chip for each file Hermes attached to them.
    const ptr = m.mine ? String(m.text || '').match(POINTER) : null
    const own = m.mine ? authored(ptr ? m.text.slice(ptr[0].length) : m.text) : null
    const bub = el('div', { class: 'bub' },
      ptr ? el('div', { class: 'quote' },
        el('div', { class: 'qwho' }, ptr[1] ? 'Replying to ' + (m.agent || m.who || 'Hermes') : 'Replying to your message'),
        el('div', { class: 'qtext' }, snippet(ptr[2], 3))) : null,
      (m.sent || []).map(f => el('div', { class: 'att' }, el('span', {}, `${f.name} · ${sizeWords(f.size)}`))),
      own ? [fileChips(own.files), own.words ? markdown(own.words) : null] : markdown(m.text))
    if (ptr && m.replyTo) replyLink(bub, m.replyTo)
    if (m.mid !== undefined && m.mid !== null && !m.status) {
      // Tapping a message (not a link or a button in it, and not while selecting text) opens its actions.
      bub.tabIndex = 0
      bub.title = 'Tap for reactions and copy'
      bub.addEventListener('click', e => {
        // Opening a pasted file's chip opens the chip, not the actions too.
        if (e.target.closest('a, button, summary') || String(getSelection())) return
        messageSheet(m)
      })
      bub.addEventListener('keydown', e => { if (e.key === 'Enter' && e.target === bub) messageSheet(m) })
    }
    const reacts = (m.reactions || []).map(r => {
      const mineR = r.by === 'patrick'
      const label = (mineR ? 'Your reaction ' : `${m.mine ? (m.agent || 'Hermes') : m.who}'s reaction `) + r.emoji +
        (r.pending ? ', being set' : '')
      return mineR && m.react
        ? el('button', { type: 'button', class: 'react me' + (r.pending ? ' pending' : ''),
            'aria-label': label + ', tap to take it off', onclick: () => m.react(null) }, r.emoji)
        : el('span', { class: 'react' + (mineR ? ' me' : '') + (r.pending ? ' pending' : ''), role: 'img',
            'aria-label': label }, r.emoji)
    })
    out.push(el('div', attrs,
      !m.mine && (last === null || by !== last.by) ? el('div', { class: 'who a-' + (AGENT_BY_NAME[m.who] || 'room') }, m.who) : null,
      bub,
      reacts.length ? el('div', { class: 'reacts' }, reacts) : null,
      el('div', { class: 'meta' + (m.warn ? ' warn' : '') }, [clock(m.at), m.note].filter(Boolean).join(' · '))))
    last = { by }
  }
  return out
}

// Hermes's own reply pointer, the line its gateway puts at the top of a message that replies to another
// (contracts.json "replies"). A reply from the phone opens with it too (reply style A), so it shows as
// the quote it is, from his own messages only.
const POINTER = /^\[Replying to( your previous message)?: "([\s\S]*?)"\]\n\n/

// The core keeps which message a reply answers, so its quote goes back to that message, or says where
// it is when it is not loaded yet.
function replyLink(bub, mid) {
  const q = bub.querySelector('.quote')
  if (!q) return
  q.classList.add('linked')
  q.tabIndex = 0
  q.setAttribute('role', 'button')
  q.setAttribute('aria-label', 'Go to the message this answers')
  const note = el('div', { class: 'meta', role: 'status' })
  const go = e => {
    e.stopPropagation()
    const n = document.querySelector(`#view .msg[data-mid="${CSS.escape(String(mid))}"]`)
    if (!n) { note.textContent = 'That message is further up. Older messages, at the top, loads it.'; q.after(note); return }
    note.remove()
    n.scrollIntoView({ block: 'center' })
    n.classList.remove('flash')
    void n.offsetWidth
    n.classList.add('flash')
  }
  q.addEventListener('click', go)
  q.addEventListener('keydown', e => { if (e.key === 'Enter') go(e) })
}

// The words a reply quotes: the whole message, as Hermes quotes it (a preview would lose the rest of a
// list or code), without the pointer it opened with, cut at the core's 2000 characters.
// A message that is only a paste quotes the paste's words. A file is never quoted as its reference (the
// core makes any that is left plain words too, own.py inert): on 2 Nov a quote that held the paste's
// reference made Hermes attach the paste again, and one cut inside a reference asked for a file that does
// not exist. A long quote is cut at a whole word.
function quoteOf(text) {
  const t = String(text || '')
  const a = authored(t.replace(POINTER, ''))
  const body = a.words || a.files.map(f => f.body).filter(Boolean).join('\n\n').trim() ||
    a.files.map(f => '[' + f.label + ']').join(' ')
  return clip(body, 1999)
}

// The reactions the phone offers; the core takes only these (own.py REACTIONS).
const REACTIONS = ['\u{1F44D}', '\u2764\uFE0F', '\u{1F602}', '\u{1F62E}', '\u{1F622}', '\u{1F64F}', '\u2705', '\u{1F440}']

function closeSheet() {
  const old = document.querySelector('.sheet-back')
  if (old) old.remove()
}

// A message's actions: a reaction (Hermes keeps one per person, so a new one
// replaces yours and the same one again takes it off), and Copy text.
function messageSheet(m) {
  closeSheet()
  const mineNow = ((m.reactions || []).find(r => r.by === 'patrick') || {}).emoji || null
  const note = el('p', { class: 'quiet', role: 'status' })
  const choose = emoji => { closeSheet(); m.react(emoji) }
  const copy = el('button', { class: 'secondary', type: 'button', onclick: async () => {
    try {
      // His own message copies what he wrote (an attached paste's text when that is all), never Hermes's attachment block.
      const a = m.mine ? authored(m.text) : null
      await navigator.clipboard.writeText(a ? (a.words || a.files.map(f => f.body).join('\n\n')) : m.text)
      note.textContent = 'Copied.'
    } catch (e) {
      note.textContent = 'This view does not allow copying. Press and hold the message text to copy it.'
    }
  } }, 'Copy text')
  const back = el('div', { class: 'sheet-back' },
    el('div', { class: 'sheet', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Message actions' },
      m.react
        ? el('div', { class: 'emoji-row' }, REACTIONS.map(e => el('button', { type: 'button', 'aria-label': 'React ' + e,
            'aria-pressed': String(mineNow === e), onclick: () => choose(mineNow === e ? null : e) }, e)))
        : el('p', { class: 'quiet' }, m.noReact || 'This message takes no reaction.'),
      m.react && mineNow ? el('button', { class: 'secondary', type: 'button', onclick: () => choose(null) },
        'Take my reaction off') : null,
      m.reply ? el('button', { class: 'secondary', type: 'button', onclick: () => { closeSheet(); m.reply() } }, 'Reply') : null,
      copy,
      el('button', { class: 'secondary', type: 'button', onclick: closeSheet }, 'Close'),
      note))
  back.addEventListener('click', e => { if (e.target === back) closeSheet() })
  back.addEventListener('keydown', e => { if (e.key === 'Escape') closeSheet() })
  document.body.append(back)
  const first = back.querySelector('button')
  if (first) first.focus()
}

// The bar on top of a chat: back, then the chat's name and where it lives.
// More, when given, opens the chat's own actions.
function chatBar(back, backLabel, title, under, agent, label, more, live) {
  const bar = screenBar({ back, backLabel, agent, label, title, under, live,
    right: more ? el('button', { class: 'ib', type: 'button', 'aria-label': 'Chat actions', onclick: more }, icon('more')) : null })
  bar.classList.add('chatbar')
  return bar
}

// The box at the foot of a chat. It grows with what is typed, up to six
// lines, and keeps its draft (and the draft's idempotency key) while the
// chat redraws. Enter is a new line; the button sends.
function chatComposer(key, label, buttonLabel, send, status, gw, attach) {
  const draft = drafts[key] || (drafts[key] = { text: '', key: newKey() })
  if (!draft.files) draft.files = []
  // A reply being written shows the words it quotes, which open the message as sent.
  const replying = draft.reply ? el('div', { class: 'replying' },
    el('div', { class: 'rq' },
      el('strong', {}, 'Replying to ' + draft.reply.who),
      el('div', { class: 'qtext' }, snippet(draft.reply.quote, 3))),
    el('button', { class: 'back', type: 'button', 'aria-label': 'Cancel the reply', onclick: () => {
      draft.reply = null
      draft.key = newKey()
      route()
    } }, icon('x'))) : null
  const box = el('textarea', { rows: '1', 'aria-label': label, placeholder: label, enterkeyhint: 'enter' })
  box.value = draft.text
  const fit = () => { box.style.height = 'auto'; box.style.height = Math.min(box.scrollHeight + 2, 160) + 'px' }
  box.addEventListener('input', () => {
    if (box.value !== draft.text) { draft.text = box.value; draft.key = newKey() }
    fit()
  })
  requestAnimationFrame(fit)
  const button = el('button', { class: 'send', type: 'button', 'aria-label': buttonLabel, onclick: async () => {
    const text = box.value.trim()
    const ready = draft.files.filter(f => f.id)
    if (draft.files.some(f => f.state === 'up')) { status.textContent = 'Wait for the files to finish uploading.'; return }
    if (!text && !ready.length) { status.textContent = 'Type a message first.'; return }
    status.textContent = 'Sending...'
    button.disabled = true
    const s = await send(text, draft.key, draft.reply, ready.map(f => f.id))
    button.disabled = false
    if (s.status === 200) { drafts[key] = null; chatPlace.toEnd = true; return route() }
    if (s.status === 401) { csrf = null; return signInView(said(s.body)) }
    const d = s.body.detail || {}
    status.textContent = s.status === 0
      ? 'Could not reach the core. Sending again sends the same message, never a second one.'
      : (gw && d.state ? stateWords({ gateway: gw, ...d })[1] : said(s.body))
  } }, icon('send'))
  const plus = attach ? el('button', { class: 'plus', type: 'button', 'aria-label': 'Add photos or files',
    onclick: () => attachSheet(attach, draft, status) }, icon('plus')) : null
  return [replying, attach ? attachStrip(attach, draft, status) : null,
    el('div', { class: 'composer', 'data-reply': draft.reply ? String(draft.reply.mid) : '',
      'data-files': draft.files.map(f => f.local + ':' + f.state).join(' ') }, plus, box, button)]
}

// Photos and files on a message to his own Hermes (Patrick, 2 Nov: "no way to add files or photos or
// anything add a + button like Claude and Hermes have"). The + offers the camera, his photos and his
// files; each is uploaded to the core as soon as it is picked, shows above the box while it goes, and
// can be taken off before sending. The message names them, and his Hermes gets them in its own folders
// (own_connector.py save_attachment): the first photo as the turn's image, every other file as an
// @file: reference, as Hermes Desktop attaches them. Up to 10 a message, 20 MB each.
const ATTACH_MAX = 20 * 1024 * 1024
let localFile = 0

function attachSheet(gw, draft, status) {
  closeSheet()
  const pick = (accept, capture) => {
    closeSheet()
    const input = el('input', { type: 'file', multiple: capture ? null : true, accept: accept || null,
      capture: capture || null, hidden: true })
    input.addEventListener('change', () => {
      for (const file of [...input.files]) addFile(gw, draft, file, status)
      input.remove()
      route()
    })
    document.body.append(input)
    input.click()
  }
  const back = el('div', { class: 'sheet-back' },
    el('div', { class: 'sheet', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Add photos or files' },
      el('button', { class: 'secondary pick-file', type: 'button', onclick: () => pick('image/*', 'environment') },
        icon('camera'), 'Take a photo'),
      el('button', { class: 'secondary pick-file', type: 'button', onclick: () => pick('image/*') }, icon('image'), 'Photos'),
      el('button', { class: 'secondary pick-file', type: 'button', onclick: () => pick() }, icon('file'), 'Files'),
      el('button', { class: 'secondary', type: 'button', onclick: closeSheet }, 'Close')))
  back.addEventListener('click', e => { if (e.target === back) closeSheet() })
  back.addEventListener('keydown', e => { if (e.key === 'Escape') closeSheet() })
  document.body.append(back)
  back.querySelector('button').focus()
}

async function addFile(gw, draft, file, status) {
  if (draft.files.length >= 10) { status.textContent = 'A message takes up to 10 files.'; return }
  const f = { local: ++localFile, name: file.name || 'photo.jpg', mime: file.type || '', size: file.size, state: 'up' }
  draft.files.push(f)
  draft.key = newKey()
  if (file.size > ATTACH_MAX) {
    Object.assign(f, { state: 'bad', why: 'over 20 MB' })
    return
  }
  const headers = { 'Content-Type': file.type || 'application/octet-stream' }
  if (csrf) headers['X-CSRF-Token'] = csrf
  let r
  try {
    const resp = await fetch(`${API}/own/${gw}/attach?name=${encodeURIComponent(f.name)}`,
      { method: 'POST', headers, credentials: 'same-origin', cache: 'no-store', body: file })
    let out = {}
    try { out = await resp.json() } catch (e) { out = {} }
    r = { status: resp.status, body: out }
  } catch (e) {
    r = { status: 0, body: { error: 'offline' } }
  }
  if (!draft.files.includes(f)) {
    // Taken off while it went up: take it off the core too.
    if (r.status === 200) call('POST', `/own/${gw}/unattach`, { id: r.body.id })
    return
  }
  if (r.status === 200) Object.assign(f, { state: 'ok', id: r.body.id, name: r.body.name, mime: r.body.mime })
  else Object.assign(f, { state: 'bad', why: r.status === 0 ? 'could not reach the core' : said(r.body) })
  draft.key = newKey()
  route()
}

function sizeWords(n) {
  return n >= 1024 * 1024 ? (n / 1024 / 1024).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB'
}

function attachStrip(gw, draft, status) {
  if (!draft.files.length) return null
  return el('div', { class: 'files', role: 'list', 'aria-label': 'Files on this message' }, draft.files.map(f =>
    el('div', { class: 'file ' + f.state, role: 'listitem' },
      icon(/^image\//.test(f.mime) ? 'image' : 'file', 18),
      el('span', { class: 'fname' }, f.name),
      el('span', { class: 'fstate' }, f.state === 'up' ? 'Uploading...' : f.state === 'bad' ? 'Not added: ' + f.why
        : sizeWords(f.size)),
      el('button', { class: 'ib', type: 'button', 'aria-label': 'Take off ' + f.name, onclick: () => {
        draft.files = draft.files.filter(x => x !== f)
        draft.key = newKey()
        if (f.id) call('POST', `/own/${gw}/unattach`, { id: f.id })
        route()
      } }, icon('x', 16)))))
}

// Where a chat is scrolled. Opening a chat, or sending, goes to the newest
// message. A redraw while Patrick reads further up keeps his place, and the
// button over the box takes him to the newest message, saying how many came in
// below. Older messages keep the one he was looking at.
//
// Opening is a new page (a tap that goes somewhere, or Back), never "a chat
// other than the last one drawn": he once left a chat for the list and came
// back to the same chat, which was not "opening" by that test, so it kept the
// list's place, at the top (2 Nov).
const chatPlace = { key: null, seen: null, toEnd: false, keep: null, fresh: 0, last: null }
let newPage = true    // the next route() draws a page Patrick went to, not a redraw of the one he is on
let pageTop = false   // the next show() starts the page at its top

function nearEnd() {
  return innerHeight + scrollY >= document.documentElement.scrollHeight - 120
}

function toNewest() {
  chatPlace.fresh = 0
  chatPlace.seen = chatPlace.last
  scrollTo({ top: document.documentElement.scrollHeight })
  placeJump()
}

// The button over the box: there whenever the newest message is out of sight.
function placeJump() {
  const jump = document.querySelector('#view .chat .jump')
  if (!jump) return
  if (nearEnd()) {
    chatPlace.fresh = 0
    chatPlace.seen = chatPlace.last
  }
  jump.hidden = nearEnd()
  const n = chatPlace.fresh
  jump.replaceChildren(icon('down', 16), n ? (n === 1 ? '1 new message' : `${n} new messages`) : 'Newest')
  jump.setAttribute('aria-label', n ? `Go to the newest message, ${n} new` : 'Go to the newest message')
}

function showChat(key, top, list, dock, items) {
  const opening = chatPlace.key !== key
  const wasNearEnd = !opening && nearEnd()
  const mids = items.map(m => String(m.mid))
  const toEnd = opening || wasNearEnd || chatPlace.toEnd
  if (toEnd) {
    chatPlace.fresh = 0
  } else if (chatPlace.seen !== null) {
    const at = mids.lastIndexOf(chatPlace.seen)
    chatPlace.fresh = at === -1 ? 0 : mids.length - 1 - at
  }
  const keep = chatPlace.keep
  const jump = el('button', { class: 'jump', type: 'button', onclick: toNewest })
  jump.hidden = true
  const fresh = el('div', { class: 'dock' }, jump, dock)
  // While the box has the focus, the messages above it are drawn again and the box stays as it is, so
  // the keyboard stays up and a word being typed is not lost. Patrick, 2 Nov: a message showed "Waiting
  // to send" long after it had gone, because a chat was not read again while its box had the focus,
  // and putting the keyboard away leaves the focus in the box.
  const old = document.querySelector('#view > .chat')
  const box = document.activeElement
  const oldDock = old && chatPlace.key === key && box && box.matches('.dock .composer textarea') && old.contains(box)
    ? box.closest('.dock') : null
  const newBox = fresh.querySelector('.composer textarea')
  const same = k => fresh.querySelector('.composer').dataset[k] === box.closest('.composer').dataset[k]
  if (oldDock && newBox && newBox.value === box.value && same('reply') && same('files')) {
    for (const n of [...old.childNodes]) if (n !== oldDock) n.remove()
    oldDock.before(...[top].flat(Infinity).filter(Boolean), el('div', { class: 'msgs' }, list))
  } else {
    show(el('div', { class: 'chat' }, top, el('div', { class: 'msgs' }, list), fresh))
  }
  chatPlace.key = key
  chatPlace.toEnd = false
  chatPlace.keep = null
  chatPlace.last = mids[mids.length - 1] ?? null
  if (toEnd) chatPlace.seen = chatPlace.last
  if (keep) {
    const n = document.querySelector(`#view .msg[data-mid="${CSS.escape(keep)}"]`)
    if (n) n.scrollIntoView({ block: 'start' })
  } else if (toEnd) {
    scrollTo({ top: document.documentElement.scrollHeight })
  }
  placeJump()
}

// ── full history of his own chats (step 2, plan Part 4) ────────────────────
//
// A page comes from Hermes itself: the core asks that gateway's connector, which
// reads Hermes's state.db and posts it back within about 5 seconds. The core holds
// it in memory only. The app keeps what it fetched only while it is open.

let viewSeq = 0          // each view, from route() and each chat render; a page that comes back late fills only its own
const pickers = {}       // gw -> { q, results, more, extra }
const threads = {}       // gw:sid -> { newest, older, olderMore, note }

// The phone's copy of each chat it has shown, so a chat opens at once from it and
// the page from Hermes then brings it up to date. Patrick, 2 Nov: every open asked
// One again and showed nothing until One answered, because the app kept what it
// fetched only while it stayed open, and Android closes it. The copy is this
// browser's own storage for this app, the newest KEPT_MESSAGES of the KEPT_CHATS
// chats opened last; signing out deletes it.
const KEPT = 'hermes.kept.v1'
const KEPT_CHATS = 30
const KEPT_MESSAGES = 150

function keptAll() {
  try { return JSON.parse(localStorage.getItem(KEPT) || '{}') || {} } catch (e) { return {} }
}

// A thread as threads[] holds it, from the phone's copy, or null.
function kept(key) {
  const k = keptAll()[key]
  return k && k.newest ? { newest: k.newest, older: k.older || [], olderMore: !!k.olderMore, note: '', error: null } : null
}

function keepThread(key, t) {
  if (!t.newest || t.newest.gone) return
  const all = keptAll()
  const shown = (t.newest.messages || t.newest.events || []).length
  const room = Math.max(KEPT_MESSAGES - shown, 0)
  const older = t.older.slice(Math.max(t.older.length - room, 0))
  all[key] = { at: Date.now(), newest: t.newest, older, olderMore: older.length < t.older.length || t.olderMore }
  for (const k of Object.keys(all).sort((a, b) => all[b].at - all[a].at).slice(KEPT_CHATS)) delete all[k]
  // Full storage drops the chats opened longest ago until it fits.
  for (;;) {
    try { localStorage.setItem(KEPT, JSON.stringify(all)); return } catch (e) {
      const oldest = Object.keys(all).sort((a, b) => all[a].at - all[b].at)
      if (oldest.length <= 1) return
      delete all[oldest[0]]
    }
  }
}

function forgetKept() {
  try { localStorage.removeItem(KEPT) } catch (e) {}
}

async function ownPage(path) {
  const sep = path.includes('?') ? '&' : '?'
  let r
  for (let i = 0; i < 3; i++) {
    r = await call('GET', path + sep + 'wait=8')
    if (r.status !== 200 || !r.body.pending) return r
  }
  return r
}

function chatRow(gw, c, ops) {
  const o = ops ? lastOp(ops, c.id) : null
  return listRow({ 'data-chat': c.id, class: c.bot_chat ? 'pinned' : null, onclick: () => go(`/m/hermes/${gw}/${c.id}`) }, {
    title: chatName(c), tags: c.bot_chat ? el('span', { class: 'rec' }, 'Bot Mode') : null,
    lines: [snippet(c.preview), o && inFlight(o) ? opLine(o, GATEWAY[gw]) : null],
    right: at(c.last_at) || when(c.last_at) })
}

async function gatewayView(gw, into = 'view') {
  // In the desktop's chat list it goes with the chat beside it, so it takes that view's number, not a new one.
  if (into === 'view') setTab('bots')
  const seqNow = into === 'view' ? ++viewSeq : viewSeq
  const r = await call('GET', '/own/' + gw)
  if (refused(r, into)) return
  const v = r.body
  const name = GATEWAY[gw]
  const pick = pickers[gw] || (pickers[gw] = { q: '', results: null, more: false, extra: [] })
  const status = el('p', { class: 'warn', role: 'status' })
  const parts = [
    screenBar({ back: into === 'view' ? () => go('/m/bots') : null, backLabel: 'All Thornswood Bots', agent: gw, title: name, heading: true,
      right: gatewayLine(v), live: gatewayLive(v) }),
    offLine(v)
  ]
  if (v.can_send && v.online) {
    parts.push(signInPart(v, status))
    if (v.sign_in_until) {
      parts.push(composer('new:' + gw, 'Start a new chat',
          (text, key) => call('POST', `/own/${gw}/send`, { new: true, text, idempotency_key: key }), status, gw), status)
    }
  } else if (!v.can_send) {
    parts.push(el('p', { class: 'quiet' }, said({ error: 'own_chat_off' })))
  }
  if (v.starting.length) {
    parts.push(el('h2', { class: 'section' }, 'Starting'), v.starting.map(m => el('div', { class: 'msg mine', 'data-status': m.status },
      el('div', { class: 'text' }, m.text),
      el('div', { class: m.status === 'dead' || m.status === 'uncertain' ? 'warn' : 'quiet' },
        STATE[m.status] + (m.reason ? ': ' + m.reason : '')),
      m.made_session_id ? el('button', { class: 'secondary', type: 'button',
        onclick: () => go(`/m/hermes/${gw}/${m.made_session_id}`) }, 'Open it') : null)))
  }
  // The picker: every chat Hermes's own pickers list, newest first, paged, with a title search.
  const search = el('input', { type: 'search', autocomplete: 'off', placeholder: 'Search chat titles',
    'aria-label': 'Search chat titles' })
  search.value = pick.q
  search.addEventListener('input', () => { pick.q = search.value })
  const note = el('p', { class: 'quiet', role: 'status' })
  async function more(before) {
    note.textContent = `Asking ${name}...`
    const q = pick.results ? pick.q.trim() : ''
    const p = await ownPage(`/own/${gw}/chats?` + new URLSearchParams({ ...(before ? { before } : {}), ...(q ? { q } : {}) }))
    if (seqNow !== viewSeq) return
    if (p.status !== 200) { note.textContent = said(p.body); return }
    if (p.body.pending) { note.textContent = `${name} has not answered yet. Try again in a moment.`; return }
    if (before) pick.extra = pick.extra.concat(p.body.chats)
    else { pick.results = p.body.chats; pick.extra = [] }
    pick.more = p.body.more
    route()
  }
  const searching = pick.results !== null
  const shown = (searching ? pick.results : v.chats).concat(pick.extra)
  const hasMore = (searching || pick.extra.length) ? pick.more : v.chats.length >= 50
  parts.push(el('h2', { class: 'section' }, searching ? `Chats with "${pick.q.trim()}" in the title` : 'Chats'),
    el('div', { class: 'search' }, search,
      el('button', { class: 'secondary', type: 'button', onclick: () => {
        if (!pick.q.trim()) { pick.results = null; pick.extra = []; return route() }
        pick.results = []
        more(null)
      } }, 'Search')),
    searching ? el('button', { class: 'secondary', type: 'button', onclick: () => {
      pick.q = ''; pick.results = null; pick.extra = []; route()
    } }, 'Show every chat') : null,
    shown.length ? shown.map(c => chatRow(gw, c, v.ops)) : el('p', { class: 'quiet' }, searching ? 'No chat has that in its title.' : 'No chats yet.'),
    hasMore ? el('button', { class: 'secondary', type: 'button', onclick: () => more(shown[shown.length - 1].last_at) }, 'Older chats') : null,
    note,
    el('button', { class: 'secondary', type: 'button', onclick: () => go(`/m/hidden/${gw}`) }, 'Hidden chats'))
  if (seqNow === viewSeq) showIn(into, parts)
}

// What Hermes holds of one of his own chats: notes to show above it, its
// messages as bubbles, and the Older messages button when there are more.
function historyPart(gw, sid, t, name, feed) {
  if (t.error) return { notes: [el('p', { class: 'warn' }, t.error)], items: [], gone: false }
  if (!t.newest) {
    // Before the first page: the last messages the connector's feed sent, when it sent this chat's.
    const items = (feed || []).map((m, k) => ({ mine: m.role === 'user', who: name, at: m.at, text: m.text, mid: 'feed' + k,
      noReact: 'This is the last copy the feed sent. A reaction can be set once the chat is read from ' + name + '.' }))
    return { notes: [el('p', { class: 'quiet' }, items.length ? `Asking ${name} for the rest of this chat.`
      : `Asking ${name} for this chat. It shows here within half a minute.`)], items, gone: false }
  }
  if (t.newest.gone) {
    return { notes: [el('p', { class: 'warn' }, `This chat is gone from ${name}. Nothing sent here would reach it.`)], items: [], gone: true }
  }
  const seen = new Set()
  const msgs = t.older.concat(t.newest.messages).filter(m => !seen.has(m.mid) && seen.add(m.mid))
  const olderMore = t.older.length ? t.olderMore : t.newest.more
  const notes = []
  if (t.note) notes.push(el('p', { class: 'receipt', role: 'status' }, t.note))
  if (olderMore && msgs.length) {
    const olderNote = el('p', { class: 'warn', role: 'status' })
    notes.push(el('button', { class: 'secondary older', type: 'button', onclick: async () => {
      olderNote.textContent = `Asking ${name}...`
      const p = await ownPage(`/own/${gw}/chats/${encodeURIComponent(sid)}/messages?before=${msgs[0].mid}`)
      if (p.status !== 200 || p.body.pending) {
        olderNote.textContent = p.status === 200 ? `${name} has not answered yet. Try again in a moment.` : said(p.body)
        return
      }
      t.older = p.body.messages.concat(t.older)
      t.olderMore = p.body.more
      keepThread(`own:${gw}:${sid}`, t)
      chatPlace.keep = String(msgs[0].mid)
      route()
    } }, 'Older messages'), olderNote)
  }
  const items = msgs.map(m => ({ mine: m.role === 'user', who: name, at: m.at, text: m.text, mid: m.mid,
    reactions: m.reactions || [] }))
  if (!items.length) notes.push(el('p', { class: 'quiet' }, 'Nothing here yet.'))
  return { notes, items, gone: false }
}

// What Patrick sent from the phone, until the chat itself shows it.
// The live line under a message being answered (Patrick, 2 Nov: "no typing or thinking indicators and no
// like what they are currently doing"; "Those would be the same lines"). It says what the turn is doing now,
// from Hermes's own stream as One's connector passed it on seconds ago: thinking, writing, or a tool by its
// name. Never the words of the reply or what a tool was given.
const TOOL_WORDS = {
  terminal: 'Running a command', process_manage: 'Running a command', read_terminal: 'Reading a command\'s output',
  close_terminal: 'Closing a command', execute_code: 'Running code', read_file: 'Reading a file',
  write_file: 'Writing a file', patch: 'Editing a file', search_files: 'Searching files',
  web_search: 'Searching the web', x_search: 'Searching X', web_extract: 'Reading a web page',
  delegate_task: 'Handing work to a subagent', memory: 'Updating its memory', todo_list: 'Updating its to-do list',
  skill_view: 'Reading a skill', skills_list: 'Looking through its skills', skill_manage: 'Updating a skill',
  session_search: 'Searching past chats', clarify: 'Asking you something', cronjob_manage: 'Setting up a scheduled job',
  vision_analyze: 'Looking at an image', image_generate: 'Making an image', video_analyze: 'Watching a video',
  video_generate: 'Making a video', text_to_speech: 'Making audio', computer_use: 'Using the computer',
  react_to_message: 'Reacting to a message'
}
function doingWords(doing) {
  if (!doing) return 'Starting'
  if (doing.state === 'thinking') return 'Thinking'
  if (doing.state === 'writing') return 'Writing'
  const tool = doing.tool || ''
  return TOOL_WORDS[tool] || (tool.startsWith('browser') ? 'Using the browser' : tool ? `Using ${tool}` : 'Using a tool')
}
// The other side's typing bubble: three moving dots and what it is doing.
function workingItem(name, agent, doing) {
  return el('div', { class: 'msg theirs working', role: 'status', 'aria-live': 'polite' },
    el('div', { class: 'who a-' + agent }, name),
    el('div', { class: 'bub typing' }, el('span', { class: 'dots', 'aria-hidden': 'true' }, el('i'), el('i'), el('i')),
      el('span', { class: 'doing' }, doingWords(doing))))
}
// While a turn runs in the open chat, its line follows every two seconds, reading only this chat's state;
// when the turn ends the chat is drawn again with the reply. One watch at a time, ended by any new page.
let turnWatch = null
function watchTurn(path, seqNow, turnId, name) {
  clearTimeout(turnWatch)
  turnWatch = setTimeout(async () => {
    if (seqNow !== viewSeq || document.visibilityState !== 'visible') return
    const r = await call('GET', path)
    if (seqNow !== viewSeq || r.status !== 200) return
    const m = r.body.mine.find(x => x.id === turnId)
    if (!m || m.status !== 'leased') {
      const typing = document.activeElement && /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)
      if (!typing) return route()
    }
    const line = document.querySelector('#view .msg.working .doing')
    if (line && m) line.textContent = doingWords(m.doing)
    watchTurn(path, seqNow, turnId, name)
  }, 2000)
}

function pendingItems(mine, readAt) {
  return mine.filter(m => m.status !== 'sent' || !readAt || new Date(m.updated_at) > readAt)
    .map(m => ({ mine: true, at: m.created_at, text: m.text, status: m.status, replyTo: m.reply_to, sent: m.files,
      note: STATE[m.status] + ((m.status === 'dead' || m.status === 'uncertain') && m.reason ? ': ' + m.reason : '')
        + (m.status === 'queued' && m.waits ? ': ' + m.waits : ''),
      warn: m.status === 'dead' || m.status === 'uncertain' }))
}

// Patrick's reaction on one message, through the chat's own route (path, with what names the chat).
async function reactTo(path, chat, mid, emoji, status) {
  status.textContent = emoji ? 'Setting your reaction...' : 'Taking your reaction off...'
  const r = await call('POST', path, { ...chat, message_id: mid, emoji })
  if (r.status === 200) { status.textContent = ''; return route() }
  if (r.status === 401) { csrf = null; return signInView(said(r.body)) }
  const d = r.body.detail || {}
  const gw = (path.match(/^\/own\/(me|one)\//) || [])[1]
  status.textContent = r.status === 0 ? 'Could not reach the core. Nothing changed.'
    : (d.state && gw ? stateWords({ gateway: gw, ...d })[1] : said(r.body))
}

// Puts Patrick's wishes (the core's rows) over the reactions a page of Hermes's shows: what he asked
// for shows at once, and Hermes's own copy takes over once a page asked for after it was set shows
// it. One asked for in the same instant may not hold it, so the wish stays. react(mid, emoji) sets
// one; without it, noReact says why not.
function withReactions(items, rows, pageAt, react, noReact) {
  const wishes = new Map((rows || []).map(w => [w.message_id, w]))
  for (const item of items) {
    // A message from the feed's copy has no Hermes id yet: it says so, and takes no reaction.
    if (String(item.mid).startsWith('feed')) { item.react = null; continue }
    let shown = item.reactions || []
    const w = wishes.get(item.mid)
    if (w && (w.status === 'queued' || w.status === 'leased' || (w.status === 'set' && (!pageAt || new Date(w.updated_at) >= pageAt)))) {
      shown = shown.filter(r => r.by !== 'patrick')
      if (w.emoji) shown.push({ emoji: w.emoji, by: 'patrick', pending: w.status !== 'set' })
    }
    if (w && w.status === 'failed') { item.note = 'Your reaction was not set: ' + (w.reason || 'no reason given'); item.warn = true }
    item.reactions = shown
    item.react = react ? emoji => react(item.mid, emoji) : null
    item.noReact = noReact
  }
}

async function ownChatView(gw, sid) {
  setTab('bots')
  const seqNow = ++viewSeq
  const key = `${gw}:${sid}`
  const t = threads[key] || (threads[key] = kept('own:' + key) || { newest: null, older: [], olderMore: false, note: '', error: null })
  const r = await call('GET', `/own/${gw}/${encodeURIComponent(sid)}`)
  if (refused(r)) return
  const v = r.body
  const name = GATEWAY[gw]
  const status = el('p', { class: 'warn', role: 'status' })
  function render() {
    // The phone's copy of what Patrick sent gives way to Hermes's copy once Hermes holds it. Both
    // times are the core's: handed_at is when it asked for the page, and Hermes was read after that.
    const pageAt = t.newest && t.newest.handed_at ? new Date(t.newest.handed_at) : null
    const readAt = pageAt || (v.as_of ? new Date(v.as_of) : null)
    const h = historyPart(gw, sid, t, name, v.history)
    const canReact = v.can_send && v.online && !h.gone && !t.error
    withReactions(h.items, v.reactions, pageAt, canReact ? (mid, emoji) => reactTo(`/own/${gw}/react`, { session_id: sid }, mid, emoji, status) : null,
      `${name} is not answering, so a reaction cannot be set now.`)
    // Reply style A: Hermes's own copy of a reply he sent shows its link back, from the core's record of
    // exactly what was sent (Hermes has no reply link of its own).
    const links = v.mine.filter(m => m.reply_to && m.status === 'sent').map(m => ({ text: m.text.trim(), to: m.reply_to }))
    for (const it of h.items) {
      if (!it.mine) continue
      const k = links.findIndex(l => l.text === String(it.text).trim())
      if (k >= 0) { it.replyTo = links[k].to; links.splice(k, 1) }
    }
    const draftKey = `own:${gw}:${sid}`
    for (const it of h.items) {
      it.reply = canReact ? () => {
        const d = drafts[draftKey] || (drafts[draftKey] = { text: '', key: newKey() })
        d.reply = { mid: it.mid, quote: quoteOf(it.text), who: it.mine ? 'yourself' : name, by: it.mine ? 'patrick' : 'hermes' }
        d.key = newKey()
        route().then(() => { const b = document.querySelector('#view .composer textarea'); if (b) b.focus() })
      } : null
    }
    const items = h.items.concat(pendingItems(v.mine, readAt).map(m => ({ ...m, who: name })))
    // A rename, hide or delete asked for here: how it stands, and once a hide or a delete is done, so it says.
    const o = lastOp(v.ops, sid)
    const ended = o && o.status === 'done' && (o.op === 'delete' || o.op === 'archive') ? o.op : null
    // His Bot Chat is kept as it is, so it offers no Rename, Hide or Delete (the core refuses them too).
    const manage = v.can_manage && v.online && !h.gone && !t.error && !ended && !(o && inFlight(o)) && !v.chat.bot_chat
    const top = [
      chatBar(() => go('/m/hermes/' + gw), 'All chats on ' + name, chatName(v.chat),
        undefined, gw, name,
        manage ? () => chatSheet(gw, sid, v.chat.title, v) : null, gatewayLive(v)),
      ended === 'delete' ? el('p', { class: 'receipt', role: 'status' }, `Deleted from ${name} for good.`) : null,
      ended === 'archive' ? el('p', { class: 'receipt', role: 'status' }, `Hidden from ${name}'s chat list, with every message kept. `,
        el('button', { class: 'secondary', type: 'button', onclick: () => go(`/m/hidden/${gw}`) }, 'Hidden chats')) : null,
      opLine(o, name),
      h.notes,
      offLine(v)
    ]
    const dock = []
    if (ended) {
      // Nothing sent here would reach a chat that is gone or hidden.
    } else if (v.can_send && v.online && !h.gone && !t.error) {
      if (v.sign_in_until) {
        dock.push(signInPart(v, status), chatComposer(draftKey, 'Message to ' + name, 'Send to ' + name,
          (text, k, reply, files) => call('POST', `/own/${gw}/send`, { session_id: sid, text, idempotency_key: k,
            ...(reply ? { reply_to: reply.mid, quote: reply.quote, quote_by: reply.by } : {}),
            ...(files.length ? { attachments: files } : {}) }), status, gw, gw), status)
      } else {
        dock.push(signInPart(v, status), status)
      }
    } else if (!v.can_send) {
      dock.push(el('p', { class: 'quiet' }, said({ error: 'own_chat_off' })))
    }
    // His message being answered now: the other side's live line under it, kept current while it runs.
    const turn = v.mine.find(m => m.status === 'leased')
    const list = turn ? [bubbles(items), workingItem(name, gw, turn.doing)] : bubbles(items)
    if (seqNow === viewSeq) {
      showChat('own:' + key, top, list, dock, items)
      if (turn) watchTurn(`/own/${gw}/${encodeURIComponent(sid)}`, seqNow, turn.id, name)
    }
  }
  render()
  const p = await ownPage(`/own/${gw}/chats/${encodeURIComponent(sid)}/messages`)
  if (seqNow !== viewSeq) return
  if (p.status === 401) { csrf = null; return signInView(said(p.body)) }
  if (p.status !== 200) {
    t.error = p.body.error === 'not_listed' ? `${name} has not listed this chat, so it does not open here.` : said(p.body)
    return render()
  }
  if (p.body.pending) return render()
  if (p.body.continued_as) {
    // Hermes carries a long chat on under a new id when it compresses it: follow on, and say so.
    threads[`${gw}:${p.body.continued_as}`] = { newest: null, older: [], olderMore: false, error: null,
      note: 'Hermes compressed this chat, so it goes on under a new id. This is where it goes on.' }
    history.replaceState(null, '', `/m/hermes/${gw}/${p.body.continued_as}`)
    return route()
  }
  t.newest = p.body
  t.error = null
  keepThread('own:' + key, t)
  render()
}

// ── renaming, hiding and deleting his own chats (redesign step 3) ─────────
//
// Each is a request the core keeps and that gateway's own connector does with
// Hermes's own calls: a rename as `hermes sessions rename` does it, hiding and
// bringing back as Hermes's archive (the whole chat, every message kept), and a
// delete as `hermes sessions delete`, which Hermes refuses while a turn runs.
// Only Hermes and One have them, never a Bot Chat or a room.

const OP_WORDS = {
  rename: ['Renaming', 'Not renamed'],
  archive: ['Hiding this chat', 'Not hidden'],
  unarchive: ['Bringing it back', 'Not brought back'],
  delete: ['Deleting this chat', 'Not deleted']
}

function inFlight(o) { return o.status === 'queued' || o.status === 'leased' }

// The newest request of one chat, from the core's rows (oldest first).
function lastOp(ops, sid) {
  for (let i = ops.length - 1; i >= 0; i--) if (ops[i].session_id === sid) return ops[i]
  return null
}

// How the newest request of a chat stands, while it is waiting or after it failed.
function opLine(o, name) {
  if (!o) return null
  const [doing, failed] = OP_WORDS[o.op]
  if (inFlight(o)) {
    return el('p', { class: 'receipt', role: 'status', 'data-op': o.op },
      `${doing}${o.op === 'rename' ? ` to "${o.title}"` : ''}. ${name} does it the next time it checks in.`)
  }
  if (o.status === 'failed') return el('p', { class: 'warn', role: 'status', 'data-op': o.op }, `${failed}: ${o.reason || 'no reason given'}`)
  return null
}

// One request to the core. The same request again (after no answer) carries the
// same key, so it is one request, never a second.
async function askSession(gw, sid, op, title, keys, note) {
  const k = op + '\n' + (title || '')
  keys[k] = keys[k] || newKey()
  note.textContent = 'Asking...'
  const r = await call('POST', `/own/${gw}/session`, { session_id: sid, op, title: op === 'rename' ? title : null,
    idempotency_key: keys[k] })
  if (r.status === 200) { closeSheet(); return r }
  if (r.status === 401) { csrf = null; closeSheet(); signInView(said(r.body)); return r }
  const d = r.body.detail || {}
  note.textContent = r.status === 0
    ? 'Could not reach the core. Asking again sends the same request, never a second one.'
    : (d.state ? stateWords({ gateway: gw, ...d })[1] : (r.body.message || said(r.body)))
  return r
}

// What deleting removes, in Hermes's own terms (contracts.json "sessions").
function deleteWords(title, name) {
  return [
    el('p', {}, el('strong', {}, `Delete "${title || 'Untitled chat'}" for good?`)),
    el('p', {}, `This deletes the chat from ${name} for good: all its messages, the earlier parts Hermes ` +
      'compressed it from, and the side tasks Hermes ran for it. It cannot be undone.'),
    el('p', { class: 'quiet' }, 'Nothing else changes: no other chat, no Bot and no room. A chat branched off ' +
      'this one is kept. What you sent from the phone stays in the core\'s record. If a turn is still running ' +
      `in it, ${name} refuses and nothing is deleted.`)
  ]
}

// The chat's actions: Rename, Hide and Delete for good, in a sheet like a message's.
function chatSheet(gw, sid, title, v) {
  closeSheet()
  const name = GATEWAY[gw]
  const keys = {}
  const note = el('p', { class: 'warn', role: 'status' })
  const sheet = el('div', { class: 'sheet', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Chat actions' })
  const back = el('div', { class: 'sheet-back' }, sheet)
  const fill = (...nodes) => { sheet.replaceChildren(...nodes.flat().filter(n => n)); const f = sheet.querySelector('input, button'); if (f) f.focus() }
  const done = () => route()
  const box = el('input', { type: 'text', maxlength: '100', autocomplete: 'off', 'aria-label': 'New title' })
  box.value = title || ''
  // A delete needs a passkey sign-in from the last 30 minutes: without one, its prompt opens by itself.
  let signed = !!v.sign_in_until
  async function remove() {
    if (!signed) {
      const s = await passkeySignIn()
      if (s.status !== 200) { note.textContent = said(s.body); return }
      signed = true
    }
    const r = await askSession(gw, sid, 'delete', null, keys, note)
    if (r.status === 200) return done()
    if (r.status === 403 && r.body.error === 'passkey_needed') signed = false
  }
  function confirmDelete() {
    fill(deleteWords(title, name),
      signed ? null : el('p', { class: 'quiet' }, `Deleting needs your ${signInWord()}. Its prompt opens when you press Delete for good.`),
      el('button', { class: 'danger', type: 'button', 'data-confirm': 'delete', onclick: remove }, 'Delete for good'),
      el('button', { class: 'secondary', type: 'button', onclick: closeSheet }, 'Keep it'),
      note)
  }
  fill(el('p', {}, el('strong', {}, 'This chat on ' + name)),
    el('div', { class: 'search' }, box, el('button', { class: 'secondary', type: 'button', onclick: async () => {
      if (!box.value.trim()) { note.textContent = 'Type a title first.'; return }
      if (box.value.trim() === (title || '')) { note.textContent = 'That is its title already.'; return }
      const r = await askSession(gw, sid, 'rename', box.value.trim(), keys, note)
      if (r.status === 200) done()
    } }, 'Rename')),
    el('button', { class: 'secondary', type: 'button', onclick: async () => {
      const r = await askSession(gw, sid, 'archive', null, keys, note)
      if (r.status === 200) done()
    } }, 'Hide this chat'),
    el('p', { class: 'quiet' }, `Hiding takes it off ${name}'s chat list and keeps every message. Hidden chats, on ${name}'s page, brings it back.`),
    el('button', { class: 'danger-quiet', type: 'button', onclick: confirmDelete }, 'Delete for good...'),
    el('button', { class: 'secondary', type: 'button', onclick: closeSheet }, 'Close'),
    note)
  back.addEventListener('click', e => { if (e.target === back) closeSheet() })
  back.addEventListener('keydown', e => { if (e.key === 'Escape') closeSheet() })
  document.body.append(back)
  box.focus()
}

// The chats Hermes hides (archived), newest first, each with Bring back. Never in the chat list.
async function hiddenView(gw) {
  setTab('bots')
  const seqNow = ++viewSeq
  const r = await call('GET', '/own/' + gw)
  if (refused(r)) return
  const v = r.body
  const name = GATEWAY[gw]
  const note = el('p', { class: 'warn', role: 'status' })
  const head = [
    chatBar(() => go('/m/hermes/' + gw), 'All chats on ' + name, 'Hidden chats', undefined, gw, name),
    offLine(v)
  ]
  show(head, el('p', { class: 'quiet' }, `Asking ${name}...`))
  const all = []
  async function page(before) {
    const p = await ownPage(`/own/${gw}/chats/archived` + (before ? '?' + new URLSearchParams({ before }) : ''))
    if (seqNow !== viewSeq) return
    if (p.status === 401) { csrf = null; return signInView(said(p.body)) }
    if (p.status !== 200) { note.textContent = said(p.body); return draw(false) }
    if (p.body.pending) { note.textContent = `${name} has not answered yet. Try again in a moment.`; return draw(false) }
    all.push(...p.body.chats)
    draw(p.body.more)
  }
  function draw(more) {
    if (seqNow !== viewSeq) return
    const keys = {}
    const rows = all.map(c => {
      const o = lastOp(v.ops, c.id)
      const line = el('p', { class: 'warn', role: 'status' })
      return el('div', { class: 'row', 'data-chat': c.id },
        el('div', { class: 't' },
          el('strong', {}, chatName(c)),
          snippet(c.preview),
          opLine(o, name),
          line),
        el('div', { class: 'w' },
          el('span', {}, when(c.last_at)),
          v.can_manage && v.online && !(o && inFlight(o))
            ? el('button', { class: 'secondary', type: 'button', onclick: async () => {
                const a = await askSession(gw, c.id, 'unarchive', null, keys, line)
                if (a.status === 200) { v.ops.push(a.body); draw(more) }
              } }, 'Bring back')
            : null))
    })
    show(head, all.length ? rows : el('p', { class: 'quiet' }, 'No hidden chats.'),
      more ? el('button', { class: 'secondary', type: 'button', onclick: () => page(all[all.length - 1].last_at) }, 'Older hidden chats') : null,
      v.can_manage ? null : el('p', { class: 'quiet' }, said({ error: 'session_manage_off' })),
      note)
  }
  await page(null)
}

async function roomsView(into = 'view') {
  if (into === 'view') setTab('rooms')
  const r = await call('GET', '/chat')
  if (refused(r, into)) return
  const rooms = r.body.rooms || []
  showIn(into, [
    screenBar({ title: 'Rooms', heading: true }),
    rooms.length ? rooms.map(m => listRow({ 'data-room': m.room_id, onclick: () => go('/m/rooms/' + m.room_id) }, {
      agent: null, label: m.name, title: m.name,
      tags: m.unsettled ? el('span', { class: 'rec' }, `${m.unsettled} waiting`) : null,
      lines: m.last ? snippet(m.last.text, 2, sender(m.last.who)) : el('span', { class: 'quiet' }, 'No messages yet.') }))
      : el('p', { class: 'quiet' }, 'No rooms yet.')])
}

// A Bot's Bot Chat or a room: what the feed last sent shows at once, then the
// newest page from Hermes itself takes its place, with older pages behind it.
async function chatView(target, tab) {
  setTab(tab)
  const seqNow = ++viewSeq
  const r = await call('GET', '/chat/' + encodeURIComponent(target))
  if (refused(r)) return
  const v = r.body
  const room = v.kind === 'room'
  const name = room ? v.name : (NAMES[target] || target)
  const key = 'chat:' + v.target
  const t = threads[key] || (threads[key] = kept(key) || { newest: null, older: [], olderMore: false, note: '', error: null })
  const idOf = m => room ? m.seq : m.mid
  const status = el('p', { class: 'warn', role: 'status' })
  function history() {
    if (!t.newest || t.newest.gone) {
      // Before the first page, or a Bot with no Bot Chat yet: what the feed last sent.
      return { notes: [], items: v.history.map((m, k) => ({ mine: m.role === 'user', who: m.who, at: m.at, text: m.text, mid: 'feed' + k,
        noReact: 'This is the last copy the feed sent. A reaction can be set once the chat is read from Hermes.' })), keyed: false }
    }
    const seen = new Set()
    const raw = t.older.concat(room ? t.newest.events : t.newest.messages).filter(m => !seen.has(idOf(m)) && seen.add(idOf(m)))
    const olderMore = t.older.length ? t.olderMore : t.newest.more
    const notes = []
    if (olderMore && raw.length) {
      const olderNote = el('p', { class: 'warn', role: 'status' })
      notes.push(el('button', { class: 'secondary older', type: 'button', onclick: async () => {
        olderNote.textContent = 'Asking for older messages...'
        const p = await ownPage(`/chat/${encodeURIComponent(v.target)}/messages?before=${idOf(raw[0])}`)
        if (p.status !== 200 || p.body.pending) {
          olderNote.textContent = p.status === 200 ? 'No answer yet. Try again in a moment.' : said(p.body)
          return
        }
        t.older = (room ? p.body.events : p.body.messages).concat(t.older)
        t.olderMore = p.body.more
        keepThread(key, t)
        chatPlace.keep = String(idOf(raw[0]))
        route()
      } }, 'Older messages'), olderNote)
    }
    const noRoom = 'A room has no reactions in Hermes, so a message here takes none.'
    const items = raw.map(m => room
      ? { mine: m.kind === 'message.user', who: m.actor || 'someone', at: m.at, text: m.text, mid: m.seq, noReact: noRoom }
      : { mine: m.role === 'user', who: name, agent: name, at: m.at, text: m.text, mid: m.mid, reactions: m.reactions })
    if (!room) {
      withReactions(items, v.reactions, t.newest.handed_at ? new Date(t.newest.handed_at) : null, v.can_send
        ? (mid, emoji) => reactTo(`/chat/${encodeURIComponent(v.target)}/react`, {}, mid, emoji, status) : null,
        said({ error: 'chat_write_off' }))
    }
    return { notes, items, keyed: true }
  }
  function render() {
    const readAt = v.as_of ? new Date(v.as_of) : null
    const h = history()
    const items = h.items.concat(pendingItems(v.mine, readAt))
    const top = [
      chatBar(() => go(room ? '/m/rooms' : '/m/bots'), room ? 'All rooms' : 'All Thornswood Bots', name, undefined,
        room ? null : v.target, name, null, room ? null : botLive(v.live)),
      h.notes
    ]
    if (!items.length) top.push(el('p', { class: 'quiet' }, 'Nothing here yet.'))
    const dock = v.can_send
      ? [chatComposer(target, 'Message to ' + name, 'Send to ' + name,
          (text, k) => call('POST', '/chat/' + encodeURIComponent(target), { text, idempotency_key: k }), status), status]
      : [el('p', { class: 'quiet' }, said({ error: 'chat_write_off' }))]
    // His message a Bot is answering now: the Bot's live line under it, kept current while the turn runs.
    const turn = room ? null : v.mine.find(m => m.status === 'leased')
    const list = turn ? [bubbles(items), workingItem(name, v.target, turn.doing)] : bubbles(items)
    if (seqNow === viewSeq) {
      showChat(key, top, list, dock, items)
      if (turn) watchTurn('/chat/' + encodeURIComponent(v.target), seqNow, turn.id, name)
    }
  }
  render()
  const p = await ownPage(`/chat/${encodeURIComponent(v.target)}/messages`)
  if (seqNow !== viewSeq || p.status !== 200 || p.body.pending) return
  t.newest = p.body
  keepThread(key, t)
  // Drawn again under a box being typed in, which stays as it is (showChat).
  const other = document.activeElement && /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName) &&
    !document.activeElement.matches('.dock .composer textarea')
  if (!other) render()
}

// ── alerts (step 5) ───────────────────────────────────────────────────────
//
// Asked for only on a deliberate tap. A "no" is told to the core, which keeps
// it, and the app never asks again by itself.
//
// The service worker shows the alerts, so it is the one that takes them off:
// the app tells it which cards still need Patrick each time it reads the list,
// and which card was just opened or answered.
//
// Each time the app opens, it checks that the browser's subscription is the
// one the core has (by the SHA-256 of its endpoint; the core never shows the
// endpoint). Installing the app can give the browser a new subscription, and
// the old one then answers 410 (Patrick's phone test, 1 Nov). alertsPlan says
// what to do, and only ever goes on with a yes Patrick already gave: never
// after he turned alerts off, said no, or the phone's permission went.

function alertsSupported() {
  return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window
}

async function toWorker(msg) {
  if (!('serviceWorker' in navigator)) return
  try {
    const reg = await navigator.serviceWorker.getRegistration('/m/')
    const worker = reg && (reg.active || reg.waiting || reg.installing)
    if (worker) worker.postMessage(msg)
  } catch (err) { /* no worker yet, so no alert of this app is showing */ }
}

// state: GET /push. browser: { hash, keyOk } of the browser's own subscription
// (hash null when it has none; keyOk false when it was made with another key).
// 'none': nothing to do. 'send': give the core the browser's subscription.
// 'renew': make the browser a new subscription, then give the core that.
function alertsPlan(state, browser, permission) {
  if (permission !== 'granted' || !state || state.consent !== 'granted' || !state.vapid_public) return 'none'
  if (!state.subscribed && !state.lapsed) return 'none'
  if (!browser.hash || !browser.keyOk) return 'renew'
  if (state.subscribed) return browser.hash === state.endpoint_sha256 ? 'none' : 'send'
  return browser.hash === state.lapsed_endpoint_sha256 ? 'renew' : 'send'
}

async function endpointHash(sub) {
  if (!sub || !sub.endpoint) return null
  const d = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(sub.endpoint))
  return Array.from(new Uint8Array(d), b => b.toString(16).padStart(2, '0')).join('')
}

function madeWith(sub, vapidPublic) {
  const k = sub && sub.options && sub.options.applicationServerKey
  return !k || toB64url(k) === vapidPublic
}

async function checkAlerts() {
  if (!alertsSupported() || Notification.permission !== 'granted') return
  if (Date.now() - alertsChecked < 10 * 60 * 1000) return
  alertsChecked = Date.now()
  const r = await call('GET', '/push')
  if (r.status !== 200) return
  const s = r.body
  let reg, sub
  try {
    reg = await navigator.serviceWorker.ready
    sub = await reg.pushManager.getSubscription()
  } catch (err) { return }
  const plan = alertsPlan(s, { hash: await endpointHash(sub), keyOk: madeWith(sub, s.vapid_public) }, Notification.permission)
  if (plan === 'none') return
  try {
    if (plan === 'renew') {
      if (sub) await sub.unsubscribe()
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: fromB64url(s.vapid_public) })
    }
  } catch (err) {
    alertsNote = 'Alerts stopped reaching this phone and it could not renew them. Tap Turn on alerts.'
    return
  }
  const done = await call('POST', '/push/subscribe', { subscription: sub.toJSON() })
  alertsNote = done.status === 200 ? `Alerts were renewed for this phone at ${when(new Date().toISOString())}.` : said(done.body)
}

// ── installing the app ────────────────────────────────────────────────────
//
// Chrome keeps its install under the three-dot menu, so the app says where it
// is, or offers its own button when Chrome hands it the prompt.

function installed() {
  return window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true
}

function installBar() {
  const bar = document.getElementById('install')
  if (installed() || sessionStorage.getItem('tw-install-later')) { bar.hidden = true; return }
  const later = el('button', { class: 'icon', type: 'button', 'aria-label': 'Not now', onclick: () => {
    sessionStorage.setItem('tw-install-later', '1')
    installBar()
  } }, '\u00d7')
  bar.replaceChildren(installPrompt
    ? el('button', { class: 'primary', type: 'button', onclick: async () => {
      const prompt = installPrompt
      installPrompt = null
      try { await prompt.prompt() } catch (err) { /* Chrome said no; the menu way still works */ }
      installBar()
    } }, 'Install the app')
    : el('p', {}, 'To install the app: Chrome\'s \u22ee menu, then Add to home screen, then Install.'), later)
  bar.hidden = false
}

async function alertsPart(status) {
  const part = el('div', {})
  if (!alertsSupported()) {
    part.append(el('p', { class: 'quiet' }, 'This browser cannot show alerts. Open the app from the home screen icon.'))
    return part
  }
  const r = await call('GET', '/push')
  if (r.status !== 200) { part.append(el('p', { class: 'quiet' }, said(r.body))); return part }
  const s = r.body
  if (alertsNote) part.append(el('p', { class: 'quiet' }, alertsNote))
  if (!installed()) {
    part.append(el('p', { class: 'quiet' }, 'Install the app first, then turn alerts on from the installed app, ' +
      'so they go to the app and not to this browser tab.'))
  }
  if (s.subscribed) {
    part.append(el('p', {}, 'Alerts are on for this phone.'))
    if (!s.sending) part.append(el('p', { class: 'quiet' }, 'The core is not sending alerts yet, so none will come.'))
    part.append(el('button', { class: 'secondary', type: 'button', onclick: async () => {
      try {
        const reg = await navigator.serviceWorker.ready
        const sub = await reg.pushManager.getSubscription()
        if (sub) await sub.unsubscribe()
      } catch (err) { /* the core forgets it below either way */ }
      const done = await call('POST', '/push/unsubscribe')
      if (done.status === 200) return settingsView('Alerts are off.')
      status.textContent = said(done.body)
    } }, 'Turn off alerts'))
    return part
  }
  if (Notification.permission === 'denied' || s.consent === 'denied') {
    part.append(el('p', { class: 'quiet' }, 'Alerts are off because this phone said no. To have them, allow ' +
      'notifications for this app in the phone\'s settings, then tap Turn on alerts.'))
  } else {
    part.append(el('p', { class: 'quiet' }, 'Alerts are off. When on, a card that needs you shows ' +
      '"Hermes needs you" on the lock screen, and nothing more.'))
  }
  if (!s.vapid_public) {
    part.append(el('p', { class: 'quiet' }, 'Alerts are not ready on the core yet.'))
    return part
  }
  part.append(el('button', { class: 'secondary', type: 'button', onclick: async () => {
    status.textContent = ''
    let perm = Notification.permission
    if (perm !== 'granted') {
      try { perm = await Notification.requestPermission() } catch (err) { perm = 'denied' }
    }
    if (perm !== 'granted') {
      const told = await call('POST', '/push/denied')
      return settingsView(told.status === 200 ? 'Alerts stay off. The app will not ask again by itself.' : said(told.body))
    }
    let sub
    try {
      const reg = await navigator.serviceWorker.ready
      const old = await reg.pushManager.getSubscription()
      if (old) await old.unsubscribe()
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: fromB64url(s.vapid_public) })
    } catch (err) {
      status.textContent = 'This phone could not turn alerts on. Nothing changed.'
      return
    }
    const done = await call('POST', '/push/subscribe', { subscription: sub.toJSON() })
    if (done.status !== 200) {
      try { await sub.unsubscribe() } catch (err) { /* nothing was kept */ }
      status.textContent = said(done.body)
      return
    }
    alertsNote = ''
    settingsView('Alerts are on.')
  } }, 'Turn on alerts'))
  return part
}

async function settingsView(note) {
  setTab('')
  const status = el('p', { class: 'quiet', role: 'status' }, note || '')
  const list = el('div', {})
  const r = await call('GET', '/passkeys')
  if (r.status === 401) return signInView(said(r.body))
  if (r.status === 200) {
    list.append(...(r.body.passkeys.length ? r.body.passkeys.map(k => el('div', { class: 'option' },
      el('strong', {}, k.label), el('div', { class: 'quiet' }, `Added ${when(k.created_at)}`)))
      : [el('p', { class: 'quiet' }, 'No passkey yet. Add one so this phone can sign in again.')]))
  }
  const name = el('input', { autocomplete: 'off', placeholder: 'This phone', 'aria-label': 'Passkey name' })
  const alerts = await alertsPart(status)
  // This phone's fingerprint key: kept only while the core still lists it.
  let held = phoneKey()
  if (held && r.status === 200 && !r.body.passkeys.some(k => k.id === held.passkey_id)) { forgetPhoneKey(); held = null }
  const finger = held
    ? [el('p', { class: 'quiet' }, 'On. This phone signs in and answers high-risk cards with your fingerprint.')]
    : (await canUseFingerprint())
      ? [el('p', { class: 'quiet' }, 'Sign in with your fingerprint right here instead of going out to your passkey app. ' +
          'When Android asks where to save it, pick Google Password Manager.'),
        el('button', { class: 'primary', type: 'button', onclick: async () => {
          status.textContent = ''
          const k = await addFingerprint()
          if (k.ok) return settingsView('Fingerprint sign-in is on for this phone.')
          status.textContent = k.message
        } }, 'Use my fingerprint')]
      : null
  show(
    el('h2', {}, 'Alerts'), alerts,
    finger ? [el('h2', {}, 'Fingerprint'), finger] : null,
    el('h2', {}, 'Passkeys'), list, name,
    el('button', { class: 'primary', type: 'button', onclick: async () => {
      status.textContent = ''
      const k = await addPasskey(name.value.trim())
      if (k.ok) return settingsView('Passkey added.')
      status.textContent = k.message
    } }, 'Add a passkey on this phone'),
    el('button', { class: 'secondary', type: 'button', onclick: async () => {
      const done = await call('POST', '/recovery-codes')
      if (done.status !== 200) { status.textContent = said(done.body); return }
      show(el('h2', {}, 'Recovery codes'),
        el('p', {}, 'Write these down and keep them off this phone. Each works once. They are shown only now.'),
        el('pre', { class: 'artifact' }, done.body.codes.join('\n')),
        el('button', { class: 'secondary', type: 'button', onclick: () => go('/m/settings') }, 'Done'))
    } }, 'Make new recovery codes'),
    el('button', { class: 'secondary', type: 'button', onclick: async () => {
      const done = await call('POST', '/pairing-codes', { device_kind: 'desktop', label: 'Desktop' })
      status.textContent = done.status === 200 ? `Desktop pairing code: ${done.body.code} (10 minutes)` : said(done.body)
    } }, 'Pair a Desktop'),
    el('button', { class: 'secondary', type: 'button', onclick: async () => {
      await call('POST', '/sign-out')
      forgetKept()
      csrf = null
      askOnOpen = false
      signInView('Signed out.')
    } }, 'Sign out'),
    status)
}

// ── routing and polling ───────────────────────────────────────────────────

function badge(n) {
  const b = document.getElementById('badge')
  b.hidden = !n
  b.textContent = n ? String(n) : ''
}

function go(path) {
  history.pushState(null, '', path)
  newPage = true
  route()
}

async function route() {
  // Every route is a new view, so a page a chat asked for and gets back after
  // Patrick left it fills nothing (it once redrew a room over the Rooms list).
  viewSeq++
  // A page he went to starts fresh: a list at its top, a chat at its newest
  // message (showChat). A redraw of the page he is on keeps his place.
  if (newPage) {
    newPage = false
    pageTop = true
    chatPlace.key = null
    chatPlace.keep = null
    chatPlace.toEnd = false
  }
  closeSheet()
  if (!csrf) {
    const me = await call('GET', '/me')
    if (me.status !== 200) return signInView(me.status === 0 ? said(me.body) : '')
    const fresh = await call('POST', '/csrf')
    if (fresh.status !== 200) return signInView(said(fresh.body))
    csrf = fresh.body.csrf
  }
  document.getElementById('tabs').hidden = false
  document.getElementById('settings').hidden = false
  checkAlerts()
  let path = location.pathname.replace(/^\/m\/?/, '').replace(/\/$/, '')
  // The tabs' first names, from an old alert or home screen link.
  const FIRST = { 'needs-you': 'decisions', work: 'board' }
  if (FIRST[path]) { path = FIRST[path]; history.replaceState(null, '', '/m/' + path) }
  // On a wide screen the list beside a chat and Decisions on the right are drawn with it.
  const d = desk(path)
  const drawn = [d.pick ? pickView(path) : mainView(path)]
  if (d.list) drawn.push(d.list('list').then(() => markCurrent(path)))
  if (d.side) drawn.push(decisionsView('side'))
  await Promise.all(drawn)
}

// The desktop (Patrick's approved mock-up, its Desktop board): a rail of
// Hermes, One, the six Bots and the tabs on the left; the chat list; the
// conversation; and Decisions on the right. A phone gets none of it: the same
// screens, one at a time, with the tabs at the foot. Which one is decided by
// the window's width alone, in a browser or installed, and redrawn when it
// changes.
const WIDE = matchMedia('(min-width: 1000px)')
const WIDEST = matchMedia('(min-width: 1360px)')
const folded = { list: false, side: false }   // the panes Patrick folded away, for this visit
const LIST_ONLY = /^(bots|rooms|hermes\/(me|one))$/

function listFor(path) {
  const own = path.match(/^(?:hermes|hidden)\/(me|one)(?:\/|$)/)
  if (own) return into => gatewayView(own[1], into)
  if (/^(bots|bramble)(\/|$)/.test(path)) return into => botsView(into)
  if (/^(rooms|room)(\/|$)/.test(path)) return into => roomsView(into)
  return null
}

function desk(path) {
  const wide = WIDE.matches
  const list = wide ? listFor(path) : null
  const side = wide && WIDEST.matches && path !== '' && !/^decisions(\/|$)/.test(path)
  const out = { list: list && !folded.list ? list : null, side: side && !folded.side,
    pick: !!(list && !folded.list && LIST_ONLY.test(path)) }
  document.body.classList.toggle('wide', wide)
  document.body.classList.toggle('with-list', !!out.list)
  document.body.classList.toggle('with-side', out.side)
  for (const id of ['list', 'side']) {
    const on = id === 'list' ? !!out.list : out.side
    document.getElementById(id).hidden = !on
    if (!on) document.getElementById(id).replaceChildren()
  }
  rail(path, { list: !!list, side })
  return out
}

// The middle of the desktop when the list on its left is the page: what to pick there.
function pickView(path) {
  setTab(path.startsWith('rooms') ? 'rooms' : 'bots')
  const own = path.match(/^hermes\/(me|one)$/)
  show(el('div', { class: 'pick' }, own
    ? [avatar(own[1]), el('p', {}, `Pick one of ${GATEWAY[own[1]]}'s chats on the left, or start a new one there.`)]
    : el('p', {}, path === 'rooms' ? 'Pick a room on the left.' : 'Pick Hermes, One or a Bot on the left.')))
}

// The row of what is open in the middle, marked in the list beside it.
function markCurrent(path) {
  const m = path.match(/^hermes\/(?:me|one)\/(.+)$/) || []
  const bot = path.match(/^bots\/(.+)$/) || (path === 'bramble' ? [0, 'bramble'] : [])
  const room = path.match(/^rooms\/(.+)$/) || []
  for (const row of document.querySelectorAll('#list .row')) {
    const on = (m[1] && row.dataset.chat === m[1]) || (bot[1] && row.dataset.bot === bot[1]) ||
      (room[1] && row.dataset.room === room[1])
    if (on) row.setAttribute('aria-current', 'true')
    else row.removeAttribute('aria-current')
  }
}

const RAIL = [['me', '/m/hermes/me', 'Hermes'], ['one', '/m/hermes/one', 'One'],
  ...['bramble', 'rift', 'sprocket', 'echo', 'magpie', 'wisp'].map(b => [b, '/m/bots/' + b, NAMES[b]])]

// The rail's people and its fold buttons. The tabs below the people are the phone's own.
function rail(path, can) {
  const people = document.getElementById('agents')
  const tools = document.getElementById('folds')
  // A phone's tab bar has the four tabs only.
  if (!WIDE.matches) { people.replaceChildren(); tools.replaceChildren(); return }
  if (!people.childElementCount) {
    people.append(...RAIL.map(([id, href, name]) => el('a', { href, class: 'agent', 'data-go': '', 'data-agent': id,
      'aria-label': name, title: name }, avatar(id, name))))
  }
  const open = (path.match(/^(?:hermes|hidden)\/(me|one)/) || path.match(/^bots\/([a-z0-9_-]+)/) || [])[1] ||
    (path === 'bramble' ? 'bramble' : null)
  for (const a of people.children) {
    if (a.dataset.agent === open) a.setAttribute('aria-current', 'page')
    else a.removeAttribute('aria-current')
  }
  tools.replaceChildren(...[
    can.list ? el('button', { class: 'ib', type: 'button', 'aria-pressed': String(!folded.list),
      'aria-label': folded.list ? 'Show the chat list' : 'Hide the chat list', title: 'Chat list',
      onclick: () => fold('list') }, icon('panel')) : null,
    can.side ? el('button', { class: 'ib', type: 'button', 'aria-pressed': String(!folded.side),
      'aria-label': folded.side ? 'Show Decisions' : 'Hide Decisions', title: 'Decisions',
      onclick: () => fold('side') }, icon('dec')) : null].filter(Boolean))
}

function fold(pane) {
  folded[pane] = !folded[pane]
  route()
}

// What the middle shows: on a phone, the whole screen.
function mainView(path) {
  const card = path.match(/^decisions\/([A-Za-z0-9_-]{1,64})$/)
  if (card) return cardView(card[1])
  if (path === 'board') return boardView()
  const bot = path.match(/^bots\/([a-z0-9][a-z0-9_-]{0,63})$/)
  if (bot) return chatView(bot[1], 'bots')
  const room = path.match(/^rooms\/([A-Za-z0-9_-]{1,64})$/)
  if (room) return chatView(room[1], 'rooms')
  if (path === 'room') return chatView('room', 'rooms')
  if (path === 'rooms') return roomsView()
  if (path === 'bramble') return chatView('bramble', 'bots')
  if (path === 'bots') return botsView()
  const own = path.match(/^hermes\/(me|one)(?:\/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127}))?$/)
  if (own) return own[2] ? ownChatView(own[1], own[2]) : gatewayView(own[1])
  const hidden = path.match(/^hidden\/(me|one)$/)
  if (hidden) return hiddenView(hidden[1])
  if (path === 'settings') return settingsView()
  return decisionsView()
}

async function poll() {
  if (!csrf || document.visibilityState !== 'visible' || answering || document.querySelector('.sheet-back')) return
  const path = location.pathname
  // A chat view reads itself again. Its messages are drawn again under the box Patrick is typing in, and
  // the box stays as it is (showChat). Any other box with focus or words in it (on the desktop, the chat
  // list's New chat box is on the page too) holds the redraw.
  const chatBox = '#view .dock .composer textarea'
  const writing = [...document.querySelectorAll('textarea')].some(t => t.value && !t.matches(chatBox))
  if (/^\/m\/(bramble$|room$|rooms\/|bots\/|hermes\/)/.test(path)) {
    const other = document.activeElement && /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName) &&
      !document.activeElement.matches(chatBox)
    if (!writing && !other) route()
    return
  }
  const r = await call('GET', '/changes?since=' + seq)
  if (r.status !== 200 || r.body.seq === seq) return
  seq = r.body.seq
  const card = path.match(/^\/m\/decisions\/([A-Za-z0-9_-]{1,64})$/)
  if (card && r.body.changed.includes(card[1]) && !document.querySelector('[data-confirm]')) return route()
  // Decisions on the desktop's right follow the list too, whatever is in the middle.
  if (/^\/m\/(decisions)?$/.test(path) || !document.getElementById('side').hidden) return route()
  // Elsewhere, the badge still follows the list.
  const list = await call('GET', '/cards')
  if (list.status === 200) badge(list.body.cards.length)
}

document.addEventListener('click', event => {
  const a = event.target.closest('a[data-tab], a[data-go]')
  if (a) { event.preventDefault(); go(a.getAttribute('href')) }
})
// Turning a phone, or widening a window on One, redraws the page in the layout that fits it.
for (const q of [WIDE, WIDEST]) q.addEventListener('change', () => { if (csrf) route() })
document.getElementById('settings').addEventListener('click', () => go('/m/settings'))
window.addEventListener('popstate', () => { newPage = true; route() })
// The app keeps its own place on every page, so the browser does not put back an old one.
if ('scrollRestoration' in history) history.scrollRestoration = 'manual'
addEventListener('scroll', placeJump, { passive: true })
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') { poll(); if (csrf) checkAlerts() }
})
window.addEventListener('beforeinstallprompt', event => {
  event.preventDefault()
  installPrompt = event
  installBar()
})
window.addEventListener('appinstalled', () => { installPrompt = null; installBar() })
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/m/sw.js', { scope: '/m/' }).catch(() => {})
  // A tapped alert, with the app already open: the worker says which card.
  navigator.serviceWorker.addEventListener('message', event => {
    const p = event.data && event.data.go
    if (typeof p === 'string' && /^\/m\/(decisions|decisions\/[A-Za-z0-9_-]{1,64}|needs-you)$/.test(p)) go(p)
  })
}
pollTimer = setInterval(poll, POLL_MS)
installBar()
route()
