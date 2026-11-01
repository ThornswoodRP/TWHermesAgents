const { chromium } = require('playwright')
// The phone app in headless Chromium with a virtual passkey (Chrome's WebAuthn
// testing API): pairing, a deep link, adding a passkey, signing in with it,
// answering a card (it leaves Decisions for Answered), a high-risk card's own
// check screen, the Bot Chats, the Rooms list and a room (as text), the Board,
// the install hint, the service worker, and alerts: a "no" kept, a push shown
// as the one quiet line with its card's tag (delivered by DevTools, with no
// push service), the alert of an answered card taken off, the alert of an
// opened card taken off, and the plan the app follows when the browser's
// subscription is not the one the core has.
// Started by test_decisions_phone_browser.py.
const [port, code, card, high, third] = process.argv.slice(2)
const base = `http://localhost:${port}`
;(async () => {
  // channel 'chromium': the full browser in its new headless mode. The bare
  // headless shell refuses every notification, so it cannot show an alert.
  const browser = await chromium.launch({ channel: 'chromium', args: ['--host-resolver-rules=MAP localhost 127.0.0.1'] })
  const ctx = await browser.newContext({ viewport: { width: 412, height: 860 } })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  page.on('console', m => { if (m.type() === 'error' && !/status of 401/.test(m.text())) errors.push(m.text()) })
  const cdp = await ctx.newCDPSession(page)
  await cdp.send('WebAuthn.enable')
  await cdp.send('WebAuthn.addVirtualAuthenticator', { options: { protocol: 'ctap2', transport: 'internal',
    hasResidentKey: true, hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true } })
  let failed = 0
  const step = async (name, fn) => { try { await fn(); console.log('PASS', name) } catch (e) { failed++; console.log('FAIL', name, e.message) } }
  await page.goto(base + '/m/decisions/' + card)
  await step('unsigned, the app shows sign-in', () => page.getByText('Pair this phone').waitFor({ timeout: 5000 }))
  await step('in a browser tab, the app says where Chrome keeps its install', async () => {
    await page.getByText(/^To install the app: Chrome's \u22ee menu, then Add to home screen, then Install\.$/).waitFor({ timeout: 3000 })
  })
  await page.getByPlaceholder('XXXX-XXXX').fill(code)
  await page.getByText('Pair this phone').click()
  await step('after pairing, the deep link opens the exact card', () => page.getByText('Turn on the nightly backup check?').waitFor({ timeout: 5000 }))
  await step('the card shows its options and marks the recommendation', async () => {
    await page.getByText('Do it', { exact: true }).waitFor({ timeout: 3000 })
    await page.getByText('Nothing happens until next week.').waitFor({ timeout: 3000 })
    if (!(await page.getByText('recommended').isVisible())) throw new Error('no recommendation mark')
    if ((await page.getByRole('button', { name: /^Choose: / }).count()) !== 2) throw new Error('not one button per option')
  })
  await page.screenshot({ path: require('os').tmpdir() + '/tw-phone-card.png', fullPage: true })
  await page.goto(base + '/m/settings')
  await step('settings after a reload (CSRF asked for again)', () => page.getByText('Add a passkey on this phone').waitFor({ timeout: 5000 }))
  await page.getByText('Add a passkey on this phone').click()
  await step('a passkey is added', () => page.getByText('Passkey added.').or(page.getByText('Added ')).first().waitFor({ timeout: 8000 }))
  await page.getByText('Sign out').click()
  await step('signed out', () => page.getByText('Sign in with your passkey').waitFor({ timeout: 5000 }))
  await page.getByText('Sign in with your passkey').click()
  await step('signed in again with the passkey', () => page.getByText('Add a passkey on this phone').waitFor({ timeout: 8000 }))
  await page.getByRole('link', { name: /Decisions/ }).click()
  await step('the Decisions list after the passkey sign-in, with its badge', async () => {
    await page.getByText('Turn on the nightly backup check?').waitFor({ timeout: 5000 })
    if ((await page.textContent('#badge')) !== '3') throw new Error('badge is not 3')
  })
  await page.getByText('Turn on the nightly backup check?').click()
  await page.getByRole('button', { name: 'Choose: Do it' }).click()
  await step('an answered card leaves Decisions at once, for Answered', async () => {
    await page.getByText('Answered "Turn on the nightly backup check?": Do it. It is under Answered below.').waitFor({ timeout: 5000 })
    if (!page.url().endsWith('/m/decisions')) throw new Error('url ' + page.url())
    await page.getByRole('heading', { name: 'Answered' }).waitFor({ timeout: 3000 })
    await page.getByText('You chose Do it, ').waitFor({ timeout: 3000 })
    if ((await page.textContent('#badge')) !== '2') throw new Error('badge is not 2')
  })
  await page.getByRole('button', { name: /^Turn on the nightly backup check\?/ }).click()
  await step('an answered card opened from Answered has its receipt', () => page.getByText(/^Answered "Do it" at /).waitFor({ timeout: 5000 }))
  await step('an answered card offers no more choices', async () => {
    if (await page.getByRole('button', { name: /^Choose: / }).count()) throw new Error('choices on an answered card')
  })
  await page.goto(base + '/m/decisions/' + high)
  await page.getByRole('button', { name: 'Choose: Wait' }).waitFor({ timeout: 5000 })
  await page.getByRole('button', { name: 'Choose: Wait' }).click()
  await step('a high-risk card asks again on its own screen', () => page.getByText('Check this answer').waitFor({ timeout: 3000 }))
  await page.screenshot({ path: require('os').tmpdir() + '/tw-phone-high.png', fullPage: true })
  await page.getByRole('button', { name: 'Yes, answer Wait' }).click()
  await step('then a fresh passkey sign-in answers it', async () => {
    const again = page.getByRole('button', { name: 'Sign in again and answer' })
    const done = page.getByText('Answered "Delete the old test branches?": Wait. It is under Answered below.')
    await again.or(done).first().waitFor({ timeout: 5000 })
    if (await again.count()) await again.click()
    await done.waitFor({ timeout: 8000 })
  })
  await page.getByRole('link', { name: 'Bramble' }).click()
  await step("Bramble's Bot Chat, as text", () => page.getByText('All green <i>tonight</i>.').waitFor({ timeout: 5000 }))
  await page.getByLabel('Message to Bramble').fill('Thanks, and look at the launcher log next.')
  await page.getByRole('button', { name: 'Send to Bramble' }).click()
  await step('a message to Bramble waits for the connector', async () => {
    await page.getByText('Thanks, and look at the launcher log next.').waitFor({ timeout: 5000 })
    await page.getByText('Waiting to send').waitFor({ timeout: 3000 })
  })
  await page.screenshot({ path: require('os').tmpdir() + '/tw-phone-bramble.png', fullPage: true })
  await page.getByRole('link', { name: 'Bots' }).click()
  await step('the Bots list has the other five', async () => {
    for (const n of ['Rift', 'Sprocket', 'Echo', 'Magpie', 'Wisp']) await page.getByText(n, { exact: true }).waitFor({ timeout: 5000 })
  })
  await page.getByText('Rift', { exact: true }).click()
  await step("Rift's chat opens on its own deep link", async () => {
    await page.getByLabel('Message to Rift').waitFor({ timeout: 5000 })
    if (!page.url().endsWith('/m/bots/rift')) throw new Error('url ' + page.url())
  })
  await page.getByRole('link', { name: 'Board' }).click()
  await step('the Board, as text', () => page.getByText('Fix the <b>launcher</b> log').waitFor({ timeout: 5000 }))
  await page.getByRole('link', { name: 'Rooms' }).click()
  await step('the Rooms list has one button for each room', async () => {
    await page.getByText('Thornswood Workbench', { exact: true }).waitFor({ timeout: 5000 })
    await page.getByText('Second test room', { exact: true }).waitFor({ timeout: 3000 })
    await page.getByText('Bramble: <script>alert(1)</script> hello').waitFor({ timeout: 3000 })
  })
  await page.getByText('Second test room', { exact: true }).click()
  await step('a room opens on its own deep link', async () => {
    await page.getByLabel('Message to Second test room').waitFor({ timeout: 5000 })
    if (!page.url().endsWith('/m/rooms/00000000-0000-4000-8000-000000000002')) throw new Error('url ' + page.url())
  })
  await page.getByRole('link', { name: 'Rooms' }).click()
  await page.getByText('Thornswood Workbench', { exact: true }).click()
  await step('the Workbench room, as text', () => page.getByText('<script>alert(1)</script> hello').waitFor({ timeout: 5000 }))
  await page.getByLabel('Message to Thornswood Workbench').fill('Morning, all.')
  await page.getByRole('button', { name: 'Send to Thornswood Workbench' }).click()
  await step('a room message waits for the connector', () => page.getByText('Waiting to send').waitFor({ timeout: 5000 }))
  await page.screenshot({ path: require('os').tmpdir() + '/tw-phone-room.png', fullPage: true })
  await page.getByRole('link', { name: 'Rooms' }).click()
  await page.getByText('Second test room', { exact: true }).waitFor({ timeout: 5000 })
  await page.screenshot({ path: require('os').tmpdir() + '/tw-phone-rooms.png', fullPage: true })
  await page.goto(base + '/m/room')
  await step("the room's first path opens the Workbench room", () => page.getByLabel('Message to Thornswood Workbench').waitFor({ timeout: 5000 }))
  await page.goto(base + '/m/work')
  await step('the old Work path opens the Board', async () => {
    await page.getByText('Fix the <b>launcher</b> log').waitFor({ timeout: 5000 })
    if (!page.url().endsWith('/m/board')) throw new Error('url ' + page.url())
  })
  await page.goto(base + '/m/settings')
  await step('settings shows alerts off', () => page.getByText(/^Alerts are off\. When on/).waitFor({ timeout: 5000 }))
  await page.getByRole('button', { name: 'Turn on alerts' }).click()
  await step('a "no" from the phone is kept and alerts stay off', async () => {
    await page.getByText('Alerts stay off. The app will not ask again by itself.').waitFor({ timeout: 5000 })
    await page.getByText(/^Alerts are off because this phone said no/).waitFor({ timeout: 3000 })
  })
  await ctx.grantPermissions(['notifications'], { origin: base })
  const regs = []
  cdp.on('ServiceWorker.workerRegistrationUpdated', e => regs.push(...e.registrations))
  await cdp.send('ServiceWorker.enable')
  await page.evaluate(() => navigator.serviceWorker.ready)
  for (let i = 0; i < 50 && !regs.some(r => r.scopeURL === base + '/m/' && !r.isDeleted); i++) await new Promise(ok => setTimeout(ok, 100))
  const reg = regs.find(r => r.scopeURL === base + '/m/' && !r.isDeleted)
  const shown = async () => page.evaluate(async () => (await (await navigator.serviceWorker.getRegistration('/m/')).getNotifications())
    .map(n => ({ title: n.title, body: n.body, tag: n.tag, silent: n.silent, path: n.data && n.data.path, actions: n.actions.length })))
  await step('a push shows only "Thornswood needs you", quiet, tagged by its card', async () => {
    if (!reg) throw new Error('no service worker registration seen')
    await cdp.send('ServiceWorker.deliverPushMessage', { origin: base, registrationId: reg.registrationId,
      data: JSON.stringify({ v: 1, event_id: 'px_1', decision_id: card, attention_revision: 1, path: '/m/decisions/' + card }) })
    let list = []
    for (let i = 0; i < 50 && !list.length; i++) { list = await shown(); if (!list.length) await new Promise(ok => setTimeout(ok, 100)) }
    const want = { title: 'Thornswood needs you', body: '', tag: 'decision:' + card, silent: true, path: '/m/decisions/' + card, actions: 0 }
    if (JSON.stringify(list) !== JSON.stringify([want])) throw new Error(JSON.stringify(list))
  })
  await step('a push with a bad card id opens only Decisions', async () => {
    await cdp.send('ServiceWorker.deliverPushMessage', { origin: base, registrationId: reg.registrationId,
      data: JSON.stringify({ decision_id: '../../api/v1/sign-out' }) })
    let list = []
    for (let i = 0; i < 50 && list.length < 2; i++) { list = await shown(); if (list.length < 2) await new Promise(ok => setTimeout(ok, 100)) }
    const odd = list.find(n => n.tag === 'thornswood')
    if (!odd || odd.path !== '/m/decisions') throw new Error(JSON.stringify(list))
  })
  await cdp.send('ServiceWorker.deliverPushMessage', { origin: base, registrationId: reg.registrationId,
    data: JSON.stringify({ v: 1, event_id: 'px_3', decision_id: third, attention_revision: 1, path: '/m/decisions/' + third }) })
  const gone = async (tag, want) => {
    let list = await shown()
    for (let i = 0; i < 50 && list.some(n => n.tag === tag) === want; i++) { await new Promise(ok => setTimeout(ok, 100)); list = await shown() }
    return list
  }
  await gone('decision:' + third, false)
  await page.getByRole('link', { name: /Decisions/ }).click()
  await page.getByText('Move the backup to Sunday?').waitFor({ timeout: 5000 })
  await step('Decisions takes off the alert of an answered card and the plain one, and keeps the rest', async () => {
    const list = await gone('decision:' + card, true)
    const tags = list.map(n => n.tag)
    if (tags.includes('decision:' + card) || tags.includes('thornswood') || !tags.includes('decision:' + third)) throw new Error(JSON.stringify(tags))
  })
  await page.getByText('Move the backup to Sunday?').click()
  await step('opening a card takes its alert off', async () => {
    const list = await gone('decision:' + third, true)
    if (list.some(n => n.tag === 'decision:' + third)) throw new Error(JSON.stringify(list))
  })
  await step('the plan when the browser and the core differ about the subscription', async () => {
    const got = await page.evaluate(() => {
      const on = { vapid_public: 'K', consent: 'granted', subscribed: true, endpoint_sha256: 'a', lapsed: false, lapsed_endpoint_sha256: null }
      const gone = { ...on, subscribed: false, endpoint_sha256: null, lapsed: true, lapsed_endpoint_sha256: 'a' }
      const b = (hash, keyOk = true) => ({ hash, keyOk })
      return [
        alertsPlan(on, b('a'), 'granted'),            // the same one: nothing
        alertsPlan(on, b('b'), 'granted'),            // the browser has a new one (after install): send it
        alertsPlan(on, b(null), 'granted'),           // the browser lost it: renew
        alertsPlan(on, b('a', false), 'granted'),     // made with another key: renew
        alertsPlan(gone, b('a'), 'granted'),          // the core's answered 410 and the browser still has it: renew
        alertsPlan(gone, b('b'), 'granted'),          // the core's answered 410, the browser has another: send it
        alertsPlan({ ...on, subscribed: false, endpoint_sha256: null }, b('a'), 'granted'),  // turned off: nothing
        alertsPlan({ ...gone, consent: 'denied' }, b('a'), 'granted'),                       // said no: nothing
        alertsPlan(gone, b('a'), 'denied'),           // the phone took the permission away: nothing
        alertsPlan(gone, b('a'), 'default')
      ]
    })
    const want = ['none', 'send', 'renew', 'renew', 'renew', 'send', 'none', 'none', 'none', 'none']
    if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(JSON.stringify(got))
  })
  await page.goto(base + '/m/needs-you')
  await step("the old Needs you path opens Decisions", async () => {
    await page.getByText('Answered', { exact: true }).waitFor({ timeout: 5000 })
    if (!page.url().endsWith('/m/decisions')) throw new Error('url ' + page.url())
  })
  await page.getByRole('button', { name: 'Not now' }).click()
  await step('the install hint goes away for this visit when told not now', async () => {
    if (await page.locator('#install').isVisible()) throw new Error('still shown')
  })
  const sw = await page.evaluate(async () => { const r = await Promise.race([navigator.serviceWorker.ready, new Promise(ok => setTimeout(() => ok(null), 5000))]); return r ? r.scope : null })
  if (!sw) failed++
  console.log(sw ? 'PASS the service worker is registered for ' + sw : 'FAIL no service worker')
  const manifest = await page.evaluate(async () => { const r = await fetch('/m/manifest.webmanifest'); return { status: r.status, body: await r.json() } })
  const mOk = manifest.status === 200 && manifest.body.name === 'Hermes' && manifest.body.short_name === 'Hermes' &&
    manifest.body.start_url === '/m/decisions'
  if (!mOk) failed++
  console.log(mOk ? 'PASS manifest served: Hermes, opening on Decisions' : 'FAIL manifest ' + JSON.stringify(manifest))
  if (errors.length) failed++
  console.log(errors.length ? 'FAIL page errors: ' + errors.join(' | ') : 'PASS no page errors')
  await browser.close()
  console.log(failed ? `${failed} FAILED` : 'ALL PASS')
  process.exit(failed ? 1 : 0)
})()
