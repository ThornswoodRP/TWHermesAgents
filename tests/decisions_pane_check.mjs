// Runs the Desktop pane's logic under node with stand-ins for the Hermes SDK
// and a scripted backend, so no Hermes and no core are needed.
//   node tests/decisions_pane_check.mjs hermes/plugins/thornswood-decisions/desktop/plugin.js
// Prints a PASS or FAIL line per check and ALL PASS at the end; exits 1 on a FAIL.

import { readFileSync, writeFileSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const source = readFileSync(process.argv[2], 'utf8')

const atom = v => {
  let value = v
  return { get: () => value, set: n => { value = n } }
}
globalThis.__twStubs = {
  sdk: { Button: 'Button', ConfirmDialog: 'ConfirmDialog', Input: 'Input', atom, useValue: a => a.get() },
  jsx: { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) },
  react: { useState: init => [init, () => {}] }
}
const body = source
  .replace(/^import \{([^}]*)\} from '@hermes\/plugin-sdk'$/m, 'const {$1} = globalThis.__twStubs.sdk')
  .replace(/^import \{([^}]*)\} from 'react\/jsx-runtime'$/m, 'const {$1} = globalThis.__twStubs.jsx')
  .replace(/^import \{([^}]*)\} from 'react'$/m, 'const {$1} = globalThis.__twStubs.react')
if (/^import /m.test(body)) {
  console.log('FAIL the pane imports something the check does not know')
  process.exit(1)
}
const dir = mkdtempSync(join(tmpdir(), 'tw-pane-'))
const file = join(dir, 'plugin.mjs')
writeFileSync(file, body)
const plugin = (await import(pathToFileURL(file).href)).default
rmSync(dir, { recursive: true, force: true })

let failed = 0
const check = (name, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${ok || !detail ? '' : ': ' + detail}`)
  if (!ok) failed++
}

const CARD = {
  id: 'dc_1', kind: 'question', status: 'pending', title: 'Pick one', revision: 1, attention_revision: 1,
  artifact_sha256: 'a'.repeat(64), recommendation: 'a', scope_of_yes: 'Only this.', risk: 'normal',
  expires_at: '2026-11-02T18:00:00+00:00', options: [{ key: 'a', label: 'Do it', consequence: 'It happens.' },
    { key: 'b', label: 'Wait', consequence: 'Nothing.' }], links: [], receipt: null
}
const RECEIPT = { response_id: 'rsp_1', decision_id: 'dc_1', revision: 1, attention_revision: 1, option_key: 'a',
  answered_at: '2026-11-01T18:01:00+00:00', device_id: 'dv_1' }

// One Desktop: storage, timers and a backend whose answers each test scripts.
function desktop(script) {
  const store = new Map()
  const calls = []
  let interval = null
  let rendered = null
  const ctx = {
    storage: {
      get: (k, f) => (store.has(k) ? JSON.parse(store.get(k)) : f),
      set: (k, v) => store.set(k, JSON.stringify(v)),
      remove: k => store.delete(k)
    },
    rest: async (path, opts) => {
      calls.push({ path, body: opts.body })
      const answer = script(path, opts.body, calls)
      if (answer instanceof Error) throw answer
      return answer
    },
    register: c => { rendered = c },
    setInterval: (fn, ms) => { interval = { fn, ms } }
  }
  plugin.register(ctx)
  const s = rendered.render().props.store
  return { ctx, store, calls, s, interval: () => interval, contribution: rendered }
}

const flush = () => new Promise(r => setTimeout(r, 0))

// Walk a rendered tree, calling function components, so a render error shows.
function walk(node) {
  if (node == null || typeof node !== 'object') return
  if (Array.isArray(node)) return node.forEach(walk)
  if (typeof node.type === 'function') return walk(node.type(node.props))
  walk(node.props && node.props.children)
}

const cards = { status: 200, body: { cards: [CARD], seq: 3 } }
const ok = (path, b) => {
  if (path === '/pair') return { status: 200, body: { device_id: 'dv_1', token: 'tok', csrf: 'cs', expires_at: 'x' } }
  if (path === '/cards') return cards
  if (path === '/card') return { status: 200, body: CARD }
  if (path === '/artifact') return { status: 200, body: { revision: 1, sha256: CARD.artifact_sha256, body: 'Text.' } }
  if (path === '/changes') return { status: 200, body: { seq: 3, changed: [] } }
  if (path === '/answer') return { status: 200, body: RECEIPT }
  return { status: 404, body: { error: 'no_route' } }
}

{
  const d = desktop(ok)
  check('it registers one pane on the right', d.contribution.area === 'panes' && d.contribution.data.placement === 'right')
  check('it polls every 5 seconds through ctx.setInterval', d.interval() && d.interval().ms === 5000)
  walk(d.contribution.render())
  check('unpaired, it shows the pairing view', d.s.$s.get().session === null)
  await d.s.pair(' ABCD-EFGH ')
  const pairCall = d.calls.find(c => c.path === '/pair')
  check('pairing sends the trimmed code and no session', pairCall.body.code === 'ABCD-EFGH' && !('session' in pairCall.body))
  check('the session is kept in plugin storage', JSON.parse(d.store.get('session')).token === 'tok')
  check('after pairing it lists the cards', d.s.$s.get().cards.length === 1)
  await d.s.open('dc_1')
  walk(d.contribution.render())
  check('every later call carries the session in its body',
    d.calls.filter(c => c.path !== '/pair').every(c => c.body.session && c.body.session.token === 'tok'))
  await d.s.answer('a')
  const ans = d.calls.find(c => c.path === '/answer').body
  check('an answer carries the revisions it was shown and a key',
    ans.revision === 1 && ans.attention_revision === 1 && ans.artifact_sha256 === CARD.artifact_sha256 &&
    /^ans_[0-9a-f]{32}$/.test(ans.idempotency_key))
  check('a landed answer clears the pending attempt', d.s.$s.get().pending === null && !d.store.has('pending'))
}

{
  // The call fails in transit. The pane reads the receipt before offering anything.
  let landed = false
  const d = desktop((path, b) => {
    if (path === '/answer') { landed = true; return new Error('socket hang up') }
    if (path === '/card') return { status: 200, body: landed ? { ...CARD, status: 'resolved', receipt: RECEIPT } : CARD }
    return ok(path, b)
  })
  await d.s.pair('ABCD-EFGH')
  await d.s.open('dc_1')
  await d.s.answer('a')
  const st = d.s.$s.get()
  check('an unclear answer that landed is found by its receipt', st.pending === null && /Answered: option a/.test(st.notice))
  check('it never sent the answer twice', d.calls.filter(c => c.path === '/answer').length === 1)
}

{
  // The call fails in transit and did not land: the same key goes again.
  let n = 0
  const d = desktop((path, b) => {
    if (path === '/answer') { n++; return n === 1 ? new Error('timed out') : { status: 200, body: RECEIPT } }
    return ok(path, b)
  })
  await d.s.pair('ABCD-EFGH')
  await d.s.open('dc_1')
  await d.s.answer('a')
  check('an unclear answer that did not land waits for Patrick', d.s.$s.get().pending.state === 'unknown')
  check('the attempt survives a reload of the pane', JSON.parse(d.store.get('pending')).id === 'dc_1')
  walk(d.contribution.render())
  await d.s.answer('a')
  const keys = d.calls.filter(c => c.path === '/answer').map(c => c.body.idempotency_key)
  check('sending again reuses the same key', keys.length === 2 && keys[0] === keys[1])
  check('then it is answered', d.s.$s.get().pending === null)
}

{
  // A different option is a different answer with its own key.
  let n = 0
  const d = desktop((path, b) => {
    if (path === '/answer') { n++; return n === 1 ? new Error('timed out') : { status: 200, body: RECEIPT } }
    return ok(path, b)
  })
  await d.s.pair('ABCD-EFGH')
  await d.s.open('dc_1')
  await d.s.answer('a')
  await d.s.answer('b')
  const keys = d.calls.filter(c => c.path === '/answer').map(c => c.body.idempotency_key)
  check('another option gets a new key', keys.length === 2 && keys[0] !== keys[1])
}

{
  // The card changed: the pane says so and reads it again; nothing is resent.
  const d = desktop((path, b) => path === '/answer'
    ? { status: 409, body: { error: 'stale_revision', message: 'x', detail: { revision: 2 } } }
    : ok(path, b))
  await d.s.pair('ABCD-EFGH')
  await d.s.open('dc_1')
  const before = d.calls.filter(c => c.path === '/card').length
  await d.s.answer('a')
  check('a stale answer reads the card again', d.calls.filter(c => c.path === '/card').length === before + 1)
  check('and says why', /changed since you opened it/.test(d.s.$s.get().notice))
  check('and holds no pending attempt', d.s.$s.get().pending === null)
}

{
  // A 401 anywhere signs the pane out and forgets the session.
  const d = desktop((path, b) => path === '/changes'
    ? { status: 401, body: { error: 'wrong_hermes_sign_in', message: 'x' } }
    : ok(path, b))
  await d.s.pair('ABCD-EFGH')
  await d.s.tick()
  check('a 401 forgets the session', d.s.$s.get().session === null && !d.store.has('session'))
  check('and says which sign-in', /another Hermes sign-in/.test(d.s.$s.get().notice))
}

{
  // Polling: a quiet poll reads nothing more; a moved card is read again.
  let seq = 3
  const d = desktop((path, b) => path === '/changes'
    ? { status: 200, body: { seq, changed: seq > 3 ? ['dc_1'] : [] } }
    : ok(path, b))
  await d.s.pair('ABCD-EFGH')
  await d.s.open('dc_1')
  const n0 = d.calls.length
  await d.s.tick()
  check('a quiet poll is one call', d.calls.length === n0 + 1)
  seq = 5
  await d.s.tick()
  const after = d.calls.slice(n0 + 1).map(c => c.path)
  check('a moved card is read again', after.includes('/cards') && after.includes('/card'))
}

{
  // High-risk cards ask before sending.
  const d = desktop((path, b) => path === '/card' ? { status: 200, body: { ...CARD, risk: 'high' } } : ok(path, b))
  await d.s.pair('ABCD-EFGH')
  await d.s.open('dc_1')
  let dialog = null
  const find = node => {
    if (node == null || typeof node !== 'object') return
    if (Array.isArray(node)) return node.forEach(find)
    if (node.type === 'ConfirmDialog') dialog = node
    if (typeof node.type === 'function') return find(node.type(node.props))
    find(node.props && node.props.children)
  }
  find(d.contribution.render())
  check('a high-risk card has a confirmation', dialog !== null && dialog.props.destructive === true)
}

await flush()
if (failed) {
  console.log(`${failed} FAILED`)
  process.exit(1)
}
console.log('ALL PASS')
