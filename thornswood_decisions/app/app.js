// The Hermes phone app (07 sections 4, 5 and 8; step 3 read only, step 4
// answers and chat, step 5 alerts). Its tabs: Bramble, Bots, Rooms, Board and
// Decisions (first named Room, Work and Needs you; those paths still open).
// The Bots tab holds the three gateways Desktop's picker shows (Patrick, 1 Nov):
// thornswood-bots with its Bots, and his own Hermes, my-hermes on the VM and
// his Hermes on One, each with its own chats.
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
  passkey_needed: 'A message to your own Hermes runs with full power, so it needs a passkey sign-in from the last 30 minutes.',
  one_off: 'One is off',
  no_gateway: 'There is no such gateway.',
  stale_revision: 'This card changed since you opened it. Read it again before you answer.',
  already_answered: 'This card was already answered.',
  expired: 'This card ran out. Bramble opens a new one if it still matters.',
  no_chat: 'There is no such chat.',
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

function signInView(note) {
  document.getElementById('tabs').hidden = true
  document.getElementById('settings').hidden = true
  const status = el('p', { class: 'quiet' }, note || '')
  const code = el('input', { autocomplete: 'off', inputmode: 'text', placeholder: 'XXXX-XXXX', 'aria-label': 'Code' })
  const recovery = el('input', { autocomplete: 'off', placeholder: 'Recovery code', 'aria-label': 'Recovery code' })
  const label = el('input', { autocomplete: 'off', placeholder: 'This phone', 'aria-label': 'Name for this phone' })
  async function done(r) {
    if (r.status !== 200) { status.textContent = said(r.body); return }
    csrf = r.body.csrf
    route()
  }
  show(
    el('h2', {}, 'Sign in'),
    el('button', { class: 'primary', type: 'button', onclick: async () => {
      status.textContent = ''
      done(await passkeySignIn())
    } }, 'Sign in with your passkey'),
    el('div', { class: 'card' },
      el('h2', {}, 'First time on this phone'),
      el('p', { class: 'quiet' }, 'Type the one-time code the VM or your Desktop gave you. It works once, for 10 minutes.'),
      code,
      el('button', { class: 'secondary', type: 'button', onclick: async () => {
        done(await call('POST', '/pair', { code: code.value.trim() }))
      } }, 'Pair this phone')),
    el('div', { class: 'card' },
      el('h2', {}, 'Lost your passkey'),
      el('p', { class: 'quiet' }, 'A recovery code signs in once and makes this phone a new device. Add a passkey right after.'),
      recovery, label,
      el('button', { class: 'secondary', type: 'button', onclick: async () => {
        done(await call('POST', '/recovery-sign-in', { code: recovery.value.trim(), label: label.value.trim() }))
      } }, 'Sign in with a recovery code')),
    status)
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
    note ? el('p', { class: 'receipt', role: 'status' }, note) : null,
    r.body.cards.length
      ? r.body.cards.map(c => el('button', { class: 'row', type: 'button', onclick: () => go('/m/decisions/' + c.id) },
        el('strong', {}, c.title),
        el('div', { class: 'quiet' }, `${c.kind}${c.risk === 'high' ? ', high risk' : ''}, answer by ${when(c.expires_at)}`)))
      : el('p', { class: 'quiet' }, 'Nothing needs you right now.'),
    answered.length ? el('h2', { class: 'section' }, 'Answered') : null,
    answered.map(c => el('button', { class: 'row done', type: 'button', onclick: () => go('/m/decisions/' + c.id) },
      el('strong', {}, c.title),
      el('div', { class: 'quiet' }, `You chose ${c.option}, ${when(c.answered_at)}`))))
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

// One option chosen: a high-risk card asks again on its own screen first.
function choose(c, o, status) {
  if (c.risk !== 'high') return answer(c, o, status)
  show(el('div', { class: 'card' },
    el('h2', {}, 'Check this answer'),
    el('p', {}, `"${c.title}" is high risk. You are choosing:`),
    el('p', {}, el('strong', {}, o.label)),
    el('p', { class: 'quiet' }, o.consequence),
    el('p', {}, el('strong', {}, 'What yes covers: '), c.scope_of_yes),
    el('p', { class: 'quiet' }, 'A high-risk answer needs a passkey sign-in from the last five minutes.'),
    el('button', { class: 'primary', type: 'button', 'data-confirm': o.key, onclick: () => answer(c, o, status) },
      'Yes, answer ' + o.label),
    el('button', { class: 'secondary', type: 'button', onclick: () => cardView(c.id) }, 'Go back to the card')))
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
    return show(el('div', { class: 'card' },
      el('p', {}, said(r.body)),
      el('button', { class: 'primary', type: 'button', onclick: async () => {
        const s = await passkeySignIn()
        if (s.status !== 200) return cardView(c.id, said(s.body))
        answer(c, o, status)
      } }, 'Sign in again and answer'),
      el('button', { class: 'secondary', type: 'button', onclick: () => cardView(c.id) }, 'Go back to the card')))
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
  if (!r.body.work) return show(el('p', { class: 'quiet' }, 'No work has come in from the Bots yet.'))
  const tasks = r.body.work.tasks
  show(
    el('p', { class: 'quiet' }, `As the Bots' Hermes reported it at ${when(r.body.as_of)}. Read only.`),
    tasks.length ? el('table', {},
      el('tr', {}, el('th', {}, 'Task'), el('th', {}, 'Who'), el('th', {}, 'State')),
      tasks.map(t => el('tr', {}, el('td', {}, t.title), el('td', {}, t.assignee), el('td', {}, t.status))))
      : el('p', { class: 'quiet' }, 'No open tasks.'))
}

const NAMES = { bramble: 'Bramble', rift: 'Rift', sprocket: 'Sprocket', echo: 'Echo', magpie: 'Magpie', wisp: 'Wisp' }
const STATE = {
  queued: 'Waiting to send',
  leased: 'Sending',
  sent: 'Sent',
  uncertain: 'Not sure this got there. Look above before you send it again.',
  dead: 'Not sent',
  quarantined: 'Held back after a restore; not sent'
}
let drafts = {}

// Patrick's own Hermes: the gateway's name as Desktop's picker shows it.
const GATEWAY = { me: 'my-hermes', one: 'One (This device)' }

function gatewayLine(g) {
  if (g.online) return el('span', { class: 'rec' }, 'On')
  return el('span', { class: 'rec off' }, g.gateway === 'one' ? 'One is off' : 'Off')
}

async function botsView() {
  setTab('bots')
  const [r, own] = await Promise.all([call('GET', '/chat'), call('GET', '/own')])
  if (refused(r)) return
  const others = r.body.bots.filter(b => b.bot !== 'bramble')
  // In the order Desktop's picker shows them: This device (One), my-hermes, then thornswood-bots.
  const gateways = own.status === 200 ? ['one', 'me'].map(gw => own.body.gateways.find(g => g.gateway === gw)).filter(Boolean) : []
  show(
    gateways.map(g => el('button', { class: 'row', type: 'button', onclick: () => go('/m/hermes/' + g.gateway) },
      el('strong', {}, GATEWAY[g.gateway] || g.name), gatewayLine(g),
      g.unsettled ? el('span', { class: 'rec' }, `${g.unsettled} waiting`) : null,
      el('div', { class: 'quiet' }, g.last ? `${g.last.title || 'Untitled chat'}: ${g.last.preview.slice(0, 100)}`
        : (g.as_of ? 'No chats yet.' : 'Its chats show here once it is turned on.')))),
    el('h2', { class: 'section' }, 'thornswood-bots'),
    el('p', { class: 'quiet' }, `Each Bot's own Bot Chat, as of ${when(r.body.as_of) || 'nothing yet'}.`),
    others.map(b => el('button', { class: 'row', type: 'button', onclick: () => go('/m/bots/' + b.bot) },
      el('strong', {}, NAMES[b.bot] || b.bot),
      b.unsettled ? el('span', { class: 'rec' }, `${b.unsettled} waiting`) : null,
      el('div', { class: 'quiet' }, b.last ? b.last.text.slice(0, 120) : 'No messages yet.'))))
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

function composer(key, label, send, status) {
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
    status.textContent = s.status === 0
      ? 'Could not reach the core. Sending again sends the same message, never a second one.'
      : said(s.body) + (s.body.detail && s.body.detail.why ? ` (${s.body.detail.why})` : '')
  } }, label)
  return [box, button]
}

function offLine(v) {
  if (v.online) return null
  return el('p', { class: 'warn' }, (v.gateway === 'one' ? 'One is off' : 'my-hermes is not answering') +
    (v.off_why ? `: ${v.off_why}.` : '.'))
}

async function gatewayView(gw) {
  setTab('bots')
  const r = await call('GET', '/own/' + gw)
  if (refused(r)) return
  const v = r.body
  const name = GATEWAY[gw]
  const status = el('p', { class: 'warn', role: 'status' })
  const parts = [
    el('h2', {}, name),
    el('p', { class: 'quiet' }, `Your chats, as of ${when(v.as_of) || 'nothing yet'}. A chat is never an answer to a card.`),
    offLine(v)
  ]
  if (v.can_send && v.online) {
    parts.push(signInPart(v, status))
    if (v.sign_in_until) {
      parts.push(el('p', { class: 'quiet' }, 'A new chat has full power on every turn. Hermes names it.'),
        composer('new:' + gw, 'Start a new chat',
          (text, key) => call('POST', `/own/${gw}/send`, { new: true, text, idempotency_key: key }), status), status)
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
  parts.push(el('h2', { class: 'section' }, 'Chats'),
    v.chats.length ? v.chats.map(c => el('button', { class: 'row', type: 'button',
      onclick: () => go(`/m/hermes/${gw}/${c.id}`) },
      el('strong', {}, c.title || 'Untitled chat'),
      el('span', { class: 'quiet' }, '  ' + when(c.last_at)),
      el('div', { class: 'quiet' }, c.preview.slice(0, 120))))
      : el('p', { class: 'quiet' }, 'No chats yet.'))
  show(parts)
}

async function ownChatView(gw, sid) {
  setTab('bots')
  const r = await call('GET', `/own/${gw}/${encodeURIComponent(sid)}`)
  if (refused(r)) return
  const v = r.body
  const name = GATEWAY[gw]
  const asOf = v.as_of ? new Date(v.as_of) : null
  const status = el('p', { class: 'warn', role: 'status' })
  const history = v.history.map(m => el('div', { class: 'msg' },
    el('div', { class: 'who' }, m.who, el('span', { class: 'quiet' }, '  ' + when(m.at))),
    el('div', { class: 'text' }, m.text)))
  const mine = v.mine.filter(m => m.status !== 'sent' || !asOf || new Date(m.updated_at) > asOf)
    .map(m => el('div', { class: 'msg mine', 'data-status': m.status },
      el('div', { class: 'who' }, 'You', el('span', { class: 'quiet' }, '  ' + when(m.created_at))),
      el('div', { class: 'text' }, m.text),
      el('div', { class: m.status === 'dead' || m.status === 'uncertain' ? 'warn' : 'quiet' },
        STATE[m.status] + ((m.status === 'dead' || m.status === 'uncertain') && m.reason ? ': ' + m.reason : ''))))
  const parts = [
    el('button', { class: 'secondary', type: 'button', onclick: () => go('/m/hermes/' + gw) }, 'All chats on ' + name),
    el('h2', {}, v.chat.title || 'Untitled chat'),
    el('p', { class: 'quiet' }, `On ${name}, as of ${when(v.as_of) || 'nothing yet'}. A chat is never an answer to a card.`),
    v.loaded ? (history.length ? history : el('p', { class: 'quiet' }, 'Nothing here yet.'))
      : el('p', { class: 'quiet' }, `Asking ${name} for this chat. It shows here within half a minute.`),
    mine,
    offLine(v)
  ]
  if (v.can_send && v.online) {
    parts.push(signInPart(v, status, true))
    if (v.sign_in_until) {
      parts.push(composer(`own:${gw}:${sid}`, 'Send to ' + name,
        (text, key) => call('POST', `/own/${gw}/send`, { session_id: sid, text, idempotency_key: key }), status), status)
    }
  } else if (!v.can_send) {
    parts.push(el('p', { class: 'quiet' }, said({ error: 'own_chat_off' })))
  }
  show(parts)
}

async function roomsView() {
  setTab('rooms')
  const r = await call('GET', '/chat')
  if (refused(r)) return
  const rooms = r.body.rooms || []
  show(
    el('p', { class: 'quiet' }, `Each group chat, as of ${when(r.body.rooms_as_of) || 'nothing yet'}.`),
    rooms.length ? rooms.map(m => el('button', { class: 'row', type: 'button', onclick: () => go('/m/rooms/' + m.room_id) },
      el('strong', {}, m.name),
      m.unsettled ? el('span', { class: 'rec' }, `${m.unsettled} waiting`) : null,
      el('div', { class: 'quiet' }, m.last ? `${m.last.who}: ${m.last.text.slice(0, 120)}` : 'No messages yet.')))
      : el('p', { class: 'quiet' }, 'No rooms yet.'))
}

async function chatView(target, tab) {
  setTab(tab)
  const r = await call('GET', '/chat/' + encodeURIComponent(target))
  if (refused(r)) return
  const v = r.body
  const room = v.kind === 'room'
  const name = room ? v.name : (NAMES[target] || target)
  const asOf = v.as_of ? new Date(v.as_of) : null
  const history = v.history.map(m => el('div', { class: 'msg' },
    el('div', { class: 'who' }, m.who, el('span', { class: 'quiet' }, '  ' + when(m.at))),
    el('div', { class: 'text' }, m.text)))
  // My own messages, until the chat above shows them.
  const mine = v.mine.filter(m => m.status !== 'sent' || !asOf || new Date(m.updated_at) > asOf)
    .map(m => el('div', { class: 'msg mine', 'data-status': m.status },
      el('div', { class: 'who' }, 'You', el('span', { class: 'quiet' }, '  ' + when(m.created_at))),
      el('div', { class: 'text' }, m.text),
      el('div', { class: m.status === 'dead' || m.status === 'uncertain' ? 'warn' : 'quiet' },
        STATE[m.status] + (m.status === 'dead' && m.reason ? ': ' + m.reason : ''))))
  const status = el('p', { class: 'warn', role: 'status' })
  const parts = [
    el('p', { class: 'quiet' }, room
      ? `${name}, as of ${when(v.as_of) || 'nothing yet'}. What is said here is never an answer to a card.`
      : `Your Bot Chat with ${name}, as of ${when(v.as_of) || 'nothing yet'}. A chat is never an answer to a card.`),
    history.length ? history : el('p', { class: 'quiet' }, 'Nothing here yet.'),
    mine
  ]
  if (v.can_send) {
    const draft = drafts[target] || (drafts[target] = { text: '', key: newKey() })
    const box = el('textarea', { rows: '3', 'aria-label': 'Message to ' + name, placeholder: 'Message ' + name })
    box.value = draft.text
    box.addEventListener('input', () => {
      if (box.value !== draft.text) { draft.text = box.value; draft.key = newKey() }
    })
    parts.push(box, el('button', { class: 'primary', type: 'button', onclick: async () => {
      const text = box.value.trim()
      if (!text) return
      status.textContent = 'Sending...'
      const s = await call('POST', '/chat/' + encodeURIComponent(target), { text, idempotency_key: draft.key })
      if (s.status === 200) { drafts[target] = null; return route() }
      if (s.status === 401) { csrf = null; return signInView(said(s.body)) }
      status.textContent = s.status === 0
        ? 'Could not reach the core. Sending again sends the same message, never a second one.'
        : said(s.body)
    } }, 'Send to ' + name), status)
  } else {
    parts.push(el('p', { class: 'quiet' }, said({ error: 'chat_write_off' })))
  }
  show(parts)
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
      const o = await call('POST', '/passkeys/register/options')
      if (o.status !== 200) { status.textContent = said(o.body); return }
      let cred
      try {
        cred = await navigator.credentials.create({ publicKey: creationOptions(o.body) })
      } catch (err) {
        status.textContent = 'No passkey was added. Nothing changed.'
        return
      }
      const done = await call('POST', '/passkeys/register', { credential: credentialJSON(cred), label: name.value.trim() })
      status.textContent = done.status === 200 ? 'Passkey added.' : said(done.body)
      if (done.status === 200) settingsView('Passkey added.')
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
  if (path === 'bramble') return chatView('bramble', 'bramble')
  if (path === 'bots') return botsView()
  const own = path.match(/^hermes\/(me|one)(?:\/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127}))?$/)
  if (own) return own[2] ? ownChatView(own[1], own[2]) : gatewayView(own[1])
  if (path === 'settings') return settingsView()
  return decisionsView()
}

async function poll() {
  if (!csrf || document.visibilityState !== 'visible' || answering) return
  const path = location.pathname
  // A chat view reads itself again, unless something is being typed.
  const box = document.querySelector('textarea')
  if (/^\/m\/(bramble$|room$|rooms\/|bots\/|hermes\/)/.test(path)) {
    if (!box || (!box.value && document.activeElement !== box)) route()
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
