// The Thornswood phone app (07 sections 4 and 5, step 3: read only).
//
// Plain script, no framework, no inline code (the page's CSP allows only this
// file). Everything shown from the core is set with textContent, never as
// markup. The session is an HttpOnly cookie the script never sees; the CSRF
// token lives only in memory and is asked for again after a reload.

'use strict'

const API = '/api/v1'
const POLL_MS = 15000
let csrf = null
let seq = 0
let pollTimer = null

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
  sign_in_again: 'This needs a fresh sign-in. Sign out, sign in again, then come back.',
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
      const o = await call('POST', '/passkeys/sign-in/options')
      if (o.status !== 200) { status.textContent = said(o.body); return }
      let cred
      try {
        cred = await navigator.credentials.get({ publicKey: requestOptions(o.body) })
      } catch (err) {
        status.textContent = 'The passkey did not sign in. Nothing changed.'
        return
      }
      done(await call('POST', '/passkeys/sign-in', { credential: credentialJSON(cred) }))
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

async function needsYou() {
  setTab('needs-you')
  const r = await call('GET', '/cards')
  if (refused(r)) return
  seq = r.body.seq
  badge(r.body.cards.length)
  if (!r.body.cards.length) return show(el('p', { class: 'quiet' }, 'Nothing needs you right now.'))
  show(r.body.cards.map(c => el('button', { class: 'row', type: 'button', onclick: () => go('/m/decisions/' + c.id) },
    el('strong', {}, c.title),
    el('div', { class: 'quiet' }, `${c.kind}${c.risk === 'high' ? ', high risk' : ''}, answer by ${when(c.expires_at)}`))))
}

async function cardView(id) {
  setTab('needs-you')
  const r = await call('GET', '/cards/' + encodeURIComponent(id))
  if (refused(r)) return
  const c = r.body
  const a = await call('GET', '/cards/' + encodeURIComponent(id) + '/artifact')
  const parts = [
    el('h2', {}, c.title),
    el('p', { class: 'quiet' }, `${c.kind}, revision ${c.revision}, ${c.status}${c.risk === 'high' ? ', high risk' : ''}`),
    a.status === 200 ? el('pre', { class: 'artifact' }, a.body.body) : el('p', { class: 'warn' }, said(a.body)),
    el('p', {}, el('strong', {}, 'What yes covers: '), c.scope_of_yes),
    c.options.map(o => el('div', { class: 'option' },
      el('strong', {}, o.label), o.key === c.recommendation ? el('span', { class: 'rec' }, 'recommended') : null,
      el('div', { class: 'quiet' }, o.consequence)))
  ]
  if (c.receipt) parts.push(el('p', {}, `Answered ${c.receipt.option_key} at ${when(c.receipt.answered_at)}.`))
  if (c.superseded_by) {
    parts.push(el('button', { class: 'secondary', type: 'button', onclick: () => go('/m/decisions/' + c.superseded_by) },
      'This card was replaced. Open the new one'))
  }
  if (c.status === 'pending') parts.push(el('p', { class: 'quiet' }, 'Answering from the phone comes next. Answer on Desktop for now.'))
  parts.push(el('button', { class: 'secondary', type: 'button', onclick: () => go('/m/needs-you') }, 'Back'))
  show(el('div', { class: 'card' }, parts))
}

async function workView() {
  setTab('work')
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

async function roomView() {
  setTab('room')
  const r = await call('GET', '/room')
  if (refused(r)) return
  if (!r.body.room) return show(el('p', { class: 'quiet' }, 'Nothing has come in from the Workbench room yet.'))
  const events = r.body.room.events
  show(
    el('p', { class: 'quiet' }, `The Workbench room as of ${when(r.body.as_of)}. Read only; what is said here is never an answer.`),
    events.length ? events.map(e => el('div', { class: 'msg' },
      el('div', { class: 'who' }, e.actor || 'someone', el('span', { class: 'quiet' }, '  ' + when(e.at))),
      el('div', { class: 'text' }, e.text)))
      : el('p', { class: 'quiet' }, 'The room is quiet.'))
}

function chatView(tab) {
  setTab(tab)
  show(el('p', { class: 'quiet' }, tab === 'bramble'
    ? 'Talking to Bramble from the phone comes in the next update.'
    : 'The six Bot Chats come to the phone in the next update.'))
}

async function settingsView() {
  setTab('')
  const status = el('p', { class: 'quiet' })
  const list = el('div', {})
  const r = await call('GET', '/passkeys')
  if (r.status === 401) return signInView(said(r.body))
  if (r.status === 200) {
    list.append(...(r.body.passkeys.length ? r.body.passkeys.map(k => el('div', { class: 'option' },
      el('strong', {}, k.label), el('div', { class: 'quiet' }, `Added ${when(k.created_at)}`)))
      : [el('p', { class: 'quiet' }, 'No passkey yet. Add one so this phone can sign in again.')]))
  }
  const name = el('input', { autocomplete: 'off', placeholder: 'This phone', 'aria-label': 'Passkey name' })
  show(
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
      if (done.status === 200) settingsView()
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
  const path = location.pathname.replace(/^\/m\/?/, '').replace(/\/$/, '')
  const card = path.match(/^decisions\/([A-Za-z0-9_-]{1,64})$/)
  if (card) return cardView(card[1])
  if (path === 'work') return workView()
  if (path === 'room') return roomView()
  if (path === 'bramble' || path === 'bots') return chatView(path)
  if (path === 'settings') return settingsView()
  return needsYou()
}

async function poll() {
  if (!csrf || document.visibilityState !== 'visible') return
  const r = await call('GET', '/changes?since=' + seq)
  if (r.status !== 200 || r.body.seq === seq) return
  seq = r.body.seq
  const path = location.pathname
  if (path.endsWith('/needs-you') || path === '/m/' || path.startsWith('/m/decisions/')) route()
}

document.addEventListener('click', event => {
  const a = event.target.closest('a[data-tab]')
  if (a) { event.preventDefault(); go(a.getAttribute('href')) }
})
document.getElementById('settings').addEventListener('click', () => go('/m/settings'))
window.addEventListener('popstate', route)
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') poll() })
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/m/sw.js', { scope: '/m/' }).catch(() => {})
pollTimer = setInterval(poll, POLL_MS)
route()
