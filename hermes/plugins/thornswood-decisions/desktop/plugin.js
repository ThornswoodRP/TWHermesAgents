// The thornswood-decisions pane for Hermes Desktop (07 section 5, step 2).
//
// It shows the decision core's Decisions (the cards waiting on Patrick) and sends Patrick's answers.
// Every call goes through this plugin's backend (dashboard/plugin_api.py) on
// his Hermes on the VM, which forwards it to the core on loopback. The pane
// holds one thing: the session the core gave this Desktop when it was paired,
// in ctx.storage. It never shows or copies a report page card.
//
// Plain ESM, loaded uncompiled: jsx()/jsxs() calls, no JSX, and only the three
// specifiers Desktop allows.

import { Button, ConfirmDialog, Input, atom, useValue } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useState } from 'react'

const POLL_MS = 5000
const CALL_TIMEOUT_MS = 20000

const muted = 'text-(--ui-text-tertiary)'

function text(className, children) {
  return jsx('div', { className, children })
}

function newKey() {
  return 'ans_' + globalThis.crypto.randomUUID().replace(/-/g, '')
}

// What a refusal from the core means, in words for Patrick.
function explain(env) {
  const b = (env && env.body) || {}
  switch (b.error) {
    case 'core_unreachable':
      return 'The decision core did not answer. Nothing was sent.'
    case 'desktop_unreachable':
      return 'Hermes on the VM did not answer.'
    case 'decision_write_off':
      return 'Answers are off for now. The report page is still where you answer.'
    case 'stale_revision':
      return 'The card changed since you opened it. Read it again before answering.'
    case 'already_answered':
      return 'This card was already answered.'
    case 'expired':
      return 'This card expired. Bramble opens a new one if it still matters.'
    case 'sign_in_again':
      return 'A high-risk card needs a fresh sign-in. Pair this Desktop again with a new code to answer it.'
    case 'wrong_hermes_sign_in':
      return 'This Desktop was paired through another Hermes sign-in. Pair it again.'
    case 'no_hermes_sign_in':
      return 'Hermes did not say who is signed in, so the core will not pair this Desktop.'
    default:
      return b.message || 'Something went wrong.'
  }
}

function makeStore(ctx) {
  const $s = atom({
    session: ctx.storage.get('session', null),
    cards: [],
    seq: 0,
    card: null,
    artifact: null,
    pending: ctx.storage.get('pending', null),
    notice: null,
    busy: false
  })
  const get = () => $s.get()
  const set = patch => $s.set({ ...$s.get(), ...patch })

  async function call(path, body) {
    const session = get().session
    try {
      return await ctx.rest(path, {
        method: 'POST',
        body: path === '/pair' ? body : { ...body, session: session || {} },
        timeoutMs: CALL_TIMEOUT_MS
      })
    } catch (err) {
      return { status: 0, body: { error: 'desktop_unreachable', message: String((err && err.message) || err) } }
    }
  }

  function signedOut(env) {
    if (env.status === 401) {
      ctx.storage.remove('session')
      set({ session: null, cards: [], card: null, artifact: null, notice: explain(env) })
      return true
    }
    return false
  }

  async function refresh() {
    if (!get().session) return
    const env = await call('/cards', {})
    if (signedOut(env)) return
    if (env.status === 200) set({ cards: env.body.cards, seq: env.body.seq })
    else set({ notice: explain(env) })
  }

  async function open(id) {
    const env = await call('/card', { id })
    if (signedOut(env)) return
    if (env.status !== 200) return set({ notice: explain(env) })
    const card = env.body
    const art = await call('/artifact', { id })
    set(art.status === 200 ? { card, artifact: art.body } : { card, artifact: null, notice: explain(art) })
  }

  // Patrick opens a card from the list: what was said about the last one goes.
  function choose(id) {
    set({ notice: null })
    return open(id)
  }

  function back() {
    set({ card: null, artifact: null, notice: null })
  }

  // One poll at a time: a slow answer from the VM never stacks a second one.
  let polling = false
  async function tick() {
    if (polling) return
    polling = true
    try {
      await poll()
    } finally {
      polling = false
    }
  }

  async function poll() {
    const s = get()
    if (!s.session) return
    const env = await call('/changes', { since: s.seq })
    if (signedOut(env) || env.status !== 200) return
    if (env.body.seq === s.seq) return
    const changed = env.body.changed || []
    set({ seq: env.body.seq })
    await refresh()
    if (s.card && changed.includes(s.card.id)) await open(s.card.id)
    const p = get().pending
    if (p && p.state !== 'sending') await settle()
  }

  async function pair(code) {
    set({ busy: true, notice: null })
    const env = await call('/pair', { code: code.trim() })
    set({ busy: false })
    if (env.status !== 200) return set({ notice: explain(env) })
    const session = { token: env.body.token, csrf: env.body.csrf, expires_at: env.body.expires_at }
    ctx.storage.set('session', session)
    set({ session, notice: null })
    await refresh()
  }

  // One answer attempt keeps its idempotency key until the outcome is known, so
  // sending it again can never make a second answer.
  async function answer(option) {
    const s = get()
    const card = s.card
    if (!card || s.busy) return
    let pending = s.pending
    if (!pending || pending.id !== card.id || pending.option !== option ||
        pending.revision !== card.revision || pending.attention_revision !== card.attention_revision) {
      pending = {
        id: card.id, option, key: newKey(), revision: card.revision,
        attention_revision: card.attention_revision, artifact_sha256: card.artifact_sha256
      }
    }
    ctx.storage.set('pending', pending)
    set({ pending: { ...pending, state: 'sending' }, busy: true, notice: null })
    const env = await call('/answer', {
      id: pending.id, option_key: pending.option, revision: pending.revision,
      attention_revision: pending.attention_revision, artifact_sha256: pending.artifact_sha256,
      idempotency_key: pending.key
    })
    set({ busy: false })
    if (signedOut(env)) return
    if (env.status === 200) return done(env.body)
    if (env.status === 0) {
      // Unclear: the answer may or may not have landed. Read the receipt first.
      set({ pending: { ...pending, state: 'checking' } })
      return settle()
    }
    forget()
    if (env.body.error === 'already_answered' && env.body.detail && env.body.detail.receipt) {
      return done(env.body.detail.receipt, explain(env))
    }
    set({ notice: explain(env) })
    if (env.body.error === 'stale_revision') await open(card.id)
  }

  // After an unclear result: the card's own receipt says what happened.
  async function settle() {
    const pending = get().pending
    if (!pending) return
    const env = await call('/card', { id: pending.id })
    if (signedOut(env)) return
    if (env.status !== 200) {
      return set({ pending: { ...pending, state: 'unknown' }, notice: explain(env) })
    }
    if (env.body.receipt) return done(env.body.receipt)
    set({ card: env.body, pending: { ...pending, state: 'unknown' },
          notice: 'The core has no answer from you on this card yet. Sending again is safe: it is the same answer.' })
  }

  function forget() {
    ctx.storage.remove('pending')
    set({ pending: null })
  }

  async function done(receipt, note) {
    forget()
    set({ notice: note || `Answered: option ${receipt.option_key}, at ${receipt.answered_at}.` })
    await refresh()
    const card = get().card
    if (card) await open(card.id)
  }

  async function signOut() {
    await call('/sign-out', {})
    ctx.storage.remove('session')
    forget()
    set({ session: null, cards: [], card: null, artifact: null, notice: 'Signed out.' })
  }

  return { $s, set, refresh, open, choose, back, tick, pair, answer, settle, signOut, forget }
}

function PairView({ store }) {
  const s = useValue(store.$s)
  const [code, setCode] = useState('')
  return jsxs('div', {
    className: 'flex flex-col gap-2 p-3 text-sm',
    children: [
      text('font-medium', 'Pair this Desktop'),
      text(muted, 'Type the one-time code the VM or your phone gave you. It works once, for 10 minutes.'),
      jsx(Input, { value: code, onChange: e => setCode(e.target.value), placeholder: 'XXXX-XXXX', autoComplete: 'off' }),
      jsx(Button, { disabled: s.busy || !code.trim(), onClick: () => store.pair(code), children: 'Pair' }),
      s.notice ? text(muted, s.notice) : null
    ]
  })
}

function CardList({ store }) {
  const s = useValue(store.$s)
  if (!s.cards.length) return text(`p-3 text-sm ${muted}`, 'Nothing needs you here.')
  return jsx('div', {
    className: 'flex flex-col',
    children: s.cards.map(c =>
      jsxs('button', {
        type: 'button',
        key: c.id,
        className: 'flex flex-col items-start gap-0.5 px-3 py-2 text-left text-sm hover:bg-(--ui-bg-hover)',
        onClick: () => store.choose(c.id),
        children: [
          text('font-medium', c.title),
          text(`text-xs ${muted}`, `${c.kind}${c.risk === 'high' ? ', high risk' : ''}, answer by ${c.expires_at}`)
        ]
      })
    )
  })
}

function CardView({ store }) {
  const s = useValue(store.$s)
  const [confirm, setConfirm] = useState(null)
  const c = s.card
  const answered = c.status !== 'pending'
  const pending = s.pending && s.pending.id === c.id ? s.pending : null
  const pick = key => (c.risk === 'high' ? setConfirm(key) : store.answer(key))
  return jsxs('div', {
    className: 'flex flex-col gap-2 p-3 text-sm',
    children: [
      jsx(Button, { variant: 'ghost', size: 'xs', onClick: () => store.back(), children: 'Back' }),
      text('font-medium', c.title),
      text(`text-xs ${muted}`, `${c.kind}, revision ${c.revision}, ${c.status}${c.risk === 'high' ? ', high risk' : ''}`),
      s.artifact
        ? jsx('pre', { className: 'whitespace-pre-wrap break-words text-xs', children: s.artifact.body })
        : text(muted, 'The card\'s text did not load.'),
      text(`text-xs ${muted}`, `What yes covers: ${c.scope_of_yes}`),
      ...c.options.map(o =>
        jsxs('div', {
          key: o.key,
          className: 'flex flex-col gap-1',
          children: [
            jsx(Button, {
              variant: o.key === c.recommendation ? 'default' : 'outline',
              disabled: answered || s.busy,
              onClick: () => pick(o.key),
              children: `${o.label}${o.key === c.recommendation ? ' (recommended)' : ''}`
            }),
            text(`text-xs ${muted}`, o.consequence)
          ]
        })
      ),
      pending && pending.state === 'checking' ? text(muted, 'Checking whether the answer landed...') : null,
      pending && pending.state === 'unknown'
        ? jsx(Button, { variant: 'secondary', disabled: s.busy, onClick: () => store.answer(pending.option),
            children: 'Send the same answer again' })
        : null,
      c.receipt
        ? text(`text-xs ${muted}`, `Answered ${c.receipt.option_key} at ${c.receipt.answered_at}.`)
        : null,
      s.notice ? text(muted, s.notice) : null,
      jsx(ConfirmDialog, {
        open: confirm !== null,
        onClose: () => setConfirm(null),
        onConfirm: () => {
          const key = confirm
          setConfirm(null)
          return store.answer(key)
        },
        title: 'Answer a high-risk card',
        description: `What yes covers: ${c.scope_of_yes}`,
        confirmLabel: 'Send this answer',
        destructive: true
      })
    ]
  })
}

function Pane({ store }) {
  const s = useValue(store.$s)
  if (!s.session) return jsx(PairView, { store })
  return jsxs('div', {
    className: 'flex h-full flex-col overflow-auto',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between px-3 py-2',
        children: [
          text('text-sm font-medium', 'Decisions'),
          jsx(Button, { variant: 'ghost', size: 'xs', onClick: () => store.signOut(), children: 'Sign out' })
        ]
      }),
      s.card ? jsx(CardView, { store }) : jsx(CardList, { store }),
      !s.card && s.notice ? text(`px-3 text-sm ${muted}`, s.notice) : null
    ]
  })
}

export default {
  id: 'thornswood-decisions',
  name: 'Thornswood decisions',
  defaultEnabled: false,
  register(ctx) {
    const store = makeStore(ctx)
    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'Thornswood',
      data: { placement: 'right', width: '340px' },
      render: () => jsx(Pane, { store })
    })
    // The pane polls: Desktop's plugin socket is a no-op on an OAuth remote.
    ctx.setInterval(() => { store.tick() }, POLL_MS)
    store.refresh()
    if (store.$s.get().pending) store.settle()
  }
}
