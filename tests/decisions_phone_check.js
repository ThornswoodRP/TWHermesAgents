const { chromium } = require('playwright')
// The phone app in headless Chromium with a virtual passkey (Chrome's WebAuthn
// testing API): pairing, a deep link, adding a passkey, signing in with it,
// answering a card, a high-risk card's own check screen, the Bot Chats and the
// room (as text), the Work view, the service worker, and alerts: a "no" kept,
// a push shown as the one quiet line with its card's tag (delivered by
// DevTools, with no push service), and the alert of an answered card taken off.
// Started by test_decisions_phone_browser.py.
const [port, code, card, high] = process.argv.slice(2)
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
  await page.getByRole('link', { name: /Needs you/ }).click()
  await step('the Needs you list after the passkey sign-in, with its badge', async () => {
    await page.getByText('Turn on the nightly backup check?').waitFor({ timeout: 5000 })
    if ((await page.textContent('#badge')) !== '2') throw new Error('badge is not 2')
  })
  await page.getByText('Turn on the nightly backup check?').click()
  await page.getByRole('button', { name: 'Choose: Do it' }).click()
  await step('an answer from the phone comes back with its receipt', () => page.getByText(/^Answered "Do it" at /).waitFor({ timeout: 5000 }))
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
    const done = page.getByText(/^Answered "Wait" at /)
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
  await page.getByRole('link', { name: 'Work' }).click()
  await step('the Work view, as text', () => page.getByText('Fix the <b>launcher</b> log').waitFor({ timeout: 5000 }))
  await page.getByRole('link', { name: 'Room' }).click()
  await step('the Room view, as text', () => page.getByText('<script>alert(1)</script> hello').waitFor({ timeout: 5000 }))
  await page.getByLabel('Message to the Workbench room').fill('Morning, all.')
  await page.getByRole('button', { name: 'Send to the Workbench room' }).click()
  await step('a room message waits for the connector', () => page.getByText('Waiting to send').waitFor({ timeout: 5000 }))
  await page.screenshot({ path: require('os').tmpdir() + '/tw-phone-room.png', fullPage: true })
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
  await step('a push with a bad card id opens only Needs you', async () => {
    await cdp.send('ServiceWorker.deliverPushMessage', { origin: base, registrationId: reg.registrationId,
      data: JSON.stringify({ decision_id: '../../api/v1/sign-out' }) })
    let list = []
    for (let i = 0; i < 50 && list.length < 2; i++) { list = await shown(); if (list.length < 2) await new Promise(ok => setTimeout(ok, 100)) }
    const odd = list.find(n => n.tag === 'thornswood')
    if (!odd || odd.path !== '/m/needs-you') throw new Error(JSON.stringify(list))
  })
  await page.getByRole('link', { name: /Needs you/ }).click()
  await step('the alert of an answered card is taken off the phone', async () => {
    let list = await shown()
    for (let i = 0; i < 50 && list.some(n => n.tag === 'decision:' + card); i++) { await new Promise(ok => setTimeout(ok, 100)); list = await shown() }
    if (list.some(n => n.tag === 'decision:' + card)) throw new Error(JSON.stringify(list))
  })
  const sw = await page.evaluate(async () => { const r = await Promise.race([navigator.serviceWorker.ready, new Promise(ok => setTimeout(() => ok(null), 5000))]); return r ? r.scope : null })
  if (!sw) failed++
  console.log(sw ? 'PASS the service worker is registered for ' + sw : 'FAIL no service worker')
  const manifest = await page.evaluate(async () => (await fetch('/m/manifest.webmanifest')).status)
  if (manifest !== 200) failed++
  console.log(manifest === 200 ? 'PASS manifest served' : 'FAIL manifest ' + manifest)
  if (errors.length) failed++
  console.log(errors.length ? 'FAIL page errors: ' + errors.join(' | ') : 'PASS no page errors')
  await browser.close()
  console.log(failed ? `${failed} FAILED` : 'ALL PASS')
  process.exit(failed ? 1 : 0)
})()
