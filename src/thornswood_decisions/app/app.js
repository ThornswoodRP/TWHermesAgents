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
  panel: 'M3 4h18v16H3zM9 4v16', info: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v6M12 7h.01', file: 'M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9zM14 3v6h6',
  image: 'M4 5h16v14H4zM4 16l5-5 4 4 3-3 4 4M15 9h.01', camera: 'M4 8h4l2-3h4l2 3h4v11H4zM12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8z', dec: 'M9 11l3 3 8-8M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9',
  mic: 'M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3zM5 11a7 7 0 0 0 14 0M12 18v3',
  talk: 'M4 10v4M8 7v10M12 4v16M16 7v10M20 10v4',
  // The kinds of link a message can hold (linkKind), each with its own mark, as the Claude apps show them.
  thread: 'M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z',
  session: 'M4 5h16v14H4zM7 9l3 3-3 3M12 15h5',
  artifact: 'M4 4h16v16H4zM4 9h16M9 9v11',
  pr: 'M6 3v12M6 15a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 9v3a3 3 0 0 1-3 3H9',
  issue: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 13a1 1 0 1 0 0-2 1 1 0 0 0 0 2z',
  web: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18',
  phone: 'M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2z'
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
  // What signs in is this phone's fingerprint once it has one (signInWord), never "passkey" there.
  sign_in_again: () => `This needs a fresh sign-in with your ${signInWord()}.`,
  decision_write_off: 'Answering is off for now. The report page is still where you answer.',
  chat_write_off: 'Chat from the phone is off for now.',
  own_chat_off: 'Chat with your own Hermes from the phone is off for now.',
  session_manage_off: 'Renaming, hiding and deleting chats from the phone is off for now.',
  bot_chat_kept: 'Your Bot Chat is kept as it is: Bot Mode and every Bot\'s message to you find it by its name, so it is not renamed, hidden or deleted.',
  removed_passkey: 'That passkey was removed here, so your phone was told to drop it. Sign in again and pick your other passkey.',
  passkey_needed: () => `This runs with full power on your own Hermes, so it needs a sign-in with your ${signInWord()} from the last 30 minutes.`,
  one_off: 'One is off',
  no_gateway: 'There is no such gateway.',
  stale_revision: 'This card changed since you opened it. Read it again before you answer.',
  already_answered: 'This card was already answered.',
  expired: 'This card ran out. Bramble opens a new one if it still matters.',
  no_chat: 'There is no such chat.',
  setup_expired: 'Setting up this phone ran out: a pairing gives 5 minutes to add the first passkey. Get a new code, pair again and add the passkey straight after.',
  passkey_unavailable: 'Passkeys are not set up on the core yet.',
  no_origin: 'The core does not know its address yet, so passkeys cannot work.',
  offline: 'Offline. Nothing was sent. Turn Tailscale on and try again.',
  voice_off: 'Dictation is not running on the VM.',
  voice_slow: 'Whisper on the VM took too long with that. Try a shorter recording.',
}

function said(body) {
  const w = body && SAID[body.error]
  return (typeof w === 'function' ? w(body) : w) || (body && body.message) || 'Something went wrong.'
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

// The core no longer lists this phone's key: forgotten here, said once on the Decisions list, and the
// fingerprint offered there again even if he had put it off.
const LOST_KEY = "This phone's fingerprint sign-in is no longer on the core, so your passkey signs in. Add the fingerprint again below."
// The core has the key but it was not made by the phone itself (8 Nov: it was saved in Bitwarden, so
// the fingerprint button opened Bitwarden). Same way on, with the reason.
const KEY_IN_APP = "This phone's fingerprint key was saved in your passkey app, so it opened that app. Your passkey signs in now. " +
  'Add the fingerprint again below and pick Google Password Manager when Android asks where to save it.'
function lostPhoneKey(why = LOST_KEY) {
  forgetPhoneKey()
  try { localStorage.removeItem(FINGERPRINT_LATER) } catch (e) {}
  flash = why
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
  const done = await call('POST', '/passkeys/register',
    { credential: credentialJSON(cred), label: 'Fingerprint on this phone', on_phone: true })
  if (done.status !== 200) {
    refusedKey(o.body, cred, done)
    return { ok: false, message: said(done.body) }
  }
  if (done.body.phone_key === false) {
    // Saved in a passkey app, not by the phone itself: the core keeps it as a passkey, so the key the app
    // now holds signs in, but it is not the fingerprint key, which would open that app every time.
    fingerprintTried = true
    const app = done.body.maker || 'your passkey app'
    return { ok: false, message: `That key was saved in ${app}, so it is kept as a passkey and signs in through ${app}. ` +
      'The fingerprint sign-in was not added: tap Use my fingerprint again and pick Google Password Manager when Android asks where to save it.' }
  }
  if (!keepPhoneKey({ credential_id: done.body.credential_id, passkey_id: done.body.passkey_id })) {
    return { ok: false, message: 'This browser would not keep the fingerprint\'s name, so the app cannot ask for it. Remove it in Settings.' }
  }
  return { ok: true }
}

async function passkeySignIn() {
  const key = phoneKey()
  let o = await call('POST', '/passkeys/sign-in/options', key ? { phone_key: key.credential_id } : {})
  if (key && ((o.status === 404 && o.body.error === 'no_phone_key') || (o.status === 409 && o.body.error === 'phone_key_in_app'))) {
    // The core no longer has this phone's key (it or the phone was removed), or the key was saved in a
    // passkey app: the passkey is the way in, and he is told so, with the fingerprint offered again,
    // never switched to the passkey unsaid.
    lostPhoneKey(o.status === 409 ? KEY_IN_APP : LOST_KEY)
    o = await call('POST', '/passkeys/sign-in/options', {})
  }
  if (o.status !== 200) return o
  const word = key && o.body.hints ? 'fingerprint' : 'passkey'
  let cred
  try {
    cred = await navigator.credentials.get({ publicKey: requestOptions(o.body) })
  } catch (err) {
    // The browser or the passkey app ended it, so the core would hear nothing (9 Nov: "my key failed" left
    // no record on the core). It is told, by this request's challenge, with the browser's own words.
    const name = (err && err.name) || 'Error'
    await call('POST', '/passkeys/sign-in/failed',
      { challenge: o.body.challenge, name, message: String((err && err.message) || '').slice(0, 160) })
    // 9 Nov: his phone held none of the keys the request named, so every sign-in ended here with nothing
    // said about the way back in.
    const none = (o.body.allowCredentials || []).length && !o.body.hints
      ? ' If your phone showed no passkey for Hermes, it holds none the core takes: sign in with a recovery code ' +
        'under Other ways in, then add a passkey.'
      : ''
    return { status: -1, body: { message: `The ${word} did not sign in (${name}). Nothing changed.${none}` } }
  }
  const r = await call('POST', '/passkeys/sign-in', { credential: credentialJSON(cred) })
  if (r.status === 200) {
    csrf = r.body.csrf
    passkeyAppAccepts(r.body.accepted)
  } else if (r.status === 401 && r.body.error === 'removed_passkey') {
    passkeyAppDrops(r.body.detail)
  }
  return r
}

// His passkey app keeps a key after the core removed it (a fingerprint key replaced, a passkey or a phone
// removed in Settings), and offers it with the live ones; on 9 Nov the one he picked was refused as "not one
// of Patrick's". So the app tells the passkey app (WebAuthn's Signal API, where the browser has it): after a
// sign-in, which keys the core takes, and after a refusal, that the key it offered was removed here. Only
// the core's own word moves it, and a key the core never had is never named. Nothing waits on it.
function passkeyAppAccepts(a) {
  if (!a || !a.user_id || !Array.isArray(a.credential_ids) || !window.PublicKeyCredential ||
      typeof PublicKeyCredential.signalAllAcceptedCredentials !== 'function') return
  PublicKeyCredential.signalAllAcceptedCredentials({ rpId: a.rp_id, userId: a.user_id,
    allAcceptedCredentialIds: a.credential_ids }).catch(() => {})
}
function passkeyAppDrops(d) {
  if (!d || !d.credential_id || !window.PublicKeyCredential ||
      typeof PublicKeyCredential.signalUnknownCredential !== 'function') return
  PublicKeyCredential.signalUnknownCredential({ rpId: d.rp_id, credentialId: d.credential_id }).catch(() => {})
}

// A key the phone just made and the core then refused (a sign-in run out) is kept by nobody here, but the
// passkey app keeps it and offers it at the next sign-in, where the core refuses it as not one of Patrick's. The app made it and the core never held it, so it is named for
// the passkey app to drop: only on the core's own refusal, never when no answer came (the core may have
// kept it) nor for a key it already holds.
function refusedKey(options, cred, done) {
  const rpId = options && options.rp && options.rp.id
  if (!(done.status >= 400 && done.status < 500) || (done.body && done.body.error === 'passkey_exists')) return
  if (rpId && cred && cred.id) passkeyAppDrops({ rp_id: rpId, credential_id: cred.id })
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
  if (done.status !== 200) refusedKey(o.body, cred, done)
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

// Whether this phone already tried the fingerprint and its passkey app took the key (9 Nov: Android saved
// both of Patrick's tries in Bitwarden, and the offer stayed though he had done what it asked). Asked of
// the core once a page load; a try made here sets it at once.
let fingerprintTried = null

async function triedFingerprint() {
  if (fingerprintTried !== null) return fingerprintTried
  const [me, keys] = await Promise.all([call('GET', '/me'), call('GET', '/passkeys')])
  if (me.status !== 200 || keys.status !== 200) return false
  return (fingerprintTried = keys.body.passkeys.some(k => k.fingerprint_try && k.device_id === me.body.device_id))
}

async function fingerprintOffer() {
  let later = false
  try { later = localStorage.getItem(FINGERPRINT_LATER) === '1' } catch (e) {}
  if (later || phoneKey() || !(await canUseFingerprint()) || await triedFingerprint()) return null
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
  const alertLine = alertTimes(c)
  if (alertLine) parts.push(alertLine)
  if (c.superseded_by) {
    parts.push(el('button', { class: 'secondary', type: 'button', onclick: () => go('/m/decisions/' + c.superseded_by) },
      'This card was replaced. Open the new one'))
  }
  parts.push(status)
  parts.push(el('button', { class: 'secondary', type: 'button', onclick: () => go('/m/decisions') }, 'Back'))
  show(el('div', { class: 'card' }, parts))
}

// Where a card's alert spent its time (Patrick, 2 Nov: a card reached his phone minutes after it was
// stored): when the card was stored, when the core's worker handed its alert to the push service, and
// when this phone got it (the service worker keeps that time on the phone, sw.js). To the second, so a
// late alert says which hop was slow without anyone being asked to measure it.
//
// The phone's time goes beside the alert it belongs to: a revised card is alerted again, so the time
// an earlier alert reached the phone is said as that (Patrick's screenshot, 2 Nov 4:01 PM: "card
// 4:01:27 PM, not sent (queued), on this phone 4:01:21 PM"). While the alert is still on its way the
// line follows it, read again from the core, so a card opened in the seconds before the worker's next
// round does not keep saying "not sent" after it went.
const ALERT_FOLLOW_MS = 3000
const ALERT_FOLLOW_TRIES = 40
function alertTimes(c) {
  if (!c.alert) return null
  const t = iso => new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' })
  const line = el('p', { class: 'quiet alert-times', 'data-alert': c.alert.status })
  let alert = c.alert
  let seen = null
  const say = () => {
    line.dataset.alert = alert.status
    const mine = seen && (seen.attention_revision == null || seen.attention_revision === alert.attention_revision)
    line.textContent = 'Alert: card ' + t(alert.queued_at) +
      (alert.sent_at ? ', sent ' + t(alert.sent_at) : ', not sent (' + alert.status + ')') +
      (seen ? (mine ? ', on this phone ' : ', an earlier alert on this phone ') + t(seen.shown_at) : '')
  }
  const look = () => 'caches' in window
    ? caches.open('tw-alerts').then(k => k.match('/m/alerts/' + c.id)).then(r => r ? r.json() : null)
      .then(j => { if (j && j.shown_at) seen = j }).catch(() => {})
    : Promise.resolve()
  say()
  look().then(say)
  const follow = async tries => {
    if (tries <= 0 || !(alert.status === 'queued' || alert.status === 'leased')) return
    await new Promise(r => setTimeout(r, ALERT_FOLLOW_MS))
    if (!line.isConnected) return
    const r = await call('GET', '/cards/' + encodeURIComponent(c.id))
    if (r.status !== 200 || !r.body.alert || !line.isConnected) return
    alert = r.body.alert
    await look()
    say()
    follow(tries - 1)
  }
  follow(ALERT_FOLLOW_TRIES)
  return line
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
  // Answered from a Bot Chat: back to that chat, where the card has gone.
  const back = answerBack
  answerBack = null
  if (back) badgeNow()
  history.replaceState(null, '', back || '/m/decisions')
  return route()
}
let answerBack = null  // the Bot Chat a card was answered from

// The Board, live (Patrick, 2 Nov: "only what each is currently doing"): one row for each turn a Bot is
// running now, in the Bots list's order, with the session's title, what the turn is doing and for how long.
// A Bot with nothing running has no row, and nothing that ended stays. The VM sends it every 2 seconds while
// it changes and the Board reads it again every 3 seconds while it is open. Its words are the Bot Chat's
// live line (doingWords), and a row opens that Bot's chat. When the VM has not sent it for a minute and a half, the Board says so instead of
// showing what may have ended.
const LIVE_MS = 3000

// How long a turn has run: under a minute, then minutes, then hours and minutes.
function forHowLong(iso) {
  const t = new Date(iso)
  if (isNaN(t)) return ''
  const s = Math.max(0, Math.round((Date.now() - t) / 1000))
  if (s < 60) return 'just now'
  const m = Math.floor(s / 60)
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`
}

async function boardView() {
  setTab('board')
  const r = await call('GET', '/work')
  if (refused(r)) return
  const work = r.body.work
  const bar = screenBar({ title: 'Board', heading: true })
  if (!work || !Array.isArray(work.doing)) {
    return show(bar, el('p', { class: 'quiet' }, 'The Board has not heard from the Bots yet.'))
  }
  if (!r.body.live) {
    return show(bar, el('p', { class: 'warn', role: 'status' },
      `The Board has not heard from the Bots since ${when(r.body.received_at)}, so it cannot say what they are doing.`))
  }
  const rank = b => (ORDER.indexOf(b) + 1) || ORDER.length + 1
  const rows = [...work.doing].sort((a, b) => rank(a.bot) - rank(b.bot) || (a.since < b.since ? -1 : 1))
  // A row opens the very session that Bot is working in (Patrick, 9 Nov: "clicking a working agent should
  // open that exact session not like a new one or their list of sessions").
  show(bar, rows.length ? rows.map(d => listRow({ class: 'doing', 'data-bot': d.bot, onclick: () => go(sessionPath(d)) }, {
    agent: d.bot, title: NAMES[d.bot] || d.bot,
    lines: [d.title ? el('span', { class: 'quiet' }, d.title) : null,
      el('span', { class: 'what', 'data-doing': d.state }, doingWords(d))],
    right: forHowLong(d.since) }))
    : el('p', { class: 'quiet' }, 'No Bot is working right now.'))
}

function sessionPath(d) {
  return d.session_id ? `/m/bots/${d.bot}/s/${encodeURIComponent(d.session_id)}` : '/m/bots/' + d.bot
}

// One session of a Bot, the one a Board row names, as a chat like any other (Patrick, 9 Nov: "it should
// bring up that session like it would if I had went their myself with the normal view with the option to
// message and all the voice features, it shouldn't be like a separate read only version"): its messages
// from Hermes's own store, the Bot's turn there while it runs, and the box, with voice. What he sends goes
// into that session through the Bot's gateway (connector.py to_session).
function botSessionView(bot, sid) {
  return chatView(bot, 'board', sid)
}

// ── Tasks: subagents, background commands and scheduled jobs (Patrick, 3 Nov) ──
//
// "I wanted to see the subagents, jobs, and background tasks on the Mobile and Desktop app." Every Hermes
// keeps these per profile, so this one list holds all of them at once: Hermes, One and the six Bots, each
// row with its agent's avatar, and a row of names to look at one of them only. What each part says comes
// from that Hermes itself, read the way Hermes Desktop reads it (contracts.json "tasks"); a Hermes the core
// has not heard from lately, or whose part could not be read, says so in one line at the foot, and none of
// its rows are shown as though they were current.
let tasksOnly = null   // the agent the list is narrowed to, for this visit; null is all of them

function agentName(id) {
  return GATEWAY[id] || NAMES[id] || id
}

function epochIso(sec) {
  return typeof sec === 'number' ? new Date(sec * 1000).toISOString() : null
}

// Where a row's chat opens: one of Hermes's or One's chats, or a Bot's own chat.
function taskChat(agent, chat) {
  if (GATEWAY[agent]) return chat ? '/m/hermes/' + agent + '/' + encodeURIComponent(chat) : '/m/hermes/' + agent
  return '/m/bots/' + agent
}

// When a job runs next, short enough for the pill on a phone: the time today, the day and time this week,
// else the date.
function nextWords(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d)) return ''
  if (d.toDateString() === new Date().toDateString()) return at(iso)
  if (d - Date.now() < 6 * 864e5) return d.toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' })
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

// A job's state on the right: green only while it runs, as the other lists' dots; red when Hermes stopped it.
function jobLine(j) {
  if (j.running) return liveLine({ cls: 'on', words: 'Running now' })
  if (j.state === 'error') return el('span', { class: 'rec off' }, 'Stopped')
  if (j.state === 'paused') return el('span', { class: 'rec' }, 'Paused')
  if (j.state === 'completed') return el('span', { class: 'rec' }, 'Done')
  return el('span', { class: 'rec' }, j.next ? 'Next ' + nextWords(j.next) : 'Scheduled')
}

function taskRows(agents) {
  const subs = [], procs = [], jobs = [], unheard = [], silent = []
  for (const a of agents) {
    const name = agentName(a.id)
    if (!a.heard) { silent.push(name + (a.since ? ' (since ' + at(a.since) + ')' : '')); continue }
    if (a.why) { unheard.push(`${name}: ${a.why}`); continue }
    if (a.subagents === null || a.processes === null) unheard.push(`${name}: ${a.live_why || 'its gateway could not be read'}`)
    if (a.jobs === null) unheard.push(`${name}: ${a.jobs_why || 'its scheduled jobs could not be read'}`)
    for (const r of a.subagents || []) subs.push({ agent: a.id, ...r })
    for (const r of a.processes || []) procs.push({ agent: a.id, ...r })
    for (const r of a.jobs || []) jobs.push({ agent: a.id, ...r })
  }
  // The ones not heard from, in one sentence, before what the others could not read.
  if (silent.length) unheard.unshift('Not heard from: ' + andList(silent))
  return { subs, procs, jobs, unheard }
}

async function tasksView() {
  setTab('tasks')
  const r = await call('GET', '/tasks')
  if (refused(r)) return
  const agents = r.body.agents || []
  const all = taskRows(agents)
  const busy = new Set([...all.subs, ...all.procs, ...all.jobs].map(t => t.agent))
  if (tasksOnly && !busy.has(tasksOnly)) tasksOnly = null
  const mine = t => !tasksOnly || t.agent === tasksOnly
  const subs = all.subs.filter(mine)
  const procs = all.procs.filter(mine)
  const jobs = all.jobs.filter(mine)
  const name = t => agentName(t.agent)
  const where = t => t.chat_title ? `${name(t)} · ${t.chat_title}` : name(t)
  const pick = busy.size > 1 ? el('div', { class: 'picks', role: 'group', 'aria-label': 'Whose tasks' },
    [null, ...agents.map(a => a.id).filter(id => busy.has(id))].map(id => el('button', {
      type: 'button', class: 'pick-one', 'aria-pressed': String(tasksOnly === id),
      onclick: () => { tasksOnly = id; tasksView() } }, id ? agentName(id) : 'All'))) : null
  const live = (cls, words) => liveLine({ cls, words })
  show(screenBar({ title: 'Tasks', heading: true }), pick,
    subs.length ? [el('h2', { class: 'section' }, 'Subagents'), subs.map(t => listRow(
      { 'data-task': 'subagent', onclick: () => go(taskChat(t.agent, t.chat)) }, {
        agent: t.agent, title: t.goal || 'A subagent', lines: [el('span', { class: 'quiet' }, where(t)),
          t.last_tool ? el('span', { class: 'what' }, toolWords(t.last_tool)) : null],
        right: [t.status === 'running' || !t.status
          ? live('on', t.background ? 'In the background' : 'Running')
          : el('span', { class: t.status === 'failed' ? 'rec off' : 'rec' }, statusWords(t.status)),
        forHowLong(epochIso(t.started))] }))] : null,
    procs.length ? [el('h2', { class: 'section' }, 'Background tasks'), procs.map(t => listRow(
      { 'data-task': 'process', onclick: () => go(taskChat(t.agent, t.chat)) }, {
        agent: t.agent, title: el('code', { class: 'cmd' }, t.command || 'A command'), lines: [el('span', { class: 'quiet' }, where(t))],
        right: t.status === 'running'
          ? [live('on', 'Running'), forHowLong(epochIso(t.started))]
          : [el('span', { class: t.exit_code === 0 ? 'rec' : 'rec off' },
            t.exit_code === 0 ? 'Done' : t.exit_code === null ? 'Stopped' : `Failed (exit ${t.exit_code})`),
          at(epochIso(t.ended))] }))] : null,
    jobs.length ? [el('h2', { class: 'section' }, 'Scheduled jobs'), jobs.map(j => {
      const failed = j.last_status && j.last_status !== 'ok'
      return listRow({ 'data-task': 'job', onclick: () => go(taskChat(j.agent, null)) }, {
        agent: j.agent, title: j.name || 'A job', lines: [el('span', { class: 'quiet' }, `${name(j)} · ${j.schedule || ''}`),
          failed ? el('span', { class: 'warn' }, `Last run ${j.last_status === 'delivery_failed' ? 'was not delivered' : 'failed'}` +
            (j.last_error ? `: ${j.last_error}` : '')) : null],
        right: [jobLine(j)] })
    })] : null,
    subs.length || procs.length || jobs.length ? null
      : el('p', { class: 'quiet' }, tasksOnly ? `${agentName(tasksOnly)} has nothing running and no scheduled jobs.`
        : 'Nothing is running, and there are no scheduled jobs.'),
    all.unheard.length ? el('p', { class: 'warn', role: 'status' }, all.unheard.join('. ') + '.') : null)
}

function andList(names) {
  return names.length < 2 ? names.join('') : names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1]
}

function statusWords(s) {
  return { running: 'Running', completed: 'Done', failed: 'Failed', interrupted: 'Stopped' }[s] || 'Running'
}

// A subagent's last tool in the words the line under a message uses for one (doingWords).
function toolWords(tool) {
  return doingWords({ state: 'tool', tool })
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
  const gateways = own.status === 200 ? ['me', 'one'].map(gw => own.body.gateways.find(g => g.gateway === gw)).filter(Boolean) : []
  // The one last written in first, as a messenger lists its chats (Patrick, 9 Nov: Wisp had the newest chat
  // and stayed at the bottom). His own message counts: the core gives a Bot's last line as the newer of the
  // Bot Chat and what he sent it. Where nothing was said, Hermes, then One, then the six Bots (his mock-up
  // answer, 1 Nov), which the sort keeps for equal times.
  const when = at => { const t = Date.parse(at || ''); return Number.isNaN(t) ? -Infinity : t }
  const rows = [...gateways.map(g => ({ g, at: when(g.last && g.last.last_at) })),
    ...bots.map(b => ({ b, at: when(b.last && b.last.at) }))].sort((x, y) => y.at - x.at)
  showIn(into, [
    screenBar({ title: 'Thornswood Bots', heading: true }),
    own.status === 200 ? null : el('p', { class: 'warn' }, said(own.body)),
    rows.map(({ g, b }) => g ? listRow({ 'data-gateway': g.gateway, onclick: () => go('/m/hermes/' + g.gateway) }, {
      agent: g.gateway, title: GATEWAY[g.gateway], live: gatewayLive(g),
      lines: [el('span', { class: 'what' }, GATEWAY_WHAT[g.gateway]),
        g.last ? snippet(g.last.preview, 2, el('b', { class: 'sender' }, chatName(g.last) + ': '))
          : el('span', { class: 'quiet' }, g.as_of ? 'No chats yet.' : 'Its chats show here once it is turned on.')],
      right: [gatewayLine(g), g.unsettled ? el('span', { class: 'rec' }, `${g.unsettled} waiting`) : null] })
    : listRow({ 'data-bot': b.bot, onclick: () => go('/m/bots/' + b.bot) }, {
      agent: b.bot, title: NAMES[b.bot] || b.bot, live: botLive(b.live),
      lines: [WHAT[b.bot] ? el('span', { class: 'what' }, WHAT[b.bot]) : null,
        b.last ? snippet(b.last.text, 2, b.last.role === 'user' ? el('b', { class: 'sender' }, 'You: ') : null)
          : el('span', { class: 'quiet' }, 'No messages yet.')],
      right: [liveLine(botLive(b.live)), b.unsettled ? el('span', { class: 'rec' }, `${b.unsettled} waiting`) : null] }))])
}

// Sending to his own Hermes needs a passkey sign-in from the last 30 minutes
// (Patrick, 1 Nov). This says until when, or offers that sign-in. A new chat is
// made with full power; Hermes keeps a chat's own approval setting for good, so
// in a chat started elsewhere a step that needs an approval is asked of him under
// the live line (turnPart), unless that chat has /yolo on.
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
// http or https address, in a new tab, with no referrer, or a phone number (tel:)
// in the phone's own dialer.

const INLINE = new RegExp([
  '`([^`\\n]+)`',                                                // 1 code
  '\\[([^\\]\\n]+)\\]\\((https?:\\/\\/[^\\s)]+)\\)',              // 2 text, 3 address
  '((?:https?:\\/\\/|(?<![\\w.@/])www\\.)[^\\s<>()]*[^\\s<>().,;:!?\'"\\]])', // 4 a bare address (www. too)
  '\\*\\*(?=\\S)([^\\n]+?)(?<=\\S)\\*\\*',                        // 5 bold
  '(?<!\\w)__(?=\\S)([^\\n]+?)(?<=\\S)__(?!\\w)',                 // 6 bold
  '~~(?=\\S)([^\\n]+?)(?<=\\S)~~',                                // 7 struck
  '(?<![\\w*])\\*(?=[^\\s*])([^*\\n]+?)(?<=\\S)\\*(?![\\w*])',    // 8 italic
  '(?<!\\w)_(?=[^\\s_])([^_\\n]+?)(?<=\\S)_(?!\\w)',              // 9 italic, never inside a_name
  '(?<![\\w/])(@(?:file|folder|url|image|tool|terminal|git):(?:"[^"\\n]+"|\'[^\'\\n]+\'|`[^`\\n]+`|\\S+))', // 10 a reference
  '(?<![\\w@/])@([A-Za-z][\\w-]{0,30})(?![\\w:@-])',              // 11 a mention
  // 12 a phone number: + and a country code, or ten digits in the US way, with its spaces, dots or dashes
  '(?<![\\w+/#.-])((?:\\+\\d{1,3}[\\s.-]?)?\\(?\\d{3}\\)?[\\s.-]\\d{3}[\\s.-]\\d{4})(?![\\w-])'
].join('|'), 'g')

// What a link is, for its mark and its words (Patrick, 2 Nov: "it shows the little chat logo and it changes
// based on what the link is"): a project thread, a Claude session or chat, an artifact, a pull request or
// an issue on GitHub, or any other site. Each opens where it lives, in a new tab or its own app.
function linkKind(href) {
  let u
  try { u = new URL(href) } catch (e) { return null }
  const host = u.hostname.replace(/^www\./, '')
  if (host === 'claude.ai') {
    if (u.searchParams.get('thread')) return { kind: 'thread', words: 'Thread' }
    if (/^\/(code\/)?artifact\//.test(u.pathname)) return { kind: 'artifact', words: 'Artifact' }
    if (/^\/code\/session_/.test(u.pathname)) return { kind: 'session', words: 'Session' }
    if (/^\/(chat|code\/project|project)\//.test(u.pathname)) return { kind: 'thread', words: 'Chat' }
    return null
  }
  const gh = host === 'github.com' && u.pathname.match(/^\/([\w.-]+)\/([\w.-]+)\/(pull|issues)\/(\d+)/)
  if (gh) return { kind: gh[3] === 'pull' ? 'pr' : 'issue', words: `${gh[2]} #${gh[4]}` }
  return null
}

function linkChip(words, href, kind) {
  return el('a', { class: 'chip-link', href, target: '_blank', rel: 'noopener noreferrer' }, icon(kind, 15), words)
}

// Who a mention names, as the app names them, with their colour (app.css .a-<id>).
const MENTIONED = { bramble: 'Bramble', rift: 'Rift', sprocket: 'Sprocket', echo: 'Echo', magpie: 'Magpie', wisp: 'Wisp',
  hermes: 'Hermes', one: 'One', patrick: 'Patrick' }

// Where a mention goes: Hermes and One to their chat lists, a Bot to its Bot Chat. Patrick is himself.
const MENTION_GOES = { me: '/m/hermes/me', one: '/m/hermes/one', bramble: '/m/bots/bramble', rift: '/m/bots/rift',
  sprocket: '/m/bots/sprocket', echo: '/m/bots/echo', magpie: '/m/bots/magpie', wisp: '/m/bots/wisp' }

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
    else if (m[2] !== undefined || m[4] !== undefined) {
      const href = m[3] ?? (/^www\./i.test(m[4]) ? 'https://' + m[4] : m[4])
      const k = !still && linkKind(href)
      out.push(k ? linkChip(m[2] ?? k.words, href, k.kind) : to(m[2] ?? m[4], href))
    }
    else if (m[12] !== undefined) {
      out.push(still ? m[12] : el('a', { class: 'chip-link', href: 'tel:' + m[12].replace(/[^\d+]/g, '') }, icon('phone', 15), m[12]))
    }
    else if (m[5] !== undefined || m[6] !== undefined) out.push(el('strong', {}, inlineMd(m[5] ?? m[6], still)))
    else if (m[7] !== undefined) out.push(el('s', {}, inlineMd(m[7], still)))
    else if (m[8] !== undefined || m[9] !== undefined) out.push(el('em', {}, inlineMd(m[8] ?? m[9], still)))
    // A file or other reference is its name, as plain text: it never opens, loads or attaches anything.
    else if (m[10] !== undefined) out.push(el('span', { class: 'ref' }, refLabel(m[10])))
    else {
      const who = MENTIONED[m[11].toLowerCase()]
      const id = who && (AGENT_BY_NAME[who] || (who === 'Patrick' ? 'patrick' : null))
      // A mention of Hermes, One or a Bot opens that chat, in the app (Patrick, 2 Nov: "I can't click on the @s").
      const to = MENTION_GOES[id]
      out.push(to && !still
        ? el('a', { href: to, 'data-go': '', class: 'mention a-' + id }, '@' + who)
        : el('span', { class: 'mention' + (id ? ' a-' + id : '') }, '@' + (who || m[11])))
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
  if (!slideReady) { slideReady = true; slideForTimes() }
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
      // Pressing and holding a message opens its actions, as in any messaging app (Patrick, 2 Nov: a tap
      // opened them). A tap does nothing here, so a link, a mention or a file's chip in it opens as itself.
      bub.tabIndex = 0
      holdForActions(bub, () => messageSheet(m))
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
      // The time shows only while the chat is slid left (slideForTimes), as in any messaging app; what is
      // happening to his own message (In queue, Sent, a failure) stays under it.
      m.note ? el('div', { class: 'meta' + (m.warn ? ' warn' : '') }, m.note) : null,
      clock(m.at) ? el('time', { class: 'at', datetime: m.at }, clock(m.at)) : null))
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

// Message times stay out of the way until the chat is slid left with a finger, as in the Claude app
// (Patrick, 2 Nov: "it shouldn't show time by default ... if you slide it does"). The messages follow the
// finger up to the width of the times and spring back when it lets go; a Desktop shows a message's time
// when the mouse is over it. One listener on the page serves every chat.
const SLIDE_MAX = 72
let slideReady = false
function slideForTimes() {
  let list = null, start = null, sliding = false
  const reset = () => {
    if (list) { list.classList.remove('sliding'); list.style.transform = '' }
    list = null; start = null; sliding = false
  }
  document.addEventListener('pointerdown', e => {
    if (e.pointerType === 'mouse') return
    const msgs = e.target.closest('.chat .msgs')
    if (!msgs) return
    list = msgs; start = [e.clientX, e.clientY]; sliding = false
  })
  document.addEventListener('pointermove', e => {
    if (!list || !start) return
    const dx = e.clientX - start[0], dy = e.clientY - start[1]
    if (!sliding) {
      if (Math.abs(dy) > 10 && Math.abs(dy) > Math.abs(dx)) return reset()   // scrolling the chat
      if (dx > -10 || Math.abs(dx) <= Math.abs(dy)) return
      sliding = true
      list.classList.add('sliding')
    }
    list.style.transform = `translateX(${Math.max(dx, -SLIDE_MAX)}px)`
  })
  for (const ev of ['pointerup', 'pointercancel']) document.addEventListener(ev, reset)
}

// Press and hold (about half a second, the finger kept still) on a phone, or a right click on a Desktop:
// Android's own long press says it as a contextmenu event, which the timer stands in for where it does not
// come. Either opens `open` once; the browser's own menu and text selection stay away from the message.
const HOLD_MS = 450
function holdForActions(node, open) {
  let timer = null, start = null, opened = 0
  const stop = () => { clearTimeout(timer); timer = null }
  const fire = () => { stop(); if (Date.now() - opened > 800) { opened = Date.now(); open() } }
  node.addEventListener('pointerdown', e => {
    if (e.pointerType === 'mouse' || e.target.closest('a, button, summary')) return
    start = [e.clientX, e.clientY]
    stop()
    timer = setTimeout(fire, HOLD_MS)
  })
  node.addEventListener('pointermove', e => {
    if (timer && start && Math.hypot(e.clientX - start[0], e.clientY - start[1]) > 10) stop()
  })
  for (const ev of ['pointerup', 'pointercancel', 'pointerleave']) node.addEventListener(ev, stop)
  node.addEventListener('contextmenu', e => {
    if (e.target.closest('a')) return
    e.preventDefault()
    fire()
  })
}

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
      note.textContent = 'This view does not allow copying.'
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
      canSpeak() && spoken(m) ? el('button', { class: 'secondary', type: 'button', onclick: () => { closeSheet(); readAloud(m) } },
        'Read aloud') : null,
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
function chatBar(back, backLabel, title, under, agent, label, more, live, info) {
  const right = [
    info ? el('button', { class: 'ib', type: 'button', 'aria-label': 'Chat details', 'data-details': '', onclick: info }, icon('info')) : null,
    more ? el('button', { class: 'ib', type: 'button', 'aria-label': 'Chat actions', onclick: more }, icon('more')) : null
  ].filter(Boolean)
  const bar = screenBar({ back, backLabel, agent, label, title, under, live,
    right: el('div', { class: 'bar-tools' }, right) })
  bar.classList.add('chatbar')
  return bar
}

// The box at the foot of a chat. It grows with what is typed, up to six
// lines, and keeps its draft (and the draft's idempotency key) while the
// chat redraws. On a computer (a mouse and a keyboard) Enter sends and
// Shift+Enter is a new line, as in Hermes Desktop's box (apps/desktop composer
// index.tsx: Enter without Shift submits); on a phone, whose keyboard has no
// Shift+Enter, Enter is a new line and the button sends. Ctrl or Cmd+Enter
// sends on both. Patrick, 2 Nov 6:03 PM: "Pressing enter doesnt send the message".
const KEYBOARD = matchMedia('(hover: hover) and (pointer: fine)')

// A paste of more than 3,000 characters into a message to his own Hermes goes
// as a "Pasted content" text file, as Hermes Desktop does it (its large-paste.ts
// LARGE_PASTE_ATTACHMENT_THRESHOLD, and electron/composer-paste.ts for the
// name): the words he types stay in the box and the paste rides along as an
// @file: reference that Hermes reads in whole. Before this the paste went
// into the box and the core refused the message at 8,000 characters, which
// Desktop never does (Patrick, 2 Nov 6:03 PM).
const PASTE_AS_FILE = 3000
const MAX_TEXT = 8000   // own.py MAX_TEXT, chat.py MAX_TEXT: the words of one message

function pastedFile(text) {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').replace('T', '_').replace('Z', '')
  const random = [...crypto.getRandomValues(new Uint8Array(3))].map(b => b.toString(16).padStart(2, '0')).join('')
  return new File([text], `pasted_content_${stamp}_${random}.txt`, { type: 'text/plain' })
}

// A status line said again, even when the words are the same as before, so a second try
// that is refused for the same reason is seen to have been tried (Patrick, 2 Nov 6:03 PM:
// "After trying to send again it ... just does nothing").
function sayAgain(status, words) {
  status.textContent = words
  status.classList.remove('again')
  void status.offsetWidth
  status.classList.add('again')
}

function chatComposer(key, label, buttonLabel, send, status, gw, attach, cards) {
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
  const box = el('textarea', { rows: '1', 'aria-label': label, placeholder: label,
    enterkeyhint: KEYBOARD.matches ? 'send' : 'enter' })
  box.value = draft.text
  const fit = () => { box.style.height = 'auto'; box.style.height = Math.min(box.scrollHeight + 2, 160) + 'px' }
  box.addEventListener('keydown', e => {
    // A key that ends a word being composed (Chinese, Japanese, accents) is the composition's, never a send.
    if (e.key !== 'Enter' || e.isComposing || e.keyCode === 229 || e.altKey) return
    if (e.ctrlKey || e.metaKey || (KEYBOARD.matches && !e.shiftKey)) {
      e.preventDefault()
      if (!button.disabled) sendIt()
    }
  })
  if (attach) {
    box.addEventListener('paste', e => {
      const text = e.clipboardData && e.clipboardData.getData('text/plain')
      if (!text || text.length <= PASTE_AS_FILE || (e.clipboardData.files && e.clipboardData.files.length)) return
      e.preventDefault()
      const file = pastedFile(text)
      addFile(attach, draft, file, status, 'Pasted content (' + sizeWords(file.size) + ')')
      route()
    })
  }
  box.addEventListener('input', () => {
    if (box.value !== draft.text) { draft.text = box.value; draft.key = newKey() }
    fit()
  })
  requestAnimationFrame(fit)
  // A message that answers a card (Patrick, 2 Nov: a chat should be an answer to a card): the line above
  // the box says which card and which option his words name, and the x sends the words without it.
  const answering = el('div', { class: 'answering', role: 'status', hidden: true })
  const paint = () => {
    if (!box.value.trim()) draft.noAnswer = false
    const m = draft.noAnswer ? null : cardMatch(box.value, cards)
    const was = draft.answer ? draft.answer.c.id + ':' + draft.answer.o.key : ''
    if ((m ? m.c.id + ':' + m.o.key : '') !== was) draft.key = newKey()
    draft.answer = m && m.c ? m : null
    answering.replaceChildren(...(draft.answer ? [
      el('div', { class: 'rq' }, el('strong', {}, 'Answers the card ' + (m.c.risk === 'high' ? '(high risk)' : '')),
        el('span', {}, inlineMd(m.c.title, true), ': ', el('b', {}, inlineMd(m.o.label, true)))),
      el('button', { class: 'back', type: 'button', 'aria-label': 'Send without answering the card', onclick: () => {
        draft.noAnswer = true
        paint()
      } }, icon('x'))] : m ? [el('div', { class: 'rq' }, m.why)] : []))
    answering.hidden = !m
  }
  box.addEventListener('input', paint)
  paint()
  const sendIt = async () => {
    const text = box.value.trim()
    const ready = draft.files.filter(f => f.id)
    if (draft.files.some(f => f.state === 'up')) { status.textContent = 'Wait for the files to finish uploading.'; return }
    if (!text && !ready.length) { sayAgain(status, 'Type a message first.'); return }
    if (text.length > MAX_TEXT) {
      sayAgain(status, `The box holds ${text.length.toLocaleString()} characters, and a message takes ` +
        `${MAX_TEXT.toLocaleString()}.` + (attach ? ' Paste long text instead of typing it, and it goes as a ' +
        'Pasted content file, as in Hermes Desktop.' : ''))
      return
    }
    status.textContent = 'Sending...'
    button.disabled = true
    const a = draft.answer
    const s = await send(text, draft.key, draft.reply, ready.map(f => f.id), a ? { decision_id: a.c.id,
      option_key: a.o.key, revision: a.c.revision, attention_revision: a.c.attention_revision,
      artifact_sha256: a.c.artifact_sha256 } : undefined)
    button.disabled = false
    if (s.status === 200) {
      if (a) { toWorker({ close: a.c.id }); badgeNow() }
      drafts[key] = null
      chatPlace.toEnd = true
      route()
      return true
    }
    if (s.status === 401) { csrf = null; return signInView(said(s.body)) }
    if (a && s.status === 403 && s.body.error === 'sign_in_again') {
      // A high-risk card: the passkey is the yes, as on the card itself. Nothing is sent until it succeeds.
      const again = el('button', { class: 'primary', type: 'button', 'data-confirm': a.o.key,
        onclick: () => promptNow(again, status, sendIt) }, 'Answer ' + a.o.label + ' with your ' + signInWord())
      status.replaceChildren(again)
      return promptNow(again, status, sendIt)
    }
    const d = s.body.detail || {}
    sayAgain(status, s.status === 0
      ? 'Could not reach the core. Sending again sends the same message, never a second one.'
      : (gw && d.state ? stateWords({ gateway: gw, ...d })[1] : said(s.body)))
  }
  const button = el('button', { class: 'send', type: 'button', 'aria-label': buttonLabel, onclick: sendIt }, icon('send'))
  const plus = attach ? el('button', { class: 'plus', type: 'button', 'aria-label': 'Add photos or files',
    onclick: () => attachSheet(attach, draft, status) }, icon('plus')) : null
  // The microphone: what he says is written down into the box by the VM's Whisper (dictate).
  const speak = canRecord() ? el('button', { class: 'mic', type: 'button', 'aria-label': 'Speak instead of typing',
    'aria-pressed': String(voice.mode === 'dictate' && voice.key === key), onclick: () => dictate(comp.voice) }, icon('mic')) : null
  const comp = el('div', { class: 'composer', 'data-reply': draft.reply ? String(draft.reply.mid) : '',
    'data-files': draft.files.map(f => f.local + ':' + f.state).join(' '),
    'data-cards': (cards || []).map(c => c.id + ':' + c.revision + ':' + c.attention_revision).join(' ') },
  plus, box, speak, button)
  // What voice needs of this box: words added to it as typed, and sent as the send button sends them.
  const put = words => {
    box.value = (box.value.trim() ? box.value.replace(/\s+$/, '') + ' ' : '') + words
    box.dispatchEvent(new Event('input'))
  }
  comp.voice = { key, name: label.replace(/^Message to /, ''), status, add: put,
    send: async words => {
      put(words)
      if (draft.answer) return 'card'
      return (await sendIt()) === true
    } }
  return [replying, answering, attach ? attachStrip(attach, draft, status) : null, comp]
}

// Which card and option his words answer: the words start with an option's label or key, as a whole
// word or more. The longest name wins ("Yes, after 4 PM" over "Yes"); the same length on two options
// answers nothing and says so, so he taps the one he means on its card. Never a guess past that.
function cardMatch(text, cards) {
  const t = text.trim().toLowerCase()
  if (!t || !cards || !cards.length) return null
  let best = [], len = 0
  for (const c of cards) {
    for (const o of c.options) {
      const n = Math.max(...[o.label, o.key].map(w => {
        const v = String(w || '').trim().toLowerCase()
        return v && t.startsWith(v) && (t.length === v.length || !/[\p{L}\p{N}]/u.test(t[v.length])) ? v.length : 0
      }))
      if (n > len) { best = [{ c, o }]; len = n } else if (n && n === len) best.push({ c, o })
    }
  }
  if (best.length === 1) return best[0]
  return best.length ? { why: 'Those words fit options on more than one card. Tap the one you mean on its card.' } : null
}

// The cards a Bot asked that wait on Patrick, at the end of its Bot Chat: each with its options, so a tap
// answers it here as on Decisions, and a message whose words name an option answers it too (chatComposer).
function chatCards(cards, back) {
  if (!cards || !cards.length) return null
  const status = el('p', { class: 'warn', role: 'status' })
  return el('div', { class: 'chat-cards' }, cards.map(c => el('div', { class: 'card', 'data-card': c.id },
    el('div', { class: 'cardhead' },
      el('a', { href: '/m/decisions/' + encodeURIComponent(c.id), onclick: e => { e.preventDefault(); go('/m/decisions/' + c.id) } },
        el('strong', {}, inlineMd(c.title, true))),
      c.risk === 'high' ? el('span', { class: 'risk' }, 'high risk') : null),
    el('div', { class: 'opts' }, c.options.map(o => el('button', {
      class: o.key === c.recommendation ? 'primary' : 'secondary', type: 'button', 'data-option': o.key,
      onclick: () => { answerBack = back; return choose(c, o, status) } }, inlineMd(o.label, true)))))), status)
}

// Photos and files on a message to his own Hermes (Patrick, 2 Nov: "no way to add files or photos or
// anything add a + button like Claude and Hermes have"), and to a Bot or a room (#81). The + offers the
// camera, his photos and his files; each is uploaded to the core as soon as it is picked (to the chat's
// own route, `base`: /own/<gw> or /chat/<Bot or room>), shows above the box while it goes, and can be taken
// off before sending. The message names them, and Hermes gets them as Desktop hands them over: in his own
// Hermes or the Bot's own folders, each photo as the turn's image and every other file as an @file:
// reference (own_connector.py hand_over), or in a room's own store (room_send.py). Up to 10 a message,
// 20 MB each; a room takes 8 a message, 15 MB each.
const ATTACH_MAX = 20 * 1024 * 1024
let localFile = 0

function attachSheet(base, draft, status) {
  closeSheet()
  const pick = (accept, capture) => {
    closeSheet()
    const input = el('input', { type: 'file', multiple: capture ? null : true, accept: accept || null,
      capture: capture || null, hidden: true })
    input.addEventListener('change', () => {
      for (const file of [...input.files]) addFile(base, draft, file, status)
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

async function addFile(base, draft, file, status, label) {
  if (draft.files.length >= 10) { sayAgain(status, 'A message takes up to 10 files.'); return }
  const f = { local: ++localFile, name: file.name || 'photo.jpg', mime: file.type || '', size: file.size, state: 'up',
    label: label || null }
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
    const resp = await fetch(`${API}${base}/attach?name=${encodeURIComponent(f.name)}`,
      { method: 'POST', headers, credentials: 'same-origin', cache: 'no-store', body: file })
    let out = {}
    try { out = await resp.json() } catch (e) { out = {} }
    r = { status: resp.status, body: out }
  } catch (e) {
    r = { status: 0, body: { error: 'offline' } }
  }
  if (!draft.files.includes(f)) {
    // Taken off while it went up: take it off the core too.
    if (r.status === 200) call('POST', `${base}/unattach`, { id: r.body.id })
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

function attachStrip(base, draft, status) {
  if (!draft.files.length) return null
  return el('div', { class: 'files', role: 'list', 'aria-label': 'Files on this message' }, draft.files.map(f =>
    el('div', { class: 'file ' + f.state, role: 'listitem' },
      icon(/^image\//.test(f.mime) ? 'image' : 'file', 18),
      el('span', { class: 'fname' }, f.label || f.name),
      el('span', { class: 'fstate' }, f.state === 'up' ? 'Uploading...' : f.state === 'bad' ? 'Not added: ' + f.why
        : sizeWords(f.size)),
      el('button', { class: 'ib', type: 'button', 'aria-label': 'Take off ' + (f.label || f.name), onclick: () => {
        draft.files = draft.files.filter(x => x !== f)
        draft.key = newKey()
        if (f.id) call('POST', `${base}/unattach`, { id: f.id })
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
  if (oldDock && newBox && newBox.value === box.value && same('reply') && same('files') && same('cards')) {
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
  const c = openComposer()
  if (c) c.chat = key
  voiceButtons()
  voiceSaw(key, items)
  wakeCheck()
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
    title: chatName(c),
    lines: [snippet(c.preview), o && inFlight(o) ? opLine(o, GATEWAY[gw]) : null],
    right: at(c.last_at) || when(c.last_at) })
}

// A new chat Patrick just started here: the app goes into it as soon as Hermes has made it, while its first
// turn runs, and he follows that turn there (10 Nov: a long first turn sat under Starting on "Sending", with
// no chat to open until it ended). Read again every two seconds until then; a message that failed stays
// under Starting with its reason.
const started = {}
let startWatch = null
function followStart(gw, v, seqNow) {
  const mid = started[gw]
  if (!mid) return false
  const sid = (v.made || {})[mid]
  if (sid) {
    delete started[gw]
    go(`/m/hermes/${gw}/${sid}`)
    return true
  }
  const m = v.starting.find(x => x.id === mid)
  if (!m || (m.status !== 'queued' && m.status !== 'leased')) {
    delete started[gw]
    return false
  }
  clearTimeout(startWatch)
  startWatch = setTimeout(() => {
    if (seqNow !== viewSeq || document.visibilityState !== 'visible' || started[gw] !== mid) return
    const typing = document.activeElement && /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)
    if (typing) return followStart(gw, v, seqNow)
    route()
  }, 2000)
  return false
}

async function gatewayView(gw, into = 'view') {
  // In the desktop's chat list it goes with the chat beside it, so it takes that view's number, not a new one.
  if (into === 'view') setTab('bots')
  const seqNow = into === 'view' ? ++viewSeq : viewSeq
  const r = await call('GET', '/own/' + gw)
  if (refused(r, into)) return
  const v = r.body
  const name = GATEWAY[gw]
  if (followStart(gw, v, seqNow)) return
  const pick = pickers[gw] || (pickers[gw] = { q: '', results: null, more: false, extra: [] })
  const status = el('p', { class: 'warn', role: 'status' })
  const parts = [
    screenBar({ back: into === 'view' ? () => go('/m/bots') : null, backLabel: 'All Thornswood Bots', agent: gw, title: name, heading: true,
      right: gatewayLine(v), live: gatewayLive(v) })
  ]
  if (v.can_send && v.online) {
    parts.push(signInPart(v, status))
    if (v.sign_in_until) {
      parts.push(composer('new:' + gw, 'Start a new chat', async (text, key) => {
        const s = await call('POST', `/own/${gw}/send`, { new: true, text, idempotency_key: key })
        if (s.status === 200 && s.body.message_id) started[gw] = s.body.message_id
        return s
      }, status, gw), status)
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
  // The picker: every chat Hermes's own pickers list, newest first, paged. Its search finds chats by the
  // words of their messages and their titles, with Hermes's own search, as Desktop's does.
  const search = el('input', { type: 'search', autocomplete: 'off', placeholder: 'Search chats',
    'aria-label': 'Search chats' })
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
    pick.why = p.body.why || ''
    route()
  }
  const searching = pick.results !== null
  const shown = (searching ? pick.results : v.chats).concat(pick.extra)
  const hasMore = (searching || pick.extra.length) ? pick.more : v.chats.length >= 50
  parts.push(el('h2', { class: 'section' }, searching ? `Chats that say "${pick.q.trim()}"` : 'Chats'),
    el('div', { class: 'search' }, search,
      el('button', { class: 'secondary', type: 'button', onclick: () => {
        if (!pick.q.trim()) { pick.results = null; pick.extra = []; return route() }
        pick.results = []
        more(null)
      } }, 'Search')),
    searching ? el('button', { class: 'secondary', type: 'button', onclick: () => {
      pick.q = ''; pick.results = null; pick.extra = []; route()
    } }, 'Show every chat') : null,
    shown.length ? shown.map(c => chatRow(gw, c, v.ops)) : (searching && pick.why ? el('p', { class: 'warn' }, pick.why) : el('p', { class: 'quiet' }, searching ? 'No chat says that.' : 'No chats yet.')),
    hasMore ? el('button', { class: 'secondary', type: 'button', onclick: () => more(shown[shown.length - 1].last_at) }, 'Older chats') : null,
    note,
    el('button', { class: 'secondary', type: 'button', onclick: () => go(`/m/hidden/${gw}`) }, 'Hidden chats'))
  if (seqNow === viewSeq) showIn(into, parts)
}

// A Bot's message in his Bot Chat comes as the user's turn, signed the way Hermes signs one
// ("Message from 🤖 Wisp (@wisp): ", tools/bot_mode_dm.py): drawn as that Bot's, in its words, without
// the note the core adds under a Bot's step for One (own.py BOT_NOTE).
const BOT_SIGNED = /^Message from \u{1F916} ([^(\n]{1,64}) \(@([A-Za-z0-9_-]{1,64})\): /u
const BOT_NOTE_START = '\n\n(Sent to you through the decision core.'
function botWords(text) {
  const i = String(text).indexOf(BOT_NOTE_START)
  return i >= 0 ? String(text).slice(0, i) : String(text)
}
function fromBot(item) {
  const m = item.mine && String(item.text || '').match(BOT_SIGNED)
  if (!m) return item
  return { ...item, mine: false, who: m[1], agent: m[2].toLowerCase(), text: botWords(item.text.slice(m[0].length)) }
}

// What Hermes holds of one of his own chats: notes to show above it, its
// messages as bubbles, and the Older messages button when there are more.
function historyPart(gw, sid, t, name, feed) {
  if (t.error) return { notes: [el('p', { class: 'warn' }, t.error)], items: [], gone: false }
  if (!t.newest) {
    // Before the first page: the last messages the connector's feed sent, when it sent this chat's.
    const items = (feed || []).map((m, k) => fromBot({ mine: m.role === 'user', who: name, at: m.at, text: m.text, mid: 'feed' + k,
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
  const items = msgs.map(m => fromBot({ mine: m.role === 'user', who: name, at: m.at, text: m.text, mid: m.mid,
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
  react_to_message: 'Reacting to a message', tw_git: 'Working on GitHub', tw_files: 'Working in its folder',
  tw_decide: 'Asking you on a card', send_message: 'Sending a message'
}
function doingWords(doing) {
  if (!doing) return 'Starting'
  if (doing.state === 'thinking') return 'Thinking'
  if (doing.state === 'writing') return 'Writing'
  const tool = doing.tool || ''
  return TOOL_WORDS[tool] || (tool.startsWith('browser') ? 'Using the browser' : tool ? `Using ${tool}` : 'Using a tool')
}
// How Hermes took a message sent into a chat whose turn was already running, by that chat's busy-input
// setting, as Desktop says it under the message (Patrick, 2 Nov: "it should say in queue like it does on
// Desktop"): in its queue, to run next; steered into the running turn; or the running turn interrupted
// and going on with it. Any other message Hermes has is sent.
const TAKEN = { queued: 'In queue', steered: 'Steered into the running turn', redirected: 'Interrupted the running turn' }
function inQueue(m) { return m.status === 'leased' && !!m.doing && m.doing.state === 'queued' }
function mineNote(m) {
  if (m.status === 'leased' && m.doing && m.doing.admitted) return m.doing.taken ? TAKEN[m.doing.taken] : TAKEN[m.doing.state] || 'Sent'
  return STATE[m.status]
}
// The message whose turn the chat's live line follows: the one being answered, never one in Hermes's queue.
function beingAnswered(mine) { return mine.find(m => m.status === 'leased' && !inQueue(m)) }
// The other side's typing bubble: three moving dots and what it is doing.
function workingItem(name, agent, doing, mid) {
  const seen = mid && liveSeen[mid]
  const parts = seen && seen.parts && seen.parts.length ? seen.parts : null
  return el('div', { class: 'msg theirs working', ...(mid ? { 'data-turn': mid } : {}) },
    el('div', { class: 'who a-' + agent }, name),
    el('div', { class: 'bub live', hidden: !parts }, parts ? liveParts(parts) : null),
    el('div', { class: 'bub typing', role: 'status', 'aria-live': 'polite' },
      el('span', { class: 'dots', 'aria-hidden': 'true' }, el('i'), el('i'), el('i')),
      el('span', { class: 'doing' }, doingWords(doing))))
}

// ── a turn as it happens, as Hermes Desktop draws it (live_turns.py) ─────
//
// Patrick, 9 Nov: "it doesnt feel live it feels very delayed and I want it all to be live". While a turn
// answers one of his messages, the chat shows it as Desktop does: its thinking (folded once it is past),
// the reply as it is written, each tool with what it runs and a tick once done, each subagent with what it
// is doing, and Hermes's status lines. The core answers the open chat the moment the turn changes (a long
// poll), so nothing waits on a read every few seconds. Once the turn is over, the reply it wrote stays
// under his message until the chat's own read from Hermes has it.
const liveSeen = {}   // message id -> the turn's parts the core last gave, {n, parts}
let liveWatch = null

function stepWords(name) {
  return TOOL_WORDS[name] || (name ? name.replace(/_/g, ' ') : 'A tool')
}

const SUB_WORDS = { completed: 'Done', failed: 'Failed', interrupted: 'Stopped', cancelled: 'Stopped', error: 'Failed',
  timeout: 'Ran out of time' }

function liveParts(parts) {
  return parts.map((part, i) => {
    const last = i === parts.length - 1
    if (part.k === 'text') return el('div', { class: 'lv-text' }, markdown(part.text))
    if (part.k === 'think') {
      return el('details', { class: 'lv-think', open: last },
        el('summary', {}, last ? 'Thinking' : 'Thought'), el('div', { class: 'lv-think-text' }, part.text))
    }
    if (part.k === 'step') {
      const prog = part.progress
      return el('div', { class: 'lv-step ' + (part.error ? 'failed' : part.done ? 'done' : 'running') },
        el('span', { class: 'lv-mark', role: 'img', 'aria-label': part.error ? 'Failed' : part.done ? 'Done' : 'Running' },
          part.error ? '\u2715' : part.done ? '\u2713' : ''),
        el('span', { class: 'lv-name' }, stepWords(part.name)),
        part.preview ? el('code', { class: 'lv-preview' }, part.preview) : null,
        prog ? el('span', { class: 'lv-prog' },
          [prog.percent !== null && prog.percent !== undefined ? prog.percent + '%' : '', prog.message].filter(Boolean).join(' ')) : null)
    }
    if (part.k === 'sub') {
      const running = part.status === 'running'
      const tools = part.tools ? `${part.tools} tool${part.tools === 1 ? '' : 's'}` : ''
      const now = running
        ? [part.tool ? stepWords(part.tool) + (part.preview ? ': ' + part.preview : '') : 'Working', tools].filter(Boolean).join(' \u00b7 ')
        : [SUB_WORDS[part.status] || 'Done', tools ? 'after ' + tools : ''].filter(Boolean).join(' ')
      return el('div', { class: 'lv-sub ' + (running ? 'running' : part.status) },
        el('div', { class: 'lv-sub-head' }, el('b', {}, 'Subagent'), part.goal ? ': ' + part.goal : ''),
        el('div', { class: 'lv-sub-now' }, now),
        !running && part.summary ? el('div', { class: 'lv-sub-sum' }, snippet(part.summary, 3)) : null)
    }
    if (part.k === 'note') return el('p', { class: 'lv-note' }, part.text)
    return null
  })
}

// The reply a turn wrote, as it showed live: its last words after its last tool.
function liveReply(parts) {
  const last = [...(parts || [])].reverse().find(p => p.k === 'text')
  return last ? last.text : ''
}

function drawLive(mid) {
  const turn = document.querySelector(`#view .msg.working[data-turn="${CSS.escape(mid)}"]`)
  const box = turn && turn.querySelector('.bub.live')
  if (!box) return
  const end = nearEnd()
  const seen = liveSeen[mid]
  box.replaceChildren(...liveParts(seen.parts).filter(Boolean))
  box.hidden = !seen.parts.length
  if (end) scrollTo({ top: document.documentElement.scrollHeight })
}

// Follows one turn while its chat is the page open: each answer is asked for again at once.
function watchLive(mid, seqNow) {
  if (liveWatch && liveWatch.mid === mid && liveWatch.seq === seqNow) return
  const me = liveWatch = { mid, seq: seqNow }
  const on = () => liveWatch === me && seqNow === viewSeq && document.visibilityState === 'visible'
  ;(async () => {
    while (on()) {
      const had = liveSeen[mid]
      const r = await call('GET', `/turns/${encodeURIComponent(mid)}?after=${had ? had.n : 0}&wait=25`)
      if (!on()) break
      if (r.status !== 200) {
        await new Promise(res => setTimeout(res, 3000))
        continue
      }
      if (r.body.parts) {
        liveSeen[mid] = r.body
        drawLive(mid)
      } else if (had && had.parts) {
        // The turn is over: its result came, and the chat reads Hermes's own copy of the reply now.
        liveSeen[mid] = { n: 0, parts: had.parts, over: true }
        break
      }
    }
    if (liveWatch === me) liveWatch = null
    if (seqNow === viewSeq && liveSeen[mid] && liveSeen[mid].over) route()
  })()
}

// While a turn runs in the open chat, its line follows every two seconds, reading only this chat's state;
// when the turn ends the chat is drawn again with the reply. A message still waiting to go is watched the
// same way, so the chat is drawn again the moment it is taken and its turn's line shows (2 Nov: it waited
// for the next 15-second read). One watch at a time, ended by any new page.
let turnWatch = null
function watchTurn(path, seqNow, turnId, name, was = 'leased', queued = false, drawn = '') {
  clearTimeout(turnWatch)
  turnWatch = setTimeout(async () => {
    if (seqNow !== viewSeq || document.visibilityState !== 'visible') return
    const r = await call('GET', path)
    if (seqNow !== viewSeq || r.status !== 200) return
    const m = r.body.mine.find(x => x.id === turnId)
    // Taken from Hermes's queue to run, or a question opened or settled: drawn again.
    if (!m || m.status !== was || (was === 'leased' && inQueue(m) !== queued) || askSig(m.doing) !== drawn) {
      const typing = document.activeElement && /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)
      if (!typing) return route()
    }
    const line = document.querySelector('#view .msg.working .doing')
    if (line && m && was === 'leased' && !queued) line.textContent = doingWords(m.doing)
    watchTurn(path, seqNow, turnId, name, was, queued, drawn)
  }, 2000)
}

// ── a running turn of his own Hermes: Stop, and its questions and approvals (own_turns.py) ──────
//
// Patrick, 9 Nov: switch fully from Hermes Desktop to this app. Desktop has Stop, and shows a question
// Hermes asks (its clarify tool) and an approval for a flagged command for him to answer; so does this,
// under the live line of the turn being answered, with Hermes's own choices. Each needs the passkey
// sign-in a message needs; the turn itself makes Hermes's own call, and Hermes checks it again.
const APPROVAL_WORDS = { once: 'Allow once', session: 'Allow in this chat', always: 'Always allow', deny: 'Deny' }
function askSig(doing) {
  return doing && (doing.asks || doing.order) ? JSON.stringify([doing.asks || [], doing.order || null]) : ''
}
function turnPart(gw, name, turn, canAct) {
  const d = turn && turn.doing
  if (!d || !d.admitted || !canAct) return null
  const status = el('p', { class: 'warn', role: 'status' })
  const order = async (body, what) => {
    status.className = 'receipt'
    status.textContent = what
    const r = await call('POST', `/own/${gw}/turn`, { message_id: turn.id, ...body })
    if (r.status === 200) return route()
    if (r.status === 401) { csrf = null; return signInView(said(r.body)) }
    status.className = 'warn'
    status.textContent = r.status === 0 ? 'Could not reach the core. Nothing was sent.' : said(r.body)
  }
  const answer = (a, value) => order({ op: 'answer', prompt_id: a.prompt_id, value }, `Sending your answer to ${name}...`)
  const asks = (d.asks || []).map(a => {
    if (a.kind === 'approval') {
      return el('div', { class: 'card ask', 'data-ask': a.prompt_id },
        el('p', {}, el('strong', {}, a.description || `${name} asks to run this`)),
        a.command ? codeBlock(a.command, 'command') : null,
        el('div', { class: 'opts' }, a.choices.map(c => el('button', {
          class: c === 'deny' ? 'secondary' : 'primary', type: 'button', 'data-choice': c,
          onclick: () => answer(a, c) }, APPROVAL_WORDS[c] || c))))
    }
    const own = el('input', { type: 'text', autocomplete: 'off', 'aria-label': 'Your answer', placeholder: 'Your answer' })
    const picked = new Set()
    const choices = a.choices.map(c => el('button', { class: 'secondary', type: 'button', 'data-choice': c,
      'aria-pressed': a.multi_select ? 'false' : null,
      onclick: e => {
        if (!a.multi_select) return answer(a, c)
        if (picked.has(c)) picked.delete(c); else picked.add(c)
        e.currentTarget.setAttribute('aria-pressed', String(picked.has(c)))
      } }, inlineMd(c, true)))
    const form = el('form', { class: 'ask-own' }, own, el('button', { class: 'primary', type: 'submit' }, 'Send'))
    form.addEventListener('submit', e => {
      e.preventDefault()
      if (own.value.trim()) return answer(a, own.value.trim())
      if (a.multi_select && picked.size) return answer(a, a.choices.filter(c => picked.has(c)))
    })
    return el('div', { class: 'card ask', 'data-ask': a.prompt_id },
      markdown(a.question || `${name} asks you something`),
      choices.length ? el('div', { class: 'opts' }, choices) : null, form)
  })
  const went = d.order && d.order.status === 'refused'
    ? el('p', { class: 'warn', role: 'status' }, (d.order.op === 'stop' ? 'Stop did not go: ' : 'Your answer did not go: ')
      + (d.order.reason || d.order.status)) : null
  return el('div', { class: 'turn-part' }, asks, went,
    el('div', { class: 'turn-stop' }, el('button', { class: 'secondary', type: 'button', 'data-stop': turn.id,
      onclick: () => order({ op: 'stop' }, `Stopping ${name}'s turn...`) }, 'Stop')), status)
}

function pendingItems(mine, readAt, who) {
  return mine.filter(m => m.status !== 'sent' || !readAt || new Date(m.updated_at) > readAt)
    .flatMap(m => {
      // A Bot's step for One (own.py ask_one): drawn as that Bot's message, not as one he sent.
      const bot = m.from_bot ? { mine: false, bot: true, who: m.from_bot.charAt(0).toUpperCase() + m.from_bot.slice(1), agent: m.from_bot } : {}
      const item = { mine: true, at: m.created_at, text: m.text, status: m.status, replyTo: m.reply_to, sent: m.files, ...bot,
        note: mineNote(m) + ((m.status === 'dead' || m.status === 'uncertain') && m.reason ? ': ' + m.reason : '')
          + (m.status === 'queued' && m.waits ? ': ' + m.waits : ''),
        warn: m.status === 'dead' || m.status === 'uncertain' }
      // The reply its turn wrote, as it showed live, until the chat's own read from Hermes has it.
      const reply = m.status === 'sent' && who && liveSeen[m.id] ? liveReply(liveSeen[m.id].parts) : ''
      return reply ? [item, { mine: false, who, agent: who, at: m.updated_at, text: reply }] : [item]
    })
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
    const items = h.items.concat(pendingItems(v.mine, readAt, name).map(m => m.bot ? m : { ...m, who: name }))
    // A rename, hide or delete asked for here: how it stands, and once a hide or a delete is done, so it says.
    const o = lastOp(v.ops, sid)
    const ended = o && o.status === 'done' && (o.op === 'delete' || o.op === 'archive') ? o.op : null
    // His Bot Chat is kept as it is, so it offers no Rename, Hide or Delete (the core refuses them too).
    const manage = v.can_manage && v.online && !h.gone && !t.error && !ended && !(o && inFlight(o)) && !v.chat.bot_chat
    const top = [
      chatBar(() => go('/m/hermes/' + gw), 'All chats on ' + name, chatName(v.chat),
        undefined, gw, name,
        manage ? () => chatSheet(gw, sid, v.chat.title, v) : null, gatewayLive(v),
        v.can_send && v.online && !h.gone && !t.error && !ended ? () => openDetails(gw, sid) : null),
      ended === 'delete' ? el('p', { class: 'receipt', role: 'status' }, `Deleted from ${name} for good.`) : null,
      ended === 'archive' ? el('p', { class: 'receipt', role: 'status' }, `Hidden from ${name}'s chat list, with every message kept. `,
        el('button', { class: 'secondary', type: 'button', onclick: () => go(`/m/hidden/${gw}`) }, 'Hidden chats')) : null,
      opLine(o, name),
      h.notes
    ]
    const dock = []
    if (ended) {
      // Nothing sent here would reach a chat that is gone or hidden.
    } else if (v.can_send && v.online && !h.gone && !t.error) {
      if (v.sign_in_until) {
        dock.push(signInPart(v, status), chatComposer(draftKey, 'Message to ' + name, 'Send to ' + name,
          (text, k, reply, files) => call('POST', `/own/${gw}/send`, { session_id: sid, text, idempotency_key: k,
            ...(reply ? { reply_to: reply.mid, quote: reply.quote, quote_by: reply.by } : {}),
            ...(files.length ? { attachments: files } : {}) }), status, gw, '/own/' + gw), status)
      } else {
        dock.push(signInPart(v, status), status)
      }
    } else if (!v.can_send) {
      dock.push(el('p', { class: 'quiet' }, said({ error: 'own_chat_off' })))
    }
    // His message being answered now: the other side's live line under it, kept current while it runs.
    const turn = beingAnswered(v.mine)
    const canAct = v.can_send && v.online && !!v.sign_in_until && !h.gone && !t.error && !ended
    const list = turn ? [bubbles(items), workingItem(name, gw, turn.doing, turn.id), turnPart(gw, name, turn, canAct)] : bubbles(items)
    if (seqNow === viewSeq) {
      showChat('own:' + key, top, list, dock, items)
      if (turn) watchLive(turn.id, seqNow)
      const waits = turn || v.mine.find(inQueue) || v.mine.find(m => m.status === 'queued')
      if (waits) watchTurn(`/own/${gw}/${encodeURIComponent(sid)}`, seqNow, waits.id, name, waits.status, inQueue(waits),
        waits === turn ? askSig(turn.doing) : '')
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

// ── a chat's model and thinking: its details (own_control.py) ──────────────
//
// What Hermes holds for one of his own chats, read through that Hermes's connector: the model, the
// thinking level, whether approvals are skipped in it, and what a message sent while it works does. The
// model and the thinking level change for that chat only, never Hermes's default. Only models his
// sign-ins, a free model or a local one covers are offered (plan Part 6, decision 4 A). On a wide screen
// the details sit on the right in place of Decisions; on a phone they open in a sheet.

const details = { open: false }
const detailKeys = {}          // the idempotency key of each change, so asking again is the same change
const detailWaited = new Set() // chats whose first read was waited for once on this visit
const BUSY_WORDS = {
  steer: 'go into the running turn at its next step',
  queue: 'wait until the running turn ends',
  interrupt: 'stop the running turn and start on them'
}
const WHY_WORDS = { subscription: 'Covered by your sign-ins', free: 'Free', local: 'On this machine' }
const LEVEL_WORDS = { none: 'Off', minimal: 'Minimal', low: 'Low', medium: 'Medium', high: 'High',
  xhigh: 'Extra high', max: 'Max', ultra: 'Ultra' }

function changeLine(c, name) {
  if (!c) return null
  const what = [c.set && c.set.model ? 'model' : null, c.set && c.set.reasoning ? 'thinking' : null]
    .filter(Boolean).join(' and ')
  if (c.status === 'queued') {
    return el('p', { class: 'receipt', role: 'status' }, `Changing its ${what}. ${name} does it the next time it checks in.`)
  }
  if (c.status === 'done') return el('p', { class: 'receipt', role: 'status' }, `Its ${what} changed.`)
  if (c.status === 'deferred') return el('p', { class: 'receipt', role: 'status' }, c.reason)
  return el('p', { class: 'warn', role: 'status' }, `Its ${what} did not change: ${c.reason || 'no reason given'}`)
}

function modelName(st) {
  const o = st.offered.find(x => x.model === st.model && x.provider === st.provider)
  return st.model ? `${st.model}${o ? ' (' + o.name + ')' : st.provider ? ' (' + st.provider + ')' : ''}` : 'Not known'
}

// The details of one chat, as nodes for a pane or a sheet. `again` draws them again.
async function detailsPart(gw, sid, close, again) {
  const name = GATEWAY[gw]
  const path = `/own/${gw}/chats/${encodeURIComponent(sid)}/control`
  const key = `${gw}:${sid}`
  // The first look at a chat waits up to 20 seconds for its connector's read; a redraw never waits.
  let r = await call('GET', path + (detailWaited.has(key) ? '' : '?wait=20'))
  detailWaited.add(key)
  const head = screenBar({ title: 'Details', heading: true,
    right: el('button', { class: 'ib', type: 'button', 'aria-label': 'Close details', onclick: close }, icon('x')) })
  if (r.status === 401) { csrf = null; return [head, el('p', { class: 'warn', role: 'status' }, said(r.body))] }
  if (r.status !== 200) return [head, el('p', { class: 'warn', role: 'status' }, r.body.message || said(r.body))]
  const v = r.body
  if (!v.state) {
    return [head, el('p', { class: 'quiet', role: 'status' }, `${name} has not said yet. This fills in once it does.`),
      changeLine(v.change, name)]
  }
  const st = v.state
  const note = el('p', { class: 'warn', role: 'status' })
  const busy = v.change && v.change.status === 'queued'
  // The model: his current one first, then what can be picked, by why it costs nothing per message.
  const pick = el('select', { 'aria-label': 'Model for this chat', 'data-detail': 'model' })
  const here = st.offered.some(o => o.model === st.model && o.provider === st.provider)
  if (!here) pick.append(el('option', { value: '', selected: '' }, `${st.model || 'Not known'} (now)`))
  for (const why of ['subscription', 'free', 'local']) {
    const rows = st.offered.filter(o => o.why === why)
    if (!rows.length) continue
    pick.append(el('optgroup', { label: WHY_WORDS[why] }, rows.map(o => {
      const opt = el('option', { value: JSON.stringify([o.provider, o.model]) }, `${o.model} (${o.name})`)
      if (o.model === st.model && o.provider === st.provider) opt.selected = true
      return opt
    })))
  }
  const level = el('select', { 'aria-label': 'Thinking for this chat', 'data-detail': 'reasoning' },
    (st.levels.length ? st.levels : [st.reasoning].filter(Boolean)).map(lv => {
      const opt = el('option', { value: lv }, LEVEL_WORDS[lv] || lv)
      if (lv === st.reasoning) opt.selected = true
      return opt
    }))
  async function save() {
    const body = {}
    if (pick.value) {
      const [provider, model] = JSON.parse(pick.value)
      if (model !== st.model || provider !== st.provider) body.model = { provider, model }
    }
    if (level.value && level.value !== st.reasoning) body.reasoning = level.value
    if (!body.model && !body.reasoning) { note.textContent = 'Nothing is different from what the chat has now.'; return }
    if (!v.can_change) {
      const s = await passkeySignIn()
      if (s.status !== 200) { note.textContent = said(s.body); return }
    }
    const k = JSON.stringify(body)
    detailKeys[key + k] = detailKeys[key + k] || newKey()
    note.textContent = 'Asking...'
    const c = await call('POST', path, { ...body, idempotency_key: detailKeys[key + k] })
    if (c.status === 200) { delete detailKeys[key + k]; return again() }
    if (c.status === 401) { csrf = null; return signInView(said(c.body)) }
    note.textContent = c.status === 0
      ? 'Could not reach the core. Asking again sends the same change, never a second one.'
      : (c.body.message || said(c.body))
  }
  for (const box of [pick, level]) box.addEventListener('change', () => { box.dataset.dirty = '' })
  const row = (label, ...nodes) => el('label', { class: 'detail' }, el('span', {}, label), ...nodes)
  return [head,
    el('div', { class: 'details' },
      row('Model', pick),
      row('Thinking', level),
      el('button', { class: 'primary', type: 'button', 'data-detail': 'save', disabled: busy ? '' : null, onclick: save },
        v.can_change ? 'Change' : `Change (with your ${signInWord()})`),
      changeLine(v.change, name),
      note,
      el('p', { 'data-detail': 'approvals' }, st.yolo
        ? 'Approvals: skipped in this chat. Commands run without asking you.'
        : 'Approvals: it asks you before a command its rules flag.'),
      st.busy ? el('p', { 'data-detail': 'busy' }, `Messages sent while it works ${BUSY_WORDS[st.busy]}.`) : null,
      el('p', { 'data-detail': 'context' }, contextWords(st)),
      el('p', { 'data-detail': 'tokens' }, tokenWords(st)))]
}

// How full a chat's context is and the tokens it used, as Hermes Desktop's context meter says them
// (own_control.py): "About 41% full: 82K of 200K tokens", "estimated" before a turn has measured it.
function bigNumber(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace(/\.0$/, '') + 'M'
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1).replace(/\.0$/, '') + 'K'
  return String(n)
}
function contextWords(st) {
  const c = st.context
  if (!c) return 'Context: not known yet' + (st.context_why ? ` (${st.context_why})` : '') + '.'
  return `Context: ${c.estimated ? 'about ' : ''}${c.percent}% full, ${bigNumber(c.used)} of ${bigNumber(c.max)} tokens.`
}
function tokenWords(st) {
  const u = st.usage
  if (!u) return 'Tokens used: not known yet' + (st.usage_why ? ` (${st.usage_why})` : '') + '.'
  const squeezed = u.compressions ? `, compressed ${u.compressions} time${u.compressions === 1 ? '' : 's'}` : ''
  return `Tokens used: ${bigNumber(u.input)} in, ${bigNumber(u.output)} out, over ${u.calls} call${u.calls === 1 ? '' : 's'}${squeezed}.`
}

// On a wide screen: the details on the right, in place of Decisions.
async function detailsView(into, gw, sid) {
  const seqNow = viewSeq
  const nodes = await detailsPart(gw, sid, () => { details.open = false; route() }, () => route())
  if (seqNow === viewSeq || into !== 'view') showIn(into, nodes)
}

// On a phone (or a window too narrow for the right pane): the details in a sheet. While a change waits,
// the sheet reads again every two seconds, as the poll holds while a sheet is open.
async function detailsSheet(gw, sid) {
  closeSheet()
  const sheet = el('div', { class: 'sheet', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Chat details' })
  const back = el('div', { class: 'sheet-back' }, sheet)
  back.addEventListener('click', e => { if (e.target === back) closeSheet() })
  back.addEventListener('keydown', e => { if (e.key === 'Escape') closeSheet() })
  sheet.append(el('p', { class: 'quiet', role: 'status' }, 'Reading from ' + GATEWAY[gw] + '...'))
  document.body.append(back)
  async function fill() {
    if (!back.isConnected) return
    sheet.replaceChildren(...(await detailsPart(gw, sid, closeSheet, fill)).flat().filter(Boolean))
    if (sheet.querySelector('[role="status"].receipt') && /^Changing/.test(sheet.querySelector('.receipt').textContent)) {
      setTimeout(fill, 2000)
    }
  }
  await fill()
  const first = sheet.querySelector('select, button')
  if (first) first.focus()
}

function openDetails(gw, sid) {
  if (WIDE.matches && WIDEST.matches) { details.open = true; folded.side = false; return route() }
  return detailsSheet(gw, sid)
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
    chatBar(() => go('/m/hermes/' + gw), 'All chats on ' + name, 'Hidden chats', undefined, gw, name)
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
// With `sid`, one session of the Bot instead (the one a Board row opens): the
// feed has no copy of it, so it shows as Hermes's first page comes.
async function chatView(target, tab, sid) {
  setTab(tab)
  const seqNow = ++viewSeq
  const inSession = sid ? '?session=' + encodeURIComponent(sid) : ''
  const [r, w] = await Promise.all([call('GET', '/chat/' + encodeURIComponent(target) + inSession),
    sid ? call('GET', '/work') : null])
  if (refused(r)) return
  const v = r.body
  const room = v.kind === 'room'
  const name = room ? v.name : (NAMES[target] || target)
  const key = sid ? `session:${v.target}:${sid}` : 'chat:' + v.target
  // The Bot's own turn in this session, from the Board, while it runs.
  const work = sid && w && w.status === 200 && w.body.live && w.body.work && Array.isArray(w.body.work.doing)
    ? w.body.work.doing.find(d => d.bot === v.target && d.session_id === sid) : null
  const pages = `/chat/${encodeURIComponent(v.target)}/messages` + inSession
  const more = pages + (sid ? '&' : '?')
  const t = threads[key] || (threads[key] = kept(key) || { newest: null, older: [], olderMore: false, note: '', error: null })
  const idOf = m => room ? m.seq : m.mid
  const status = el('p', { class: 'warn', role: 'status' })
  function history() {
    if (sid && t.newest && t.newest.gone) {
      return { notes: [el('p', { class: 'quiet' }, `This session is not one of ${name}'s.`)], items: [], keyed: false }
    }
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
        const p = await ownPage(`${more}before=${idOf(raw[0])}`)
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
    const noSession = `A reaction goes on ${name}'s Bot Chat only.`
    const items = raw.map(m => room
      ? { mine: m.kind === 'message.user', who: m.actor || 'someone', at: m.at, text: m.text, mid: m.seq, noReact: noRoom }
      : sid ? { mine: m.role === 'user', who: name, agent: name, at: m.at, text: m.text, mid: m.mid, noReact: noSession }
        : { mine: m.role === 'user', who: name, agent: name, at: m.at, text: m.text, mid: m.mid, reactions: m.reactions })
    if (!room && !sid) {
      withReactions(items, v.reactions, t.newest.handed_at ? new Date(t.newest.handed_at) : null, v.can_send
        ? (mid, emoji) => reactTo(`/chat/${encodeURIComponent(v.target)}/react`, {}, mid, emoji, status) : null,
        said({ error: 'chat_write_off' }))
    }
    return { notes, items, keyed: true }
  }
  function render() {
    const readAt = v.as_of ? new Date(v.as_of) : null
    const h = history()
    const items = h.items.concat(pendingItems(v.mine, readAt, room ? null : name))
    const top = [
      sid ? chatBar(() => go('/m/board'), 'Board', name, work && work.title ? work.title : 'Session', v.target, name,
        null, botLive(v.live))
        : chatBar(() => go(room ? '/m/rooms' : '/m/bots'), room ? 'All rooms' : 'All Thornswood Bots', name, undefined,
          room ? null : v.target, name, null, room ? null : botLive(v.live)),
      h.notes
    ]
    if (sid && !t.newest) top.push(el('p', { class: 'quiet', role: 'status' }, 'Reading this session from Hermes...'))
    else if (!items.length && !(sid && t.newest && t.newest.gone)) top.push(el('p', { class: 'quiet' }, 'Nothing here yet.'))
    const dock = v.can_send
      ? [chatComposer(sid ? key : target, 'Message to ' + name, 'Send to ' + name,
          (text, k, reply, files, answer) => call('POST', '/chat/' + encodeURIComponent(target),
            { text, idempotency_key: k, ...(answer ? { answer } : {}), ...(sid ? { session: sid } : {}),
              ...(files.length ? { attachments: files } : {}) }),
          status, null, '/chat/' + encodeURIComponent(target), v.cards), status]
      : [el('p', { class: 'quiet' }, said({ error: 'chat_write_off' }))]
    // His message a Bot is answering now: the Bot's live line under it, kept current while the turn runs. In a
    // session, the Bot's own turn there shows the same way while it runs, as on the Board.
    const turn = room ? null : v.mine.find(m => m.status === 'leased')
    const list = [bubbles(items), turn ? workingItem(name, v.target, turn.doing, turn.id) : work ? workingItem(name, v.target, work) : null,
      room ? null : chatCards(v.cards, location.pathname)]
    if (seqNow === viewSeq) {
      showChat(key, top, list, dock, items)
      if (turn && !room) watchLive(turn.id, seqNow)
      const waits = room ? null : turn || v.mine.find(m => m.status === 'queued')
      if (waits) watchTurn('/chat/' + encodeURIComponent(v.target) + inSession, seqNow, waits.id, name, waits.status)
    }
  }
  render()
  const p = await ownPage(pages)
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
      '"Hermes needs you" on the lock screen; a chat shows who answered, or that your Hermes asks you ' +
      'something; and nothing more.'))
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

// Where a key is kept, in his words: the phone itself (its fingerprint) or the passkey app that holds it.
function keptIn(k) {
  if (k.phone_key) return 'this phone\'s fingerprint'
  return k.maker ? `kept in ${k.maker}` : 'kept in a passkey app'
}

// One recent sign-in, said whole (10 Nov: a new chat asked for the passkey through Bitwarden instead of the
// fingerprint, and only the core's database said which way it had asked).
function signInSaid(s) {
  const asked = s.asked === 'fingerprint' ? 'Asked for the fingerprint' : 'Asked for any passkey'
  if (s.outcome === 'signed_in') return `${asked}; signed in with ${s.key ? `${s.key.label} (${keptIn(s.key)})` : 'a key since removed'}`
  if (s.outcome === 'no_phone_key') return 'Asked for the fingerprint, but the core no longer had this phone\'s key, so it asked for any passkey'
  if (s.outcome === 'phone_key_in_app') return 'Asked for the fingerprint, but that key was kept in a passkey app, so it asked for any passkey'
  if (s.outcome === 'ended') return `${asked}; ended on the phone (${s.why})`
  if (s.outcome === 'refused') return `${asked}; refused (${s.why})`
  return `${asked}; no answer came back`
}

async function settingsView(note) {
  setTab('')
  const status = el('p', { class: 'quiet', role: 'status' }, note || '')
  const list = el('div', {})
  const r = await call('GET', '/passkeys')
  if (r.status === 401) return signInView(said(r.body))
  if (r.status === 200) {
    list.append(...(r.body.passkeys.length ? r.body.passkeys.map(k => el('div', { class: 'option' },
      el('strong', {}, k.label),
      el('div', { class: 'quiet' }, [keptIn(k), `added ${when(k.created_at)}`,
        k.last_used_at ? `last signed in ${when(k.last_used_at)}` : 'never signed in'].join(' · '))))
      : [el('p', { class: 'quiet' }, 'No passkey yet. Add one so this phone can sign in again.')]))
  }
  const recent = r.status === 200 && (r.body.sign_ins || []).length
    ? [el('h2', {}, 'Recent sign-ins'),
       el('p', { class: 'quiet' }, 'What the app asked for each time, and which key answered.'),
       ...r.body.sign_ins.map(s => el('div', { class: 'option' },
         el('strong', {}, signInSaid(s)), el('div', { class: 'quiet' }, when(s.at))))]
    : null
  const name = el('input', { autocomplete: 'off', placeholder: 'This phone', 'aria-label': 'Passkey name' })
  const alerts = await alertsPart(status)
  // This phone's fingerprint key: kept only while the core still lists it.
  let held = phoneKey()
  if (held && r.status === 200 && !r.body.passkeys.some(k => k.id === held.passkey_id)) {
    lostPhoneKey()
    flash = ''
    held = null
    status.textContent = "This phone's fingerprint sign-in is no longer on the core, so your passkey signs in. Add it again here."
  }
  const tried = !held && r.status === 200 && (await canUseFingerprint()) && (await triedFingerprint())
  const finger = held
    ? [el('p', { class: 'quiet' }, 'On. This phone signs in and answers high-risk cards with your fingerprint.')]
    : (await canUseFingerprint())
      ? [el('p', { class: 'quiet' }, tried
          ? 'This phone saved its fingerprint key in your passkey app, so you sign in through that app. ' +
            'To sign in right here instead, try again and pick Google Password Manager when Android asks where to save it.'
          : 'Sign in with your fingerprint right here instead of going out to your passkey app. ' +
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
    el('h2', {}, 'Voice'), wakeSetting(),
    el('h2', {}, 'Passkeys'), list, recent, name,
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

// ── voice (Patrick, 2 Nov) ────────────────────────────────────────────────
//
// Dictation, Read aloud, voice chat and "Hey Hermes", all at no cost: the words come from the VM's own
// Whisper (POST /voice/transcribe, voice.py and voice_worker.py), and reading aloud is this phone's own
// speech (speechSynthesis). The microphone opens only after a tap (the microphone in the box, Voice chat
// in a chat's bar) or with "Hey Hermes" turned on in Settings, only while the app is the page in front,
// and the bar under the top says so the whole time it is open. Hiding the app or locking the phone stops
// listening and reading at once, so nothing is read out on a locked phone. "Hey Hermes" listens only in
// an open chat, never in the background.

const WAKE_KEY = 'hermes.wake.v1'
// "Hey Hermes", as Whisper writes it down: a greeting, then the name, then what he asks.
const WAKE = /^[\s"'“]*(?:hey|hi|hello|okay|ok)[\s,.!-]+hermes\b[\s,.!?:;-]*/i
const voice = { gen: 0, mode: null, state: null, key: null, path: null, name: '', queue: [], heard: null,
  fromWake: false, paused: false, rec: null }
const mic = { stream: null, ctx: null, an: null, buf: null }

function canRecord() {
  return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder)
}

function canSpeak() {
  return 'speechSynthesis' in window && 'SpeechSynthesisUtterance' in window
}

function wakeOn() {
  try { return localStorage.getItem(WAKE_KEY) === '1' } catch (e) { return false }
}

async function micOn() {
  if (mic.stream) return null
  try {
    mic.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true,
      autoGainControl: true } })
  } catch (e) {
    return e.name === 'NotAllowedError'
      ? 'The microphone is blocked for this app. Allow it in the site settings for this app, then try again.'
      : e.name === 'NotFoundError' ? 'This phone has no microphone the app can use.'
        : 'The microphone did not start (' + (e.name || 'no reason given') + ').'
  }
  const AC = window.AudioContext || window.webkitAudioContext
  if (AC) {
    mic.ctx = new AC()
    mic.an = mic.ctx.createAnalyser()
    mic.an.fftSize = 2048
    mic.ctx.createMediaStreamSource(mic.stream).connect(mic.an)
    if (mic.ctx.state !== 'running') { try { await mic.ctx.resume() } catch (e) {} }
  }
  return null
}

function micOff() {
  if (mic.stream) for (const t of mic.stream.getTracks()) t.stop()
  if (mic.ctx) mic.ctx.close().catch(() => {})
  Object.assign(mic, { stream: null, ctx: null, an: null, buf: null })
}

// How loud the room is now (the root mean square of the last few hundredths of a second).
function level() {
  if (!mic.an || !mic.ctx || mic.ctx.state !== 'running') return 0
  const buf = mic.buf || (mic.buf = new Float32Array(mic.an.fftSize))
  mic.an.getFloatTimeDomainData(buf)
  let s = 0
  for (const v of buf) s += v * v
  return Math.sqrt(s / buf.length)
}

// Whether this phone can tell where speech starts and ends: voice chat and "Hey Hermes" need it.
function hearsSpeech() {
  return !!(mic.an && mic.ctx && mic.ctx.state === 'running')
}

function recType() {
  for (const t of ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4', 'audio/webm']) {
    if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t)) return t
  }
  return ''
}

// One recording from the open microphone. It records at once and hears where speech starts and ends: the
// loudness against the room's own level, through WebAudio's analyser (nothing loaded, nothing sent). It
// gives the recording once he has been quiet for `quiet` ms after speaking, or Done was tapped (rec.done),
// or `most` ms passed. Nothing said for `wait` ms (0: no limit) gives null, as does rec.drop().
function utterance({ quiet = 1500, most = 120000, wait = 0 } = {}) {
  return new Promise(resolve => {
    const type = recType()
    const rec = new MediaRecorder(mic.stream, type ? { mimeType: type } : undefined)
    const chunks = []
    const started = Date.now()
    let floor = null, loud = 0, lastLoud = 0, keep = false
    const r = { spoke: false }
    const stop = k => { keep = k; if (rec.state !== 'inactive') rec.stop() }
    r.done = () => stop(true)
    r.drop = () => stop(false)
    rec.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data) }
    rec.onstop = () => {
      clearInterval(tick)
      if (voice.rec === r) voice.rec = null
      resolve(keep && chunks.length ? new Blob(chunks, { type: (rec.mimeType || type || 'audio/webm').split(';')[0] }) : null)
    }
    const tick = setInterval(() => {
      const now = Date.now()
      const l = level()
      if (floor === null) floor = l
      const gate = Math.max(0.015, floor * 3)
      if (l > gate) {
        loud += 50
        lastLoud = now
        if (loud >= 150 && !r.spoke) { r.spoke = true; paintVoice() }
      } else {
        loud = 0
        if (!r.spoke) floor = floor * 0.9 + l * 0.1
      }
      if (r.spoke && now - lastLoud > quiet) stop(true)
      else if (now - started > most) stop(r.spoke || voice.mode === 'dictate')
      else if (wait && !r.spoke && now - started > wait) stop(false)
    }, 50)
    voice.rec = r
    rec.start(250)
  })
}

async function transcribe(blob) {
  const headers = { 'Content-Type': blob.type || 'audio/webm' }
  if (csrf) headers['X-CSRF-Token'] = csrf
  try {
    const resp = await fetch(API + '/voice/transcribe', { method: 'POST', headers, credentials: 'same-origin',
      cache: 'no-store', body: blob })
    let out = {}
    try { out = await resp.json() } catch (e) { out = {} }
    return { status: resp.status, body: out }
  } catch (e) {
    return { status: 0, body: { error: 'offline' } }
  }
}

// Reading aloud, on this phone, a sentence or so at a time (a long run in one go can stop partway).
function pieces(text) {
  const out = []
  for (const s of String(text).replace(/\s+/g, ' ').trim().split(/(?<=[.!?])\s+/)) {
    for (let rest = s; rest; ) {
      const cut = rest.length <= 220 ? rest.length : Math.max(rest.lastIndexOf(' ', 220), 1)
      out.push(rest.slice(0, cut).trim())
      rest = rest.slice(cut).trim()
    }
  }
  return out.filter(Boolean)
}

async function speakText(text, g) {
  if (!canSpeak()) return false
  for (const p of pieces(text)) {
    if (g !== voice.gen) return false
    const ok = await new Promise(resolve => {
      const u = new SpeechSynthesisUtterance(p)
      u.lang = navigator.language || 'en-US'
      u.onend = () => resolve(true)
      u.onerror = () => resolve(false)
      speechSynthesis.speak(u)
    })
    if (!ok) return false
  }
  return g === voice.gen
}

// What a message says, to read out: his own words or a reply's, as plain words.
function spoken(m) {
  const a = authored(m.text)
  return plain(a.words || '').replace(/\s+/g, ' ').trim()
}

// The bar under the top: there the whole time the microphone is open or the phone reads aloud.
function paintVoice() {
  let bar = document.getElementById('voicebar')
  if (!voice.mode) {
    if (bar) bar.hidden = true
    paintVoiceButtons()
    return
  }
  if (!bar) {
    bar = el('div', { id: 'voicebar', class: 'voicebar', role: 'status' })
    document.querySelector('header.top').after(bar)
  }
  const n = voice.name || 'Hermes'
  const hearing = voice.rec && voice.rec.spoke
  const words = {
    'dictate:listening': hearing ? 'Hearing you. Tap Done when you finish.' : 'Listening. Speak, then tap Done.',
    'dictate:writing': 'Writing down what you said...',
    'chat:listening': hearing ? `Voice chat with ${n}: hearing you` : `Voice chat with ${n}: listening`,
    'chat:writing': `Voice chat with ${n}: writing down what you said...`,
    'chat:waiting': `Voice chat with ${n}: waiting for the answer`,
    'chat:speaking': `Voice chat with ${n}: reading the answer aloud`,
    'wake:listening': hearing ? 'Hearing you. Listening for "Hey Hermes"' : 'Listening for "Hey Hermes"',
    'wake:writing': 'Listening for "Hey Hermes"',
    'wake:tap': '"Hey Hermes" is on. This phone wants a tap before it listens.',
    'read:speaking': 'Reading aloud'
  }[voice.mode + ':' + voice.state] || ''
  const buttons = []
  if (voice.mode === 'wake' && voice.state === 'tap') {
    buttons.push(el('button', { class: 'primary', type: 'button', onclick: async () => {
      if (mic.ctx) { try { await mic.ctx.resume() } catch (e) {} }
      wakeLoop(voice.gen)
    } }, 'Listen'))
  }
  if (voice.mode === 'dictate' && voice.state === 'listening') {
    buttons.push(el('button', { class: 'primary', type: 'button', onclick: () => voice.rec && voice.rec.done() }, 'Done'))
  }
  buttons.push(el('button', { class: 'secondary', type: 'button', onclick: () => voiceStop(true) },
    voice.mode === 'dictate' ? 'Cancel' : 'Stop'))
  bar.className = 'voicebar' + (voice.state === 'listening' ? ' hot' : '') + (hearing ? ' hearing' : '')
  bar.dataset.mode = voice.mode + ':' + voice.state
  bar.replaceChildren(el('span', { class: 'vdot', 'aria-hidden': 'true' }), el('span', { class: 'vt' }, words), ...buttons)
  bar.hidden = false
  paintVoiceButtons()
}

function paintVoiceButtons() {
  for (const b of document.querySelectorAll('#view .composer .mic')) {
    b.setAttribute('aria-pressed', String(voice.mode === 'dictate'))
  }
  for (const b of document.querySelectorAll('#view .chatbar .talk')) {
    b.setAttribute('aria-pressed', String(voice.mode === 'chat'))
  }
}

// Everything stops: the recording is dropped, the reading cut off, the microphone closed. Stop tapped
// (byHim) also keeps "Hey Hermes" quiet until he next comes back to the app.
function voiceStop(byHim) {
  voice.gen++
  if (voice.rec) voice.rec.drop()
  if (canSpeak()) speechSynthesis.cancel()
  micOff()
  Object.assign(voice, { mode: null, state: null, key: null, path: null, queue: [], heard: null, fromWake: false, rec: null })
  if (byHim) voice.paused = true
  paintVoice()
}

function voiceState(state) {
  voice.state = state
  paintVoice()
}

// The open chat's box: its draft's key, who it goes to, where it says how things went, and its send.
function openComposer() {
  const c = document.querySelector('#view .dock .composer')
  return c && c.voice ? c.voice : null
}

// The microphone in the box: what he says goes into the box, where he reads it before sending.
async function dictate(c) {
  if (voice.mode === 'dictate') { if (voice.rec) voice.rec.done(); return }
  if (voice.mode) voiceStop(false)
  const g = voice.gen
  Object.assign(voice, { mode: 'dictate', key: c.key, path: location.pathname, name: c.name })
  voiceState('listening')
  const no = await micOn()
  if (g !== voice.gen) return
  if (no) { voiceStop(false); c.status.textContent = no; return }
  const blob = await utterance({ quiet: 2500, most: 240000 })
  if (g !== voice.gen) return
  micOff()
  if (!blob) { voiceStop(false); c.status.textContent = 'Nothing was recorded.'; return wakeCheck() }
  voiceState('writing')
  const r = await transcribe(blob)
  if (g !== voice.gen) return
  voiceStop(false)
  if (r.status === 401) { csrf = null; return signInView(said(r.body)) }
  if (r.status !== 200) { c.status.textContent = r.status === 0 ? 'Could not reach the core. Nothing was written down.' : said(r.body); return wakeCheck() }
  const text = String(r.body.text || '').trim()
  if (!text) c.status.textContent = 'Whisper heard no words in that.'
  else {
    const now = openComposer()
    ;(now && now.key === c.key ? now : c).add(text)
  }
  wakeCheck()
}

// Voice chat in the open chat: he talks, his words are sent, the answer is read aloud, and it listens again.
async function talk(c, first) {
  if (voice.mode === 'chat') return voiceStop(true)
  if (voice.mode === 'wake') {
    // From "Hey Hermes", or the button while it listens: the microphone stays open.
    if (voice.rec) voice.rec.drop()
    voice.gen++
  } else if (voice.mode) voiceStop(false)
  const g = voice.gen
  Object.assign(voice, { mode: 'chat', key: c.key, path: location.pathname, name: c.name, queue: [],
    heard: new Set(lastItems.key === c.chat ? lastItems.items.filter(m => !m.mine).map(m => String(m.text).trim()) : []),
    fromWake: !!first })
  voiceState('listening')
  const no = await micOn()
  if (g !== voice.gen) return
  if (no) { voiceStop(false); c.status.textContent = no; return }
  if (!hearsSpeech()) {
    voiceStop(false)
    c.status.textContent = 'This phone cannot tell here where you stop speaking, so voice chat cannot work. The microphone in the box still writes down what you say.'
    return
  }
  if (first) return sendHeard(g, first)
  listen(g)
}

async function listen(g) {
  if (g !== voice.gen || voice.mode !== 'chat') return
  if (voice.queue.length) return speakQueue(g)
  voiceState('listening')
  // Started by "Hey Hermes": a minute with nothing said goes back to listening for it.
  const blob = await utterance({ quiet: 1300, most: 60000, wait: voice.fromWake ? 60000 : 30000 })
  if (g !== voice.gen || voice.mode !== 'chat' || voice.state !== 'listening') return
  if (!blob) {
    if (voice.fromWake) return backToWake()
    return listen(g)
  }
  voiceState('writing')
  const r = await transcribe(blob)
  if (g !== voice.gen) return
  if (r.status !== 200) return voiceFailed(r)
  const text = String(r.body.text || '').trim()
  if (!text) return listen(g)
  sendHeard(g, text)
}

async function sendHeard(g, text) {
  const c = openComposer()
  if (!c || c.key !== voice.key) return voiceStop(false)
  voiceState('waiting')
  const sent = await c.send(text)
  if (g !== voice.gen) return
  if (sent === 'card') {
    // Words that answer a card are never sent by voice alone: they wait in the box, with the line over it.
    voiceStop(true)
    c.status.textContent = 'What you said answers a card. Read the line over the box, then send it or take the answer off.'
    return
  }
  if (!sent) return voiceStop(false)
  if (voice.queue.length) speakQueue(g)
}

function voiceFailed(r) {
  const c = openComposer()
  voiceStop(false)
  if (r.status === 401) { csrf = null; return signInView(said(r.body)) }
  if (c) c.status.textContent = r.status === 0 ? 'Could not reach the core. Voice chat stopped.' : said(r.body)
}

async function speakQueue(g) {
  if (voice.state === 'speaking') return
  voiceState('speaking')
  while (voice.queue.length && g === voice.gen) {
    const text = voice.queue.shift()
    if (!await speakText(text, g)) break
  }
  if (g === voice.gen && voice.mode === 'chat') listen(g)
}

// Each time a chat is drawn: in voice chat, the new answers in it are read aloud. One that comes while he
// has not started speaking is read at once; one that comes while he speaks waits until his words are sent.
let lastItems = { key: null, items: [] }
function voiceSaw(key, items) {
  lastItems = { key, items }
  if (voice.mode !== 'chat' || !voice.heard) return
  const c = openComposer()
  if (!c || c.chat !== key || c.key !== voice.key) return
  for (const m of items) {
    if (m.mine || m.status || !m.text) continue
    const sig = String(m.text).trim()
    if (voice.heard.has(sig)) continue
    voice.heard.add(sig)
    const words = spoken(m)
    if (words) voice.queue.push(words)
  }
  if (!voice.queue.length) return
  if (voice.state === 'waiting') speakQueue(voice.gen)
  else if (voice.state === 'listening' && voice.rec && !voice.rec.spoke) { voice.state = 'queued'; voice.rec.drop(); speakQueue(voice.gen) }
}

// "Hey Hermes": on only when he turned it on in Settings, only in an open chat, only while the app is in
// front, and never after he tapped Stop until he next comes back to the app. Each thing said is written
// down by the VM's Whisper; only words that start with "Hey Hermes" do anything.
function wakeCheck() {
  if (!wakeOn() || voice.paused || voice.mode || document.visibilityState !== 'visible' || !canRecord()) return
  const c = openComposer()
  if (!c) return
  wakeStart(c)
}

async function wakeStart(c) {
  const g = voice.gen
  Object.assign(voice, { mode: 'wake', key: c.key, path: location.pathname, name: c.name })
  voiceState('listening')
  const no = await micOn()
  if (g !== voice.gen) return
  if (no) { voiceStop(true); c.status.textContent = '"Hey Hermes" cannot listen: ' + no; return }
  if (!mic.an) { voiceStop(true); c.status.textContent = 'This phone cannot tell here where you stop speaking, so "Hey Hermes" cannot work.'; return }
  wakeLoop(g)
}

async function wakeLoop(g) {
  if (g !== voice.gen || voice.mode !== 'wake') return
  if (!hearsSpeech()) return voiceState('tap')
  voiceState('listening')
  const blob = await utterance({ quiet: 1000, most: 20000, wait: 20000 })
  if (g !== voice.gen || voice.mode !== 'wake') return
  if (!blob) return wakeLoop(g)
  voiceState('writing')
  const r = await transcribe(blob)
  if (g !== voice.gen || voice.mode !== 'wake') return
  if (r.status !== 200) return voiceFailed(r)
  const text = String(r.body.text || '')
  const m = text.match(WAKE)
  if (!m) return wakeLoop(g)
  const c = openComposer()
  if (!c) return voiceStop(false)
  talk(c, text.slice(m[0].length).trim() || null)
}

// A voice chat "Hey Hermes" started, with nothing said for a minute: back to listening for it.
function backToWake() {
  if (!wakeOn() || document.visibilityState !== 'visible') return voiceStop(false)
  if (voice.rec) voice.rec.drop()
  if (canSpeak()) speechSynthesis.cancel()
  const g = ++voice.gen
  Object.assign(voice, { mode: 'wake', queue: [], heard: null, fromWake: false })
  wakeLoop(g)
}

// Leaving the page a voice chat or a dictation is on stops it; "Hey Hermes" starts again in the next chat.
function voiceLeft() {
  if (voice.mode && voice.path !== location.pathname) voiceStop(false)
}

// Voice chat's button in an open chat's bar.
function voiceButtons() {
  const bar = document.querySelector('#view .chatbar')
  const c = openComposer()
  if (!bar || !c || !canRecord() || bar.querySelector('.talk')) return
  const b = el('button', { class: 'ib talk', type: 'button', 'aria-label': 'Voice chat', 'aria-pressed': String(voice.mode === 'chat'),
    onclick: () => { const now = openComposer(); if (now) talk(now) } }, icon('talk'))
  bar.querySelector('.bar-tools').prepend(b)
}

function readAloud(m) {
  if (voice.mode) voiceStop(false)
  const g = voice.gen
  Object.assign(voice, { mode: 'read', path: location.pathname })
  voiceState('speaking')
  speakText(spoken(m), g).then(() => { if (g === voice.gen) { voiceStop(false); wakeCheck() } })
}

function wakeSetting() {
  const on = wakeOn()
  const note = el('p', { class: 'quiet', role: 'status' })
  return [
    el('p', { class: 'quiet' }, 'The microphone in a chat\'s box writes down what you say with Whisper on the VM, ' +
      'and Voice chat in a chat\'s bar talks with it. Read aloud is in a message\'s actions.'),
    el('p', { class: 'quiet' }, on
      ? '"Hey Hermes" is on for this phone: while a chat is open and the app is in front, it listens, with the bar ' +
        'at the top saying so. It never listens in the background or with the phone locked.'
      : '"Hey Hermes" is off. Turned on, it listens while a chat is open and the app is in front, with the bar at ' +
        'the top saying so. It never listens in the background or with the phone locked.'),
    canRecord() ? el('button', { class: on ? 'secondary' : 'primary', type: 'button', onclick: () => {
      try {
        if (on) localStorage.removeItem(WAKE_KEY)
        else localStorage.setItem(WAKE_KEY, '1')
      } catch (e) {
        note.textContent = 'This phone did not keep the setting.'
        return
      }
      if (on && voice.mode === 'wake') voiceStop(false)
      voice.paused = false
      settingsView(on ? '"Hey Hermes" is off.' : '"Hey Hermes" is on. Open a chat to use it.')
    } }, on ? 'Turn "Hey Hermes" off' : 'Turn "Hey Hermes" on') : el('p', { class: 'warn' }, 'This browser cannot record, so voice does not work here.'),
    note
  ]
}

// ── routing and polling ───────────────────────────────────────────────────

function badge(n) {
  const b = document.getElementById('badge')
  b.hidden = !n
  b.textContent = n ? String(n) : ''
}

// The badge read again at once after an answer made away from Decisions (a Bot Chat), not at the next poll.
async function badgeNow() {
  const list = await call('GET', '/cards')
  if (list.status === 200) badge(list.body.cards.length)
}

function go(path) {
  history.pushState(null, '', path)
  newPage = true
  route()
}

// A chat an alert can open (alerts.py PATH, sw.js CHAT_PATH): a Bot's chat or session, or one of his own.
const CHAT_PATH = /^\/m\/(bots\/[a-z0-9][a-z0-9_-]{0,63}(\/s\/[A-Za-z0-9][A-Za-z0-9_.:-]{0,127})?|hermes\/(me|one)(\/[A-Za-z0-9][A-Za-z0-9_.:-]{0,127})?)$/

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
  voiceLeft()
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
  // A chat he opens takes its alert off the phone (alerts.py, sw.js).
  if (CHAT_PATH.test(location.pathname)) toWorker({ opened: location.pathname })
  let path = location.pathname.replace(/^\/m\/?/, '').replace(/\/$/, '')
  // The tabs' first names, from an old alert or home screen link.
  const FIRST = { 'needs-you': 'decisions', work: 'board' }
  if (FIRST[path]) { path = FIRST[path]; history.replaceState(null, '', '/m/' + path) }
  // On a wide screen the list beside a chat and Decisions on the right are drawn with it.
  const d = desk(path)
  const drawn = [d.pick ? pickView(path) : mainView(path)]
  if (d.list) drawn.push(d.list('list').then(() => markCurrent(path)))
  const ownChat = path.match(/^hermes\/(me|one)\/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})$/)
  if (!ownChat) details.open = false
  if (d.side) drawn.push(details.open && ownChat ? detailsView('side', ownChat[1], ownChat[2]) : decisionsView('side'))
  document.getElementById('side').setAttribute('aria-label', details.open && ownChat ? 'Chat details' : 'Decisions')
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
  if (path === 'tasks') return tasksView()
  const bot = path.match(/^bots\/([a-z0-9][a-z0-9_-]{0,63})$/)
  if (bot) return chatView(bot[1], 'bots')
  const session = path.match(/^bots\/([a-z0-9][a-z0-9_-]{0,63})\/s\/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})$/)
  if (session) return botSessionView(session[1], session[2])
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
  const writing = [...document.querySelectorAll('textarea')].some(t => t.value && !t.matches(chatBox)) ||
    !!document.querySelector('select[data-dirty]')
  // The Bots and rooms lists too: a Bot's answer moves its row to the top and shows as its line.
  if (/^\/m\/(bramble$|room$|rooms$|rooms\/|bots$|bots\/|hermes\/)/.test(path)) {
    const other = document.activeElement && /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) &&
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
  if (document.visibilityState === 'visible') {
    poll()
    if (csrf) checkAlerts()
    voice.paused = false
    wakeCheck()
  } else {
    // Hidden or locked: no listening and nothing read out.
    voiceStop(false)
  }
})
addEventListener('pagehide', () => voiceStop(false))
window.addEventListener('beforeinstallprompt', event => {
  event.preventDefault()
  installPrompt = event
  installBar()
})
window.addEventListener('appinstalled', () => { installPrompt = null; installBar() })
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/m/sw.js', { scope: '/m/' }).catch(() => {})
  // A tapped alert, with the app already open: the worker says which card, or which chat.
  navigator.serviceWorker.addEventListener('message', event => {
    const p = event.data && event.data.go
    if (typeof p === 'string' && (/^\/m\/(decisions|decisions\/[A-Za-z0-9_-]{1,64}|needs-you)$/.test(p) || CHAT_PATH.test(p))) go(p)
  })
}
pollTimer = setInterval(poll, POLL_MS)
// The Board is live: read again every 3 seconds while it is the page open and nothing is being answered.
setInterval(() => {
  if (csrf && document.visibilityState === 'visible' && !answering && location.pathname === '/m/board' &&
    !document.querySelector('.sheet-back')) boardView()
}, LIVE_MS)
// Tasks are read again every 5 seconds while they are the page open: the connectors report every 20 or 30.
setInterval(() => {
  if (csrf && document.visibilityState === 'visible' && !answering && location.pathname === '/m/tasks' &&
    !document.querySelector('.sheet-back')) tasksView()
}, 5000)
installBar()
route()
