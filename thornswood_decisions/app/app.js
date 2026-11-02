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
  const view = document.getElementById('view')
  view.replaceChildren(...nodes.flat(Infinity).filter(Boolean))
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
  more: 'M12 5h.01M12 12h.01M12 19h.01'
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
function screenBar({ back, backLabel, agent, label, title, heading, under, right }) {
  return el('div', { class: 'bar' },
    back ? el('button', { class: 'ib', type: 'button', 'aria-label': backLabel, onclick: back }, icon('back')) : null,
    agent !== undefined ? avatar(agent, label || title) : null,
    el('div', { class: 't' }, el(heading ? 'h2' : 'strong', {}, title), under ? el('span', {}, under) : null),
    right || null)
}

// A row of a list: avatar, title with its tags, the lines under it, and what
// goes on the right (a state or a time).
function listRow(attrs, { agent, label, title, tags, lines, right }) {
  return el('button', { type: 'button', ...attrs, class: 'row' + (attrs.class ? ' ' + attrs.class : '') },
    agent !== undefined ? avatar(agent, label || title) : null,
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

async function passkeySignIn() {
  const o = await call('POST', '/passkeys/sign-in/options')
  if (o.status !== 200) return o
  let cred
  try {
    cred = await navigator.credentials.get({ publicKey: requestOptions(o.body) })
  } catch (err) {
    return { status: -1, body: { message: 'The passkey did not sign in. Nothing changed.' } }
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
  } }, 'Sign in with your passkey')
  const auto = hasKey === true && askOnOpen
  askOnOpen = false
  show(
    el('h2', {}, 'Welcome back'),
    el('p', { class: 'quiet' }, hasKey ? 'Your passkey is set up.' : 'Sign in with your passkey, or set up this phone below.'),
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
  for (const a of document.querySelectorAll('#tabs a')) {
    if (a.dataset.tab === name) a.setAttribute('aria-current', 'page')
    else a.removeAttribute('aria-current')
  }
}

function refused(r) {
  if (r.status === 401) { csrf = null; signInView(said(r.body)); return true }
  if (r.status !== 200) { show(el('p', { class: 'warn' }, said(r.body))); return true }
  return false
}

async function decisionsView() {
  setTab('decisions')
  const r = await call('GET', '/cards')
  if (refused(r)) return
  seq = r.body.seq
  badge(r.body.cards.length)
  // Every alert of a card that no longer needs Patrick goes off the phone.
  toWorker({ pending: r.body.cards.map(c => c.id) })
  const note = flash
  flash = ''
  const answered = r.body.answered || []
  show(
    screenBar({ title: 'Decisions', heading: true }),
    note ? el('p', { class: 'receipt', role: 'status' }, note) : null,
    r.body.cards.length
      ? [el('h2', { class: 'section' }, `Waiting on you (${r.body.cards.length})`),
        r.body.cards.map(c => listRow({ onclick: () => go('/m/decisions/' + c.id) }, {
          title: c.title, tags: c.risk === 'high' ? el('span', { class: 'risk' }, 'high risk') : null,
          lines: el('span', { class: 'quiet' }, `${c.kind}, answer by ${when(c.expires_at)}`) }))]
      : el('p', { class: 'quiet' }, 'Nothing needs you right now.'),
    answered.length ? el('h2', { class: 'section' }, 'Answered') : null,
    answered.map(c => listRow({ class: 'done', onclick: () => go('/m/decisions/' + c.id) }, {
      title: c.title, lines: el('span', { class: 'quiet' }, `You chose ${c.option}, ${when(c.answered_at)}`) })))
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
    el('h2', {}, c.title),
    el('p', { class: 'quiet' }, `${c.kind}, revision ${c.revision}, ${c.status}${c.risk === 'high' ? ', high risk' : ''}`),
    a.status === 200 ? el('pre', { class: 'artifact' }, a.body.body) : el('p', { class: 'warn' }, said(a.body)),
    el('p', {}, el('strong', {}, 'What yes covers: '), c.scope_of_yes),
    c.options.map(o => el('div', { class: 'option' },
      el('strong', {}, o.label), o.key === c.recommendation ? el('span', { class: 'rec' }, 'recommended') : null,
      el('div', { class: 'quiet' }, o.consequence),
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

// One option chosen: a high-risk card shows what is being approved on its own
// screen, and the passkey prompt opens over it by itself (Patrick, 1 Nov). The
// passkey is the yes: it signs in afresh, then the answer goes. If the prompt is
// dismissed or fails, its button shows; nothing is answered until it succeeds.
async function choose(c, o, status) {
  if (c.risk !== 'high') return answer(c, o, status)
  const note = el('p', { class: 'warn', role: 'status' })
  const button = el('button', { class: 'primary', type: 'button', 'data-confirm': o.key, onclick: () =>
    promptNow(button, note, () => answer(c, o, status)) }, 'Answer ' + o.label + ' with your passkey')
  show(el('div', { class: 'card' },
    el('h2', {}, 'Check this answer'),
    el('p', {}, `"${c.title}" is high risk. You are choosing:`),
    el('p', {}, el('strong', {}, o.label)),
    el('p', { class: 'quiet' }, o.consequence),
    el('p', {}, el('strong', {}, 'What yes covers: '), c.scope_of_yes),
    el('p', { class: 'quiet' }, 'Your passkey answers it. Cancel the prompt to change your mind.'),
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

// An answered card leaves Decisions at once: its alert closes, and the list
// says what was chosen and keeps the card under Answered for a day.
function answered(c, o) {
  toWorker({ close: c.id })
  flash = `Answered "${c.title}": ${o.label}. It is under Answered below.`
  history.replaceState(null, '', '/m/decisions')
  return route()
}

async function boardView() {
  setTab('board')
  const r = await call('GET', '/work')
  if (refused(r)) return
  const bar = screenBar({ title: 'Board', heading: true })
  if (!r.body.work) return show(bar, el('p', { class: 'quiet' }, 'No work has come in from the Bots yet.'))
  const tasks = r.body.work.tasks
  show(bar,
    el('p', { class: 'quiet' }, `As the Bots' Hermes reported it at ${when(r.body.as_of)}. Read only.`),
    tasks.length ? el('table', {},
      el('tr', {}, el('th', {}, 'Task'), el('th', {}, 'Who'), el('th', {}, 'State')),
      tasks.map(t => el('tr', {}, el('td', {}, t.title), el('td', {}, t.assignee), el('td', {}, t.status))))
      : el('p', { class: 'quiet' }, 'No open tasks.'))
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

async function botsView() {
  setTab('bots')
  const [r, own] = await Promise.all([call('GET', '/chat'), call('GET', '/own')])
  if (refused(r)) return
  const rank = b => (ORDER.indexOf(b.bot) + 1) || ORDER.length + 1
  const bots = r.body.bots.slice().sort((a, b) => rank(a) - rank(b))
  // Hermes, then One, then the six Bots (Patrick's mock-up answer, 1 Nov).
  const gateways = own.status === 200 ? ['me', 'one'].map(gw => own.body.gateways.find(g => g.gateway === gw)).filter(Boolean) : []
  show(
    screenBar({ title: 'Thornswood Bots', heading: true }),
    gateways.map(g => listRow({ 'data-gateway': g.gateway, onclick: () => go('/m/hermes/' + g.gateway) }, {
      agent: g.gateway, title: GATEWAY[g.gateway],
      lines: [el('span', { class: 'what' }, GATEWAY_WHAT[g.gateway]),
        el('span', { class: 'quiet' }, g.last ? `${g.last.title || 'Untitled chat'}: ${g.last.preview.slice(0, 100)}`
          : (g.as_of ? 'No chats yet.' : 'Its chats show here once it is turned on.'))],
      right: [gatewayLine(g), g.unsettled ? el('span', { class: 'rec' }, `${g.unsettled} waiting`) : null] })),
    own.status === 200 ? null : el('p', { class: 'warn' }, said(own.body)),
    bots.map(b => listRow({ 'data-bot': b.bot, onclick: () => go('/m/bots/' + b.bot) }, {
      agent: b.bot, title: NAMES[b.bot] || b.bot,
      lines: [WHAT[b.bot] ? el('span', { class: 'what' }, WHAT[b.bot]) : null,
        el('span', { class: 'quiet' }, b.last ? b.last.text.slice(0, 120) : 'No messages yet.')],
      right: b.unsettled ? el('span', { class: 'rec' }, `${b.unsettled} waiting`) : null })),
    el('p', { class: 'quiet foot' }, `The Bots' chats as of ${when(r.body.as_of) || 'nothing yet'}.`))
}

// Sending to his own Hermes needs a passkey sign-in from the last 30 minutes
// (Patrick, 1 Nov). This says until when, or offers that sign-in. A new chat is
// made with full power; Hermes keeps a chat's own approval setting for good, so
// in a chat started elsewhere a step that needs an approval is refused unless
// that chat has /yolo on.
function signInPart(v, status, existing) {
  if (v.sign_in_until) {
    const until = new Date(v.sign_in_until).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
    return el('p', { class: 'quiet' }, existing
      ? `You can send until ${until}, 30 minutes after your passkey sign-in. A step that needs an approval runs only if this chat was started from the phone or has /yolo on.`
      : `Full power until ${until}, 30 minutes after your passkey sign-in.`)
  }
  return el('div', { class: 'card' },
    el('p', { class: 'quiet' }, said({ error: 'passkey_needed' })),
    el('button', { class: 'secondary', type: 'button', onclick: async () => {
      const s = await passkeySignIn()
      if (s.status === 200) return route()
      status.textContent = said(s.body)
    } }, 'Sign in with your passkey'))
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
  '(?<!\\w)_(?=[^\\s_])([^_\\n]+?)(?<=\\S)_(?!\\w)'               // 9 italic, never inside a_name
].join('|'), 'g')

function link(text, href) {
  return el('a', { href, target: '_blank', rel: 'noopener noreferrer' }, text)
}

function inlineMd(text) {
  const out = []
  let last = 0
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) out.push(text.slice(last, m.index))
    if (m[1] !== undefined) out.push(el('code', {}, m[1]))
    else if (m[2] !== undefined) out.push(link(m[2], m[3]))
    else if (m[4] !== undefined) out.push(link(m[4], m[4]))
    else if (m[5] !== undefined || m[6] !== undefined) out.push(el('strong', {}, inlineMd(m[5] ?? m[6])))
    else if (m[7] !== undefined) out.push(el('s', {}, inlineMd(m[7])))
    else out.push(el('em', {}, inlineMd(m[8] ?? m[9])))
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
    const bub = el('div', { class: 'bub' }, markdown(m.text))
    if (m.mid !== undefined && m.mid !== null && !m.status) {
      // Tapping a message (not a link or a button in it, and not while selecting text) opens its actions.
      bub.tabIndex = 0
      bub.title = 'Tap for reactions and copy'
      bub.addEventListener('click', e => {
        if (e.target.closest('a, button') || String(getSelection())) return
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
      await navigator.clipboard.writeText(m.text)
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
function chatBar(back, backLabel, title, under, agent, label, more) {
  const bar = screenBar({ back, backLabel, agent, label, title, under,
    right: more ? el('button', { class: 'ib', type: 'button', 'aria-label': 'Chat actions', onclick: more }, icon('more')) : null })
  bar.classList.add('chatbar')
  return bar
}

// The box at the foot of a chat. It grows with what is typed, up to six
// lines, and keeps its draft (and the draft's idempotency key) while the
// chat redraws. Enter is a new line; the button sends.
function chatComposer(key, label, buttonLabel, send, status, gw) {
  const draft = drafts[key] || (drafts[key] = { text: '', key: newKey() })
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
    if (!text) { status.textContent = 'Type a message first.'; return }
    status.textContent = 'Sending...'
    button.disabled = true
    const s = await send(text, draft.key)
    button.disabled = false
    if (s.status === 200) { drafts[key] = null; chatPlace.toEnd = true; return route() }
    if (s.status === 401) { csrf = null; return signInView(said(s.body)) }
    const d = s.body.detail || {}
    status.textContent = s.status === 0
      ? 'Could not reach the core. Sending again sends the same message, never a second one.'
      : (gw && d.state ? stateWords({ gateway: gw, ...d })[1] : said(s.body))
  } }, icon('send'))
  return el('div', { class: 'composer' }, box, button)
}

// Where a chat is scrolled. Opening a chat, or sending, goes to the newest
// message. A redraw while Patrick reads further up keeps his place and says
// how many came in below. Older messages keep the one he was looking at.
const chatPlace = { key: null, seen: null, toEnd: false, keep: null }

function nearEnd() {
  return innerHeight + scrollY >= document.documentElement.scrollHeight - 120
}

function showChat(key, top, list, dock, items) {
  const opening = chatPlace.key !== key
  const wasNearEnd = !opening && nearEnd()
  const mids = items.map(m => String(m.mid))
  let fresh = 0
  if (!opening && !wasNearEnd && !chatPlace.toEnd && chatPlace.seen !== null) {
    const at = mids.lastIndexOf(chatPlace.seen)
    fresh = at === -1 ? 0 : mids.length - 1 - at
  }
  const toEnd = opening || wasNearEnd || chatPlace.toEnd
  const keep = chatPlace.keep
  const row = fresh ? el('button', { class: 'newrow', type: 'button', onclick: () => {
    row.remove()
    chatPlace.seen = mids[mids.length - 1] ?? null
    scrollTo({ top: document.documentElement.scrollHeight })
  } }, fresh === 1 ? '1 new message' : `${fresh} new messages`) : null
  show(el('div', { class: 'chat' }, top, el('div', { class: 'msgs' }, list), el('div', { class: 'dock' }, row, dock)))
  chatPlace.key = key
  chatPlace.toEnd = false
  chatPlace.keep = null
  if (toEnd) chatPlace.seen = mids[mids.length - 1] ?? null
  if (keep) {
    const n = document.querySelector(`#view .msg[data-mid="${CSS.escape(keep)}"]`)
    if (n) n.scrollIntoView({ block: 'start' })
  } else if (toEnd) {
    scrollTo({ top: document.documentElement.scrollHeight })
  }
}

// ── full history of his own chats (step 2, plan Part 4) ────────────────────
//
// A page comes from Hermes itself: the core asks that gateway's connector, which
// reads Hermes's state.db and posts it back within about 5 seconds. The core holds
// it in memory only. The app keeps what it fetched only while it is open.

let viewSeq = 0          // each view, from route() and each chat render; a page that comes back late fills only its own
const pickers = {}       // gw -> { q, results, more, extra }
const threads = {}       // gw:sid -> { newest, older, olderMore, note }

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
    title: c.title || 'Untitled chat', tags: c.bot_chat ? el('span', { class: 'rec' }, 'Bot Mode') : null,
    lines: [el('span', { class: 'quiet' }, c.preview.slice(0, 120)), o && inFlight(o) ? opLine(o, GATEWAY[gw]) : null],
    right: at(c.last_at) || when(c.last_at) })
}

async function gatewayView(gw) {
  setTab('bots')
  const seqNow = ++viewSeq
  const r = await call('GET', '/own/' + gw)
  if (refused(r)) return
  const v = r.body
  const name = GATEWAY[gw]
  const pick = pickers[gw] || (pickers[gw] = { q: '', results: null, more: false, extra: [] })
  const status = el('p', { class: 'warn', role: 'status' })
  const parts = [
    screenBar({ back: () => go('/m/bots'), backLabel: 'All Thornswood Bots', agent: gw, title: name, heading: true,
      under: GATEWAY_WHAT[gw], right: gatewayLine(v) }),
    el('p', { class: 'quiet' }, `Your chats, as of ${when(v.as_of) || 'nothing yet'}. A chat is never an answer to a card.`),
    offLine(v)
  ]
  if (v.can_send && v.online) {
    parts.push(signInPart(v, status))
    if (v.sign_in_until) {
      parts.push(el('p', { class: 'quiet' }, 'New chat is the only way a new conversation starts. It has full power on every turn, and Hermes names it.'),
        composer('new:' + gw, 'Start a new chat',
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
  if (seqNow === viewSeq) show(parts)
}

// What Hermes holds of one of his own chats: notes to show above it, its
// messages as bubbles, and the Older messages button when there are more.
function historyPart(gw, sid, t, name) {
  if (t.error) return { notes: [el('p', { class: 'warn' }, t.error)], items: [], gone: false }
  if (!t.newest) {
    return { notes: [el('p', { class: 'quiet' }, `Asking ${name} for this chat. It shows here within half a minute.`)], items: [], gone: false }
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
function pendingItems(mine, readAt) {
  return mine.filter(m => m.status !== 'sent' || !readAt || new Date(m.updated_at) > readAt)
    .map(m => ({ mine: true, at: m.created_at, text: m.text, status: m.status,
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
  const t = threads[key] || (threads[key] = { newest: null, older: [], olderMore: false, note: '', error: null })
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
    const h = historyPart(gw, sid, t, name)
    const canReact = v.can_send && v.online && !h.gone && !t.error
    withReactions(h.items, v.reactions, pageAt, canReact ? (mid, emoji) => reactTo(`/own/${gw}/react`, { session_id: sid }, mid, emoji, status) : null,
      `${name} is not answering, so a reaction cannot be set now.`)
    const items = h.items.concat(pendingItems(v.mine, readAt))
    // A rename, hide or delete asked for here: how it stands, and once a hide or a delete is done, so it says.
    const o = lastOp(v.ops, sid)
    const ended = o && o.status === 'done' && (o.op === 'delete' || o.op === 'archive') ? o.op : null
    // His Bot Chat is kept as it is, so it offers no Rename, Hide or Delete (the core refuses them too).
    const manage = v.can_manage && v.online && !h.gone && !t.error && !ended && !(o && inFlight(o)) && !v.chat.bot_chat
    const top = [
      chatBar(() => go('/m/hermes/' + gw), 'All chats on ' + name, v.chat.title || 'Untitled chat',
        `${name}, ${GATEWAY_WHAT[gw]}. A chat is never an answer to a card.`, gw, name,
        manage ? () => chatSheet(gw, sid, v.chat.title, v) : null),
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
        dock.push(signInPart(v, status, true), chatComposer(`own:${gw}:${sid}`, 'Message to ' + name, 'Send to ' + name,
          (text, k) => call('POST', `/own/${gw}/send`, { session_id: sid, text, idempotency_key: k }), status, gw), status)
      } else {
        dock.push(signInPart(v, status, true), status)
      }
    } else if (!v.can_send) {
      dock.push(el('p', { class: 'quiet' }, said({ error: 'own_chat_off' })))
    }
    if (seqNow === viewSeq) showChat('own:' + key, top, bubbles(items), dock, items)
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
      signed ? null : el('p', { class: 'quiet' }, 'Deleting needs your passkey. Its prompt opens when you press Delete for good.'),
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
    chatBar(() => go('/m/hermes/' + gw), 'All chats on ' + name, 'Hidden chats', `${name}, ${GATEWAY_WHAT[gw]}`, gw, name),
    el('p', { class: 'quiet' }, `The chats you hid on ${name}, with every message kept. Bring back puts one in the chat list again.`),
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
          el('strong', {}, c.title || 'Untitled chat'),
          el('span', { class: 'quiet' }, (c.preview || '').slice(0, 120)),
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

async function roomsView() {
  setTab('rooms')
  const r = await call('GET', '/chat')
  if (refused(r)) return
  const rooms = r.body.rooms || []
  show(
    screenBar({ title: 'Rooms', heading: true, under: `Each group chat, as of ${when(r.body.rooms_as_of) || 'nothing yet'}` }),
    rooms.length ? rooms.map(m => listRow({ onclick: () => go('/m/rooms/' + m.room_id) }, {
      agent: null, label: m.name, title: m.name,
      tags: m.unsettled ? el('span', { class: 'rec' }, `${m.unsettled} waiting`) : null,
      lines: el('span', { class: 'quiet' }, m.last ? `${m.last.who}: ${m.last.text.slice(0, 120)}` : 'No messages yet.') }))
      : el('p', { class: 'quiet' }, 'No rooms yet.'))
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
  const t = threads[key] || (threads[key] = { newest: null, older: [], olderMore: false, note: '', error: null })
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
      chatBar(() => go(room ? '/m/rooms' : '/m/bots'), room ? 'All rooms' : 'All Thornswood Bots', name, room
        ? `A room, as of ${when(v.as_of) || 'nothing yet'}. What is said here is never an answer to a card.`
        : `Your Bot Chat, as of ${when(v.as_of) || 'nothing yet'}. A chat is never an answer to a card.`, room ? null : v.target, name),
      h.notes
    ]
    if (!items.length) top.push(el('p', { class: 'quiet' }, 'Nothing here yet.'))
    const dock = v.can_send
      ? [chatComposer(target, 'Message to ' + name, 'Send to ' + name,
          (text, k) => call('POST', '/chat/' + encodeURIComponent(target), { text, idempotency_key: k }), status), status]
      : [el('p', { class: 'quiet' }, said({ error: 'chat_write_off' }))]
    if (seqNow === viewSeq) showChat(key, top, bubbles(items), dock, items)
  }
  render()
  const p = await ownPage(`/chat/${encodeURIComponent(v.target)}/messages`)
  if (seqNow !== viewSeq || p.status !== 200 || p.body.pending) return
  t.newest = p.body
  // Never redrawn under someone typing; the next read shows it.
  const typing = document.activeElement && /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)
  if (!typing) render()
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
  show(
    el('h2', {}, 'Alerts'), alerts,
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
  route()
}

async function route() {
  // Every route is a new view, so a page a chat asked for and gets back after
  // Patrick left it fills nothing (it once redrew a room over the Rooms list).
  viewSeq++
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
  // A chat view reads itself again, unless something is being typed.
  const box = document.querySelector('textarea')
  if (/^\/m\/(bramble$|room$|rooms\/|bots\/|hermes\/)/.test(path)) {
    const typing = document.activeElement && /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)
    if ((!box || !box.value) && !typing) route()
    return
  }
  const r = await call('GET', '/changes?since=' + seq)
  if (r.status !== 200 || r.body.seq === seq) return
  seq = r.body.seq
  const card = path.match(/^\/m\/decisions\/([A-Za-z0-9_-]{1,64})$/)
  if (card && r.body.changed.includes(card[1]) && !document.querySelector('[data-confirm]')) return route()
  if (/^\/m\/(decisions)?$/.test(path)) return route()
  // Elsewhere, the badge still follows the list.
  const list = await call('GET', '/cards')
  if (list.status === 200) badge(list.body.cards.length)
}

document.addEventListener('click', event => {
  const a = event.target.closest('a[data-tab]')
  if (a) { event.preventDefault(); go(a.getAttribute('href')) }
})
document.getElementById('settings').addEventListener('click', () => go('/m/settings'))
window.addEventListener('popstate', route)
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
