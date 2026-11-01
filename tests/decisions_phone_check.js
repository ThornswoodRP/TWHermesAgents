const { chromium } = require('playwright')
// The phone app in headless Chromium with a virtual passkey (Chrome's WebAuthn
// testing API): pairing, a deep link, adding a passkey, signing in with it, the
// Work and Room views, and the service worker. Started by test_decisions_phone_browser.py.
const [port, code, card] = process.argv.slice(2)
const base = `http://localhost:${port}`
;(async () => {
  const browser = await chromium.launch({ args: ['--host-resolver-rules=MAP localhost 127.0.0.1'] })
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
    await page.getByText('Do it').waitFor({ timeout: 3000 })
    await page.getByText('Nothing happens until next week.').waitFor({ timeout: 3000 })
    if (!(await page.getByText('recommended').isVisible())) throw new Error('no recommendation mark')
    if (await page.getByRole('button', { name: 'Do it' }).count()) throw new Error('an answer button on a read-only phone')
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
    if ((await page.textContent('#badge')) !== '1') throw new Error('badge is not 1')
  })
  await page.getByRole('link', { name: 'Work' }).click()
  await step('the Work view, as text', () => page.getByText('Fix the <b>launcher</b> log').waitFor({ timeout: 5000 }))
  await page.getByRole('link', { name: 'Room' }).click()
  await step('the Room view, as text', () => page.getByText('<script>alert(1)</script> hello').waitFor({ timeout: 5000 }))
  await page.screenshot({ path: require('os').tmpdir() + '/tw-phone-room.png', fullPage: true })
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
