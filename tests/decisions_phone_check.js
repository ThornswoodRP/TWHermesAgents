const { chromium } = require('playwright')
// The phone app in headless Chromium with a virtual passkey (Chrome's WebAuthn
// testing API): setting up the phone (pairing and its first passkey on one
// screen), a deep link, adding a passkey, signing in with it,
// answering a card (it leaves Decisions for good), a high-risk card's own
// check screen, the four tabs, the Thornswood Bots list (Hermes, One, then the
// six Bots) and a chat with Patrick's own Hermes on One (as text, a new chat,
// Hermes on the VM with no connector yet, and reactions: Hermes's own, setting
// one from a message's actions and taking it off), the Rooms
// list and a room (as text), the Board,
// the install hint, the service worker, and alerts: a "no" kept, a push shown
// as the one quiet line with its card's tag (delivered by DevTools, with no
// push service), the alert of an answered card taken off, the alert of an
// opened card taken off, and the plan the app follows when the browser's
// subscription is not the one the core has. And voice, with Chromium's fake
// microphone playing a voice-like hum and the test server's stand-in Whisper:
// dictation into the box, voice chat with an answer read aloud, Read aloud, and
// "Hey Hermes" (off until turned on, only in an open chat, stopped when hidden).
// Started by test_decisions_phone_browser.py.
const [port, code, card, high, third, boardNext, voiceWav, VOICE, VOICE_REPLY] = process.argv.slice(2)
const base = `http://localhost:${port}`
;(async () => {
  // channel 'chromium': the full browser in its new headless mode. The bare
  // headless shell refuses every notification, so it cannot show an alert.
  const browser = await chromium.launch({ channel: 'chromium', args: ['--host-resolver-rules=MAP localhost 127.0.0.1',
    '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', '--use-file-for-fake-audio-capture=' + voiceWav] })
  // The microphone is allowed by --use-fake-ui-for-media-stream, the way Patrick allows it once on his phone.
  const ctx = await browser.newContext({ viewport: { width: 412, height: 860 } })
  // The phone's own speech, stood in for: what it would say is kept in window.__said.
  await ctx.addInitScript(() => {
    window.__said = []
    const fake = { speaking: false, pending: false, getVoices: () => [], cancel () {},
      speak (u) { window.__said.push(u.text); setTimeout(() => u.onend && u.onend(new Event('end')), 30) } }
    Object.defineProperty(window, 'speechSynthesis', { value: fake, configurable: true })
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  // A 401 is the signed-out app asking; a 404 is said below only when it is not one of the two that are answers.
  page.on('console', m => { if (m.type() === 'error' && !/status of (401|404)/.test(m.text())) errors.push(m.text()) })
  page.on('response', r => {
    // Two 404s are answers, not missing routes: a chat a gateway never listed, and a fingerprint key the core no longer has.
    if (r.status() === 404 && !/\/api\/v1\/(own\/(me|one)\/chats\/[^/]+\/messages|passkeys\/sign-in\/options)$/.test(new URL(r.url()).pathname)) {
      errors.push('404 ' + r.url())
    }
  })
  // The names Hermes and One are shown only: every request carries the ids me and one (plan Part 2).
  const shownNames = []
  page.on('request', q => {
    const u = new URL(q.url())
    if (!u.pathname.startsWith('/api/')) return
    const own = u.pathname.match(/^\/api\/v1\/own\/([^/]+)/)
    if (own && !['me', 'one'].includes(own[1])) shownNames.push(u.pathname)
    const body = q.postData() || ''
    if (/"(gateway|target)"\s*:\s*"(Hermes|One)"/.test(body)) shownNames.push(u.pathname + ' ' + body)
  })
  const cdp = await ctx.newCDPSession(page)
  await cdp.send('WebAuthn.enable')
  const { authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', { options: { protocol: 'ctap2', transport: 'internal',
    hasResidentKey: true, hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true } })
  let failed = 0
  const step = async (name, fn) => { try { await fn(); console.log('PASS', name) } catch (e) { failed++; console.log('FAIL', name, e.message) } }
  await page.goto(base + '/m/decisions/' + card)
  await step('with no passkey yet, the app shows setting up this phone', async () => {
    await page.getByRole('heading', { name: 'Set up this phone' }).waitFor({ timeout: 5000 })
    await page.getByText(/--kind phone --label 'Pixel 9a'"$/).waitFor({ timeout: 3000 })
    if (await page.getByRole('button', { name: 'Sign in with your passkey' }).count()) throw new Error('a passkey button with no passkey')
  })
  await step('in a browser tab, the app says where Chrome keeps its install', async () => {
    await page.getByText(/^To install the app: Chrome's \u22ee menu, then Add to home screen, then Install\.$/).waitFor({ timeout: 3000 })
  })
  await page.getByPlaceholder('XXXX-XXXX').fill(code)
  await page.getByRole('button', { name: 'Pair and add a passkey' }).click()
  await step('after pairing and its passkey, the deep link opens the exact card', () => page.getByText('Turn on the nightly backup check?').waitFor({ timeout: 8000 }))
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
  await step('the pairing added the first passkey, and the phone is not given a second', async () => {
    await page.getByText('This phone already has a passkey for Hermes. Nothing changed.').waitFor({ timeout: 8000 })
    if ((await page.getByText(/^Added /).count()) !== 1) throw new Error('not one passkey')
  })
  await page.getByText('Sign out').click()
  await step('signed out, with a passkey: one button, the other ways folded away', async () => {
    await page.getByRole('heading', { name: 'Welcome back' }).waitFor({ timeout: 5000 })
    await page.getByText('Other ways in: a pairing code, or a recovery code').waitFor({ timeout: 3000 })
    if (await page.getByRole('button', { name: 'Pair and add a passkey' }).isVisible()) throw new Error('the pairing is not folded away')
  })
  await step('signing out never opens the passkey prompt by itself', async () => {
    if (!(await page.getByRole('button', { name: 'Sign in with your passkey' }).isVisible())) throw new Error('no button after signing out')
    if (await page.getByText('Add a passkey on this phone').count()) throw new Error('signed in again by itself')
  })
  await page.getByText('Sign in with your passkey').click()
  await step('signed in again with the passkey', () => page.getByText('Add a passkey on this phone').waitFor({ timeout: 8000 }))
  // Opening the app signed out asks for the passkey at once, with no button (Patrick, 1 Nov).
  await page.getByText('Sign out').click()
  await page.getByRole('heading', { name: 'Welcome back' }).waitFor({ timeout: 5000 })
  await page.goto(base + '/m/settings')
  await step('opening the app signed out opens the passkey prompt by itself, and it signs in', async () => {
    await page.getByText('Add a passkey on this phone').waitFor({ timeout: 8000 })
  })
  // A dismissed prompt (no face or fingerprint given): then, and only then, the button shows.
  await page.getByText('Sign out').click()
  await page.getByRole('heading', { name: 'Welcome back' }).waitFor({ timeout: 5000 })
  await cdp.send('WebAuthn.setUserVerified', { authenticatorId, isUserVerified: false })
  await page.goto(base + '/m/settings')
  await step('a dismissed prompt on opening shows the button, and the button signs in', async () => {
    const button = page.getByRole('button', { name: 'Sign in with your passkey' })
    await page.getByText('The passkey did not sign in. Nothing changed.').waitFor({ timeout: 8000 })
    if (!(await button.isVisible())) throw new Error('no button after a dismissed prompt')
    await cdp.send('WebAuthn.setUserVerified', { authenticatorId, isUserVerified: true })
    await button.click()
    await page.getByText('Add a passkey on this phone').waitFor({ timeout: 8000 })
  })
  await page.getByRole('link', { name: /Decisions/ }).click()
  await step('the Decisions list after the passkey sign-in, with its badge', async () => {
    await page.getByText('Turn on the nightly backup check?').waitFor({ timeout: 5000 })
    if ((await page.textContent('#badge')) !== '4') throw new Error('badge is not 4')
  })
  await page.getByText('Turn on the nightly backup check?').click()
  await page.getByRole('button', { name: 'Choose: Do it' }).click()
  await step('an answered card leaves Decisions at once and for good: only cards waiting on him are listed', async () => {
    await page.getByText('Answered "Turn on the nightly backup check?": Do it.', { exact: true }).waitFor({ timeout: 5000 })
    if (!page.url().endsWith('/m/decisions')) throw new Error('url ' + page.url())
    if (await page.getByRole('heading', { name: 'Answered' }).count()) throw new Error('an Answered section')
    if (await page.getByRole('button', { name: /^Turn on the nightly backup check\?/ }).count()) throw new Error('the answered card is still listed')
    if ((await page.textContent('#badge')) !== '3') throw new Error('badge is not 3')
    await page.reload()
    await page.getByRole('heading', { name: /^Waiting on you/ }).waitFor({ timeout: 5000 })
    if (await page.getByText('Turn on the nightly backup check?').count()) throw new Error('the answered card is back after a reload')
  })
  await page.goto(base + '/m/decisions/' + card)
  await step('an answered card opened by its link has its receipt', () => page.getByText(/^Answered "Do it" at /).waitFor({ timeout: 5000 }))
  await step('a card says when its alert was queued and whether it was sent, to the second', async () => {
    const t = await page.locator('.alert-times').textContent({ timeout: 3000 })
    if (!/^Alert: card \d{1,2}:\d{2}:\d{2}\s?[AP]M, not sent \(\w+\)$/.test(t)) throw new Error('alert line ' + JSON.stringify(t))
  })
  await step('an answered card offers no more choices', async () => {
    if (await page.getByRole('button', { name: /^Choose: / }).count()) throw new Error('choices on an answered card')
  })
  await page.goto(base + '/m/decisions/' + high)
  await page.getByRole('button', { name: 'Choose: Wait' }).waitFor({ timeout: 5000 })
  // First the prompt is dismissed: the button shows, beside what is being approved, and nothing is answered.
  await cdp.send('WebAuthn.setUserVerified', { authenticatorId, isUserVerified: false })
  await page.getByRole('button', { name: 'Choose: Wait' }).click()
  await step('a high-risk card asks again on its own screen', () => page.getByText('Check this answer').waitFor({ timeout: 3000 }))
  await step('a dismissed prompt on a high-risk answer shows its button and answers nothing', async () => {
    await page.getByText('The passkey did not sign in. Nothing changed.').waitFor({ timeout: 8000 })
    if (!(await page.getByRole('button', { name: 'Answer Wait with your passkey' }).isVisible())) throw new Error('no button')
    await page.getByText('What yes covers: ').waitFor({ timeout: 3000 })
    if (await page.getByText(/^Answered "Delete the old test branches\?"/).count()) throw new Error('answered without the passkey')
  })
  await page.screenshot({ path: require('os').tmpdir() + '/tw-phone-high.png', fullPage: true })
  await cdp.send('WebAuthn.setUserVerified', { authenticatorId, isUserVerified: true })
  await page.getByRole('button', { name: 'Go back to the card' }).click()
  await page.getByRole('button', { name: 'Choose: Wait' }).click()
  await step('then the passkey prompt opens by itself and answers it, with no button', async () => {
    await page.getByText('Answered "Delete the old test branches?": Wait.', { exact: true }).waitFor({ timeout: 8000 })
  })
  await step('four tabs: Bots, Rooms, Board and Decisions', async () => {
    const tabs = (await page.locator('#tabs a').allTextContents()).map(t => t.replace(/\s*\d*\s*$/, '').trim())
    if (JSON.stringify(tabs) !== JSON.stringify(['Bots', 'Rooms', 'Board', 'Decisions'])) throw new Error(JSON.stringify(tabs))
  })
  await page.getByRole('link', { name: 'Bots' }).click()
  await step('the Thornswood Bots list: Hermes, One, then the six Bots', async () => {
    await page.getByRole('heading', { name: 'Thornswood Bots' }).waitFor({ timeout: 5000 })
    const rows = await page.locator('#view > button.row strong').allTextContents()
    const want = ['Hermes', 'One', 'Bramble', 'Rift', 'Sprocket', 'Echo', 'Magpie', 'Wisp']
    if (JSON.stringify(rows) !== JSON.stringify(want)) throw new Error(JSON.stringify(rows))
    await page.getByText('your own Hermes on the VM', { exact: true }).waitFor({ timeout: 3000 })
    await page.getByText('your Hermes on One', { exact: true }).waitFor({ timeout: 3000 })
    await page.getByText('router, merges', { exact: true }).waitFor({ timeout: 3000 })
    await page.getByText('Plan the week: three things').waitFor({ timeout: 3000 })
  })
  await step("each gateway's state in its own words", async () => {
    const tag = gw => page.locator(`#view > button.row[data-gateway="${gw}"] .rec`).first().textContent()
    if ((await tag('one')) !== 'On') throw new Error('One: ' + (await tag('one')))
    if ((await tag('me')) !== 'Connector missing') throw new Error('Hermes: ' + (await tag('me')))
  })
  await step('each one shows online or offline from what is running: a dot on its avatar, and words', async () => {
    const got = await page.evaluate(() => Object.fromEntries([...document.querySelectorAll('#view > button.row')].map(r => [
      r.dataset.bot || r.dataset.gateway,
      [(r.querySelector('.avwrap .dot') || {}).className, (r.querySelector('.avwrap .dot') || { getAttribute: () => null }).getAttribute('aria-label'),
        (r.querySelector('.rec.live') || {}).textContent || null]])))
    const want = { one: ['dot on', 'On', null], me: ['dot off', 'Connector missing', null],
      bramble: ['dot on', 'On', 'On'], rift: ['dot off', 'Offline', 'Offline'], sprocket: ['dot wait', 'Starting', 'Starting'],
      echo: ['dot wait', 'Not heard from', 'Not heard from'], wisp: ['dot wait', 'Not heard from', 'Not heard from'] }
    for (const [k, v] of Object.entries(want)) {
      if (JSON.stringify(got[k]) !== JSON.stringify(v)) throw new Error(`${k}: ${JSON.stringify(got[k])}`)
    }
  })
  await page.screenshot({ path: require('os').tmpdir() + '/tw-phone-bots.png', fullPage: true })
  await page.getByText('Bramble', { exact: true }).click()
  await step("Bramble's Bot Chat, as text, under Bots", async () => {
    await page.getByText('All green <i>tonight</i>.').waitFor({ timeout: 5000 })
    if (!page.url().endsWith('/m/bots/bramble')) throw new Error('url ' + page.url())
    if ((await page.locator('#tabs a[aria-current="page"]').textContent()) !== 'Bots') throw new Error('not on the Bots tab')
  })
  await step("the Bot Chat's bar shows Bramble is on by its dot, with no line of words under it", async () => {
    if (await page.locator('#view .chatbar .t span').count()) throw new Error(await page.locator('#view .chatbar .t span').first().textContent())
    const d = page.locator('#view .chatbar .avwrap .dot')
    if ((await d.getAttribute('class')) !== 'dot on' || (await d.getAttribute('aria-label')) !== 'On') throw new Error('no green dot')
  })
  await page.getByLabel('Message to Bramble').fill('Thanks, and look at the launcher log next.')
  await page.getByRole('button', { name: 'Send to Bramble' }).click()
  await step('a message to Bramble waits for the connector', async () => {
    await page.getByText('Thanks, and look at the launcher log next.').waitFor({ timeout: 5000 })
    await page.getByText('Waiting to send').waitFor({ timeout: 3000 })
  })
  await page.screenshot({ path: require('os').tmpdir() + '/tw-phone-bramble.png', fullPage: true })
  await page.getByRole('button', { name: 'Older messages' }).click()
  await step("Bramble's Bot Chat pages back to its first message, from Hermes itself", async () => {
    await page.getByText('bramble 0', { exact: true }).waitFor({ timeout: 15000 })
    const texts = await page.locator('#view .msg[data-mid] .text').allTextContents()
    const want = [...Array.from({ length: 68 }, (_, i) => `bramble ${i}`), 'How is the nightly?', 'All green <i>tonight</i>.']
    if (JSON.stringify(texts) !== JSON.stringify(want)) throw new Error(texts.length + ' ' + texts.slice(0, 2))
  })
  await step("in a Bot Chat, the Bot's own reaction shows with its name", () =>
    page.locator('#view .msg[data-mid="69"] .react[aria-label="Bramble\'s reaction \u{1F440}"]').waitFor({ timeout: 5000 }))
  await page.locator('#view .msg[data-mid="70"] .bub').click()
  await page.getByRole('button', { name: 'React \u2764\uFE0F' }).click()
  await step('a reaction in a Bot Chat is set in that Bot\'s Bot Chat', () =>
    page.locator('#view .msg[data-mid="70"] .react.me:not(.pending)').waitFor({ timeout: 25000 }))
  await page.goto(base + '/m/bramble')
  await step("Bramble's first path still opens its chat, under Bots", async () => {
    await page.getByLabel('Message to Bramble').waitFor({ timeout: 5000 })
    if ((await page.locator('#tabs a[aria-current="page"]').textContent()) !== 'Bots') throw new Error('not on the Bots tab')
  })
  await page.getByRole('link', { name: 'Bots' }).click()
  await page.getByText('Rift', { exact: true }).click()
  await step("Rift's chat opens on its own deep link", async () => {
    await page.getByLabel('Message to Rift').waitFor({ timeout: 5000 })
    if (!page.url().endsWith('/m/bots/rift')) throw new Error('url ' + page.url())
  })
  await step("the card Rift asked waits in Rift's chat with its options, and a message naming one answers it", async () => {
    const c = page.locator('.chat-cards .card', { hasText: 'Rebuild the load order now?' })
    await c.waitFor({ timeout: 5000 })
    for (const o of ['Do it', 'Wait']) await c.getByRole('button', { name: o, exact: true }).waitFor({ timeout: 2000 })
    if (await page.locator('.chat-cards', { hasText: 'Move the backup to Sunday?' }).count()) throw new Error("Bramble's card is in Rift's chat")
    const box = page.getByLabel('Message to Rift')
    await box.fill('Hello Rift')
    if (await page.locator('.answering').isVisible()) throw new Error('words that name no option answer a card')
    await box.fill('wait, until tonight please')
    await page.locator('.answering', { hasText: 'Rebuild the load order now?' }).getByText('Wait', { exact: true }).waitFor({ timeout: 2000 })
    await page.getByRole('button', { name: 'Send without answering the card' }).click()
    if (await page.locator('.answering').isVisible()) throw new Error('the x left the answer on')
    await box.fill('Wait, until tonight please, really')
    if (await page.locator('.answering').isVisible()) throw new Error('the x did not hold while he kept typing')
    await box.fill('')
    await box.fill('Wait, until tonight please')
    await page.locator('.answering', { hasText: 'Rebuild the load order now?' }).waitFor({ timeout: 2000 })
    const sent = page.waitForRequest(q => q.method() === 'POST' && /\/chat\/rift$/.test(q.url()))
    await page.getByRole('button', { name: 'Send to Rift' }).click()
    const body = JSON.parse((await sent).postData())
    if (!body.answer || body.answer.option_key !== 'b' || body.text !== 'Wait, until tonight please') throw new Error('send body ' + JSON.stringify(body))
    await page.getByText('Wait, until tonight please').waitFor({ timeout: 5000 })
    await page.locator('.chat-cards').waitFor({ state: 'detached', timeout: 5000 })
    // The badge drops at once (Bramble's third card is the one left), not at the next poll.
    await page.waitForFunction(() => document.getElementById('badge').textContent === '1', null, { timeout: 3000 })
  })
  await page.getByRole('link', { name: 'Bots' }).click()
  await page.getByText('Its chats show here once it is turned on.').waitFor({ timeout: 5000 })
  await page.getByText('One', { exact: true }).click()
  await step("One's chats, signed in, with no line about the passkey's 30 minutes", async () => {
    await page.getByText('Plan the week', { exact: true }).waitFor({ timeout: 5000 })
    await page.getByText('Untitled chat', { exact: true }).waitFor({ timeout: 3000 })
    await page.getByLabel('Start a new chat').waitFor({ timeout: 3000 })
    if (await page.getByText(/passkey sign-in|Full power until/).count()) throw new Error('the passkey line is back')
    if (!page.url().endsWith('/m/hermes/one')) throw new Error('url ' + page.url())
  })
  await page.getByLabel('Start a new chat').fill('Start a list for the weekend.')
  await page.getByRole('button', { name: 'Start a new chat' }).click()
  await step('a new chat waits under Starting', async () => {
    await page.getByRole('heading', { name: 'Starting' }).waitFor({ timeout: 5000 })
    await page.getByText('Start a list for the weekend.').waitFor({ timeout: 3000 })
    await page.getByText('Waiting to send').waitFor({ timeout: 3000 })
  })
  await page.getByText('Plan the week', { exact: true }).click()
  await step('a chat on One, as text, on its own deep link', async () => {
    await page.getByText('<b>three</b> things').waitFor({ timeout: 15000 })
    if (!page.url().endsWith('/m/hermes/one/s_plan')) throw new Error('url ' + page.url())
    if (await page.getByText(/You can send until|\/yolo on/).count()) throw new Error('the send-window paragraph is back')
  })
  await page.getByLabel('Message to One').fill('Add the backup check.')
  await page.getByRole('button', { name: 'Send to One' }).click()
  await step('a message to One waits for its connector', async () => {
    await page.getByText('Add the backup check.').waitFor({ timeout: 5000 })
    await page.getByText('Waiting to send').waitFor({ timeout: 3000 })
  })
  await step('the + in the box offers a photo, his photos and his files, and a picked file goes with the message', async () => {
    await page.getByRole('button', { name: 'Add photos or files' }).click()
    const sheet = page.getByRole('dialog', { name: 'Add photos or files' })
    for (const name of ['Take a photo', 'Photos', 'Files']) await sheet.getByRole('button', { name }).waitFor({ timeout: 3000 })
    const chooser = page.waitForEvent('filechooser')
    await sheet.getByRole('button', { name: 'Files' }).click()
    await (await chooser).setFiles({ name: 'build notes.txt', mimeType: 'text/plain', buffer: Buffer.from('the backup ran at 3 AM\n') })
    await page.locator('.files .file.ok', { hasText: 'build notes.txt' }).waitFor({ timeout: 5000 })
    const sent = page.waitForRequest(q => q.method() === 'POST' && /\/own\/one\/send$/.test(q.url()))
    await page.getByRole('button', { name: 'Send to One' }).click()
    const body = JSON.parse((await sent).postData())
    if (body.text !== '' || !Array.isArray(body.attachments) || body.attachments.length !== 1) {
      throw new Error('send body ' + JSON.stringify(body))
    }
    await page.locator('#view .msg.mine .att', { hasText: 'build notes.txt' }).waitFor({ timeout: 5000 })
    if (await page.locator('.files').count()) throw new Error('the picked file stayed above the box after sending')
  })
  await step("Hermes's own reaction shows on the message it is on", () =>
    page.locator('#view .msg[data-mid="1"] .react[aria-label="Hermes\'s reaction \u{1F440}"]').waitFor({ timeout: 5000 }))
  const reactBody = []
  page.on('request', q => { if (q.method() === 'POST' && /\/own\/one\/react$/.test(q.url())) reactBody.push(JSON.parse(q.postData())) })
  await page.locator('#view .msg[data-mid="2"] .bub').click()
  await step('tapping a message opens its actions: eight reactions, Copy text and Close', async () => {
    const sheet = page.getByRole('dialog', { name: 'Message actions' })
    await sheet.waitFor({ timeout: 3000 })
    if (await sheet.getByRole('button', { name: /^React / }).count() !== 8) throw new Error('not eight reactions')
    await sheet.getByRole('button', { name: 'Copy text' }).waitFor({ timeout: 1000 })
    if (await sheet.getByRole('button', { name: 'Take my reaction off' }).count()) throw new Error('take off with no reaction')
  })
  await page.getByRole('button', { name: 'React \u{1F44D}' }).click()
  const mineChip = page.locator('#view .msg[data-mid="2"] .react.me')
  await step('a reaction shows at once, and asks the core for exactly that message and emoji', async () => {
    await mineChip.waitFor({ timeout: 5000 })
    if (await page.getByRole('dialog').count()) throw new Error('the sheet stayed open')
    if (JSON.stringify(reactBody) !== JSON.stringify([{ session_id: 's_plan', message_id: 2, emoji: '\u{1F44D}' }])) throw new Error(JSON.stringify(reactBody))
  })
  await step("once One says it is set, the chip is no longer dashed", async () => {
    await page.locator('#view .msg[data-mid="2"] .react.me:not(.pending)').waitFor({ timeout: 25000 })
    if (await mineChip.getAttribute('aria-label') !== 'Your reaction \u{1F44D}, tap to take it off') throw new Error(await mineChip.getAttribute('aria-label'))
  })
  await page.locator('#view .msg[data-mid="2"] .bub').click()
  await step('the actions mark the reaction that is on', async () => {
    if (await page.getByRole('button', { name: 'React \u{1F44D}' }).getAttribute('aria-pressed') !== 'true') throw new Error('not pressed')
  })
  await page.getByRole('button', { name: 'Take my reaction off' }).click()
  await step('taking it off asks for none, and the chip goes once One has taken it off', async () => {
    if (JSON.stringify(reactBody[1]) !== JSON.stringify({ session_id: 's_plan', message_id: 2, emoji: null })) throw new Error(JSON.stringify(reactBody))
    await page.locator('#view .msg[data-mid="2"] .react').waitFor({ state: 'detached', timeout: 25000 })
    await page.locator('#view .msg[data-mid="1"] .react').waitFor({ timeout: 1000 })
  })
  await page.screenshot({ path: require('os').tmpdir() + '/tw-phone-own-chat.png', fullPage: true })
  await page.goto(base + '/m/hermes/one')
  await page.getByLabel('Search chat titles').fill('long')
  await page.getByRole('button', { name: 'Search', exact: true }).click()
  await step("the picker searches One's chat titles", async () => {
    await page.getByRole('heading', { name: 'Chats with "long" in the title' }).waitFor({ timeout: 15000 })
    const rows = await page.locator('#view button.row[data-chat] strong').allTextContents()
    if (JSON.stringify(rows) !== JSON.stringify(['A long chat'])) throw new Error(JSON.stringify(rows))
  })
  await page.getByText('A long chat', { exact: true }).click()
  await step('a long chat opens on its newest 50 messages, from Hermes itself', async () => {
    await page.getByText('message 119', { exact: true }).waitFor({ timeout: 15000 })
    if (await page.getByText('message 69', { exact: true }).count()) throw new Error('more than the newest page')
  })
  const inView = text => page.getByText(text, { exact: true }).evaluate(n => {
    const r = n.getBoundingClientRect()
    return r.top >= 0 && r.bottom <= innerHeight
  })
  await step('a chat opens on its newest message', async () => {
    if (!(await inView('message 119'))) throw new Error('message 119 is not on screen')
  })
  await page.getByRole('button', { name: 'Older messages' }).click()
  await page.getByText('message 20', { exact: true }).waitFor({ timeout: 15000 })
  await step('older messages keep the place: the message that was first stays on screen', async () => {
    if (!(await inView('message 70'))) throw new Error('message 70 is not on screen')
  })
  await page.getByRole('button', { name: 'Older messages' }).click()
  await step('older messages page back to the first, in order, with no gaps or repeats', async () => {
    await page.getByText('message 0', { exact: true }).waitFor({ timeout: 15000 })
    const texts = await page.locator('#view .msg[data-mid] .text').allTextContents()
    const want = Array.from({ length: 120 }, (_, i) => `message ${i}`)
    if (JSON.stringify(texts) !== JSON.stringify(want)) throw new Error(texts.length + ' ' + texts.slice(0, 3))
    if (await page.getByRole('button', { name: 'Older messages' }).count()) throw new Error('more offered at the first')
  })
  const jump = page.locator('#view .chat .jump')
  await step('read further up, a chat offers Newest over the box, and it goes to the newest message', async () => {
    await jump.waitFor({ state: 'visible', timeout: 3000 })
    if ((await jump.textContent()).trim() !== 'Newest') throw new Error(await jump.textContent())
    await jump.click()
    await page.waitForFunction(() => innerHeight + scrollY >= document.documentElement.scrollHeight - 2, null, { timeout: 3000 })
    if (!(await inView('message 119'))) throw new Error('message 119 is not on screen')
    await jump.waitFor({ state: 'hidden', timeout: 3000 })
  })
  await step('a chat left for the list opens on its newest message again, and the list opens at its top', async () => {
    await page.evaluate(() => scrollTo({ top: 0 }))
    await page.getByRole('button', { name: 'All chats on One' }).click()
    await page.getByText('A long chat', { exact: true }).waitFor({ timeout: 15000 })
    const listAt = await page.evaluate(() => scrollY)
    if (listAt !== 0) throw new Error('the list opened at ' + listAt)
    await page.getByText('A long chat', { exact: true }).click()
    await page.getByText('message 119', { exact: true }).waitFor({ timeout: 15000 })
    await page.waitForTimeout(300)
    if (!(await inView('message 119'))) throw new Error('message 119 is not on screen')
    if (!(await jump.isHidden())) throw new Error('Newest is offered at the newest message')
  })
  await step('Back and Forward open the chat on its newest message too', async () => {
    await page.evaluate(() => scrollTo({ top: 0 }))
    await page.goBack()
    await page.getByText('A long chat', { exact: true }).waitFor({ timeout: 15000 })
    await page.goForward()
    await page.getByText('message 119', { exact: true }).waitFor({ timeout: 15000 })
    await page.waitForTimeout(300)
    if (!(await inView('message 119'))) throw new Error('message 119 is not on screen')
  })
  // Android closes the app between opens: a fresh load keeps only the phone's copy.
  const openTime = async () => {
    const t0 = Date.now()
    await page.goto(base + '/m/hermes/one/s_long')
    await page.getByText('message 119', { exact: true }).waitFor({ timeout: 30000 })
    return Date.now() - t0
  }
  await step("a chat opens at once from the phone's copy, without waiting for One", async () => {
    const pageUrl = '**/api/v1/own/one/chats/s_long/messages*'
    // One taking 6 seconds to answer, as a claim round trip to One can.
    await page.route(pageUrl, r => { setTimeout(() => r.continue().catch(() => {}), 6000) })
    await page.evaluate(() => localStorage.removeItem('hermes.kept.v1'))
    const before = await openTime()
    const after = await openTime()
    await page.unroute(pageUrl)
    await page.waitForTimeout(6500)
    console.log(`MEASURE open a 120-message chat with One 6 s slow: no copy (as before) ${before} ms, from the phone's copy ${after} ms`)
    if (before < 5000) throw new Error('the slow page was not waited for without a copy: ' + before + ' ms')
    if (after > 3000) throw new Error('waited for One: ' + after + ' ms')
    if (!(await inView('message 119'))) throw new Error('message 119 is not on screen')
  })
  await page.goto(base + '/m/hermes/one')
  await step('a chat Hermes has not titled is named by its first message, and its line shows words, not a file path', async () => {
    const row = page.locator('#view [data-chat="s_paste"]')
    await row.waitFor({ timeout: 15000 })
    const text = await row.textContent()
    if (!text.includes('Here is the plan')) throw new Error(text)
    if (/@file:|Attached Context|pasted_content/.test(text)) throw new Error(text)
  })
  await page.goto(base + '/m/hermes/one/s_paste')
  await step('a pasted file shows as a chip that opens its text, never as @file paths and Attached Context', async () => {
    const chip = page.locator('#view .msg.mine details.att')
    await chip.waitFor({ timeout: 15000 })
    if ((await chip.locator('summary').textContent()).trim() !== 'Pasted text \u00B7 40 tokens') throw new Error(await chip.locator('summary').textContent())
    const mine = await page.locator('#view .msg.mine .bub').textContent()
    if (/@file:|Attached Context|pasted_content/.test(mine)) throw new Error(mine)
    if (!mine.includes('Here is the plan')) throw new Error(mine)
    await chip.locator('summary').click()
    await page.getByText(/^Step 1\. gh pr merge 55/).waitFor({ timeout: 3000 })
  })
  await step("a reply to a paste quotes the paste's words, drawn as they are, and never its file reference", async () => {
    const mine = page.locator('#view .msg.mine', { hasText: 'Here is the plan' })
    if (await page.locator('.sheet-back').count()) throw new Error("opening the chip opened the message's actions too")
    await mine.locator('.bub .md').click()
    await page.getByRole('dialog', { name: 'Message actions' }).getByRole('button', { name: 'Reply' }).click()
    const quote = page.locator('#view .replying .qtext')
    await quote.waitFor({ timeout: 8000 })
    const words = await quote.textContent()
    if (/@file:|Attached Context|pasted_content/.test(words) || !words.includes('Here is the plan')) throw new Error(words)
    await page.getByRole('button', { name: 'Cancel the reply' }).click()
    await page.locator('#view .replying').waitFor({ state: 'detached', timeout: 8000 })
  })
  await step('a long command in a code block wraps on the phone, and Copy still copies it whole', async () => {
    const fit = await page.locator('#view .msg.theirs .md-code pre').evaluate(n => ({ s: n.scrollWidth, c: n.clientWidth }))
    if (fit.s > fit.c + 1) throw new Error(JSON.stringify(fit))
    const wide = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)
    if (wide > 0) throw new Error('sideways scroll of ' + wide)
  })
  await page.goto(base + '/m/hermes/one/s_old')
  await step('a chat Hermes no longer has says so and offers no send', async () => {
    await page.getByText('This chat is gone from One. Nothing sent here would reach it.').waitFor({ timeout: 15000 })
    if (await page.getByLabel('Send to One').count()) throw new Error('a composer for a gone chat')
  })
  await page.goto(base + '/m/hermes/one/s_never')
  await step('a chat One never listed does not open', () =>
    page.getByText('One has not listed this chat, so it does not open here.').waitFor({ timeout: 5000 }))
  await page.goto(base + '/m/hermes/one/s_work')
  await step('a message being answered shows one live line saying what its turn is doing', async () => {
    const live = page.locator('#view .msg.working')
    await live.waitFor({ timeout: 15000 })
    if (await live.getAttribute('role') !== 'status') throw new Error('the live line is not a status')
    if (await live.locator('.dots').count() !== 1) throw new Error('no working dots')
    if (await live.locator('.doing').textContent() !== 'Running a command') throw new Error('doing: ' + await live.locator('.doing').textContent())
    if (await page.locator('#view .msg.working').count() !== 1) throw new Error('more than one live line')
  })
  await page.goto(base + '/m/bots/wisp')
  await step("a Bot answering his message shows the same live line in its Bot Chat", async () => {
    const live = page.locator('#view .msg.working')
    await live.waitFor({ timeout: 15000 })
    if (await live.locator('.who').textContent() !== 'Wisp') throw new Error('the line is not from Wisp')
    if (await live.locator('.doing').textContent() !== 'Writing') throw new Error('doing: ' + await live.locator('.doing').textContent())
    if (await page.locator('#view .msg.working').count() !== 1) throw new Error('more than one live line')
  })
  await page.goto(base + '/m/hermes/one/s_md')
  await step("Markdown is drawn: bold, italic, code, lists, a link, a code block, a table and a quote", async () => {
    const msg = page.locator('#view .msg[data-mid]').first()
    await msg.locator('strong', { hasText: 'Bold' }).waitFor({ timeout: 15000 })
    if (await msg.locator('em').textContent() !== 'italic') throw new Error('italic')
    if (await msg.locator('p code').textContent() !== 'code') throw new Error('inline code')
    if (!(await msg.textContent()).includes('a_snake_name kept')) throw new Error('an underscore name was changed')
    if (JSON.stringify(await msg.locator('ul > li').evaluateAll(n => n.map(li => li.firstChild.textContent))) !== '["one","two"]') throw new Error('the list')
    if (await msg.locator('ul > li ol > li').textContent() !== 'nested') throw new Error('the nested list')
    const safe = msg.locator('a', { hasText: 'safe link' })
    if (await safe.getAttribute('href') !== 'https://example.com/x') throw new Error('the link')
    if (await safe.getAttribute('target') !== '_blank' || !/noopener/.test(await safe.getAttribute('rel'))) throw new Error('the link opens in place')
    if (await msg.locator('a').count() !== 1) throw new Error('a link that is not http or https')
    if (await msg.locator('img').count()) throw new Error('HTML in a message was drawn')
    if (!(await msg.textContent()).includes('[bad](javascript:alert(1)) and <img src=x onerror=alert(1)>')) throw new Error('the unsafe text is not shown as text')
    if (await msg.locator('.md-code pre').textContent() !== 'print("hi")') throw new Error('the code block')
    if (await msg.locator('.md-code-bar span').textContent() !== 'python') throw new Error('the code block language')
    if (JSON.stringify(await msg.locator('th').allTextContents()) !== '["a","b"]') throw new Error('the table head')
    if (JSON.stringify(await msg.locator('td').allTextContents()) !== '["1","2"]') throw new Error('the table row')
    if (await msg.locator('blockquote').textContent() !== 'quoted') throw new Error('the quote')
  })
  await step('Copy on a code block copies it, or selects it where the clipboard is refused', async () => {
    const copy = page.locator('#view .md-code .copy')
    await copy.click()
    await page.waitForFunction(() => /^(Copied|Selected)$/.test(document.querySelector('#view .md-code .copy').textContent), null, { timeout: 3000 })
  })
  await step('a chat fits the phone: no sideways scroll, and the box sits above the tabs', async () => {
    const m = await page.evaluate(() => ({
      wide: document.documentElement.scrollWidth - innerWidth,
      dock: document.querySelector('#view .dock').getBoundingClientRect().bottom,
      tabs: document.getElementById('tabs').getBoundingClientRect().top
    }))
    if (m.wide > 0) throw new Error('sideways scroll of ' + m.wide)
    if (m.dock > m.tabs + 0.5) throw new Error(`the box ends at ${m.dock}, under the tabs at ${m.tabs}`)
  })
  await page.screenshot({ path: require('os').tmpdir() + '/tw-phone-markdown.png' })
  // Renaming, hiding, bringing back and deleting one of his own chats (redesign step 3). The scratch
  // connector does each on its state.db and feeds the chat list again; the app reads it on its next draw.
  const again = async (path, test) => {
    for (let i = 0; i < 20; i++) {
      await page.goto(base + path)
      await page.locator('#view').filter({ hasNotText: 'Loading...' }).waitFor({ timeout: 5000 })
      await page.waitForTimeout(250)
      if (await test()) return
    }
    throw new Error('never came to pass on ' + path)
  }
  await page.getByRole('button', { name: 'Chat actions' }).click()
  await step('a chat of his own has its actions: Rename, Hide and Delete for good, and no reactions', async () => {
    const sheet = page.getByRole('dialog', { name: 'Chat actions' })
    for (const name of ['Rename', 'Hide this chat', 'Delete for good...']) await sheet.getByRole('button', { name }).waitFor({ timeout: 3000 })
    if ((await page.getByLabel('New title').inputValue()) !== 'Markdown check') throw new Error('the title box does not hold its title')
  })
  await page.getByLabel('New title').fill('  Markdown   look ')
  await page.getByRole('button', { name: 'Rename', exact: true }).click()
  await step('a rename shows at once as asked, then the chat has its new name from One', async () => {
    // The scratch connector checks in every 0.3 s, so the rename may be done before the chat redraws:
    // then the chat shows the new name at once instead of the line saying it is on its way.
    await page.waitForFunction(() => document.body.innerText.includes('Renaming to "Markdown look".') ||
      document.querySelector('.chatbar .t strong')?.textContent === 'Markdown look', null, { timeout: 5000 })
    await again('/m/hermes/one/s_md', async () => (await page.locator('.chatbar .t strong').textContent()) === 'Markdown look')
  })
  await page.getByRole('button', { name: 'Chat actions' }).click()
  await page.getByRole('button', { name: 'Hide this chat' }).click()
  await step('a hidden chat says so, offers no send, and is off the chat list', async () => {
    await again('/m/hermes/one/s_md', () => page.getByText(/^Hidden from One's chat list/).count())
    if (await page.getByLabel('Message to One').count()) throw new Error('a composer on a hidden chat')
    await page.goto(base + '/m/hermes/one')
    await page.getByRole('button', { name: /^Plan the week/ }).waitFor({ timeout: 5000 })
    if (await page.locator('#view .row[data-chat="s_md"]').count()) throw new Error('the hidden chat is still listed')
  })
  await page.getByRole('button', { name: 'Hidden chats' }).click()
  await step('Hidden chats lists it from One itself, with Bring back', () =>
    page.locator('#view .row[data-chat="s_md"]').getByRole('button', { name: 'Bring back' }).waitFor({ timeout: 8000 }))
  await page.screenshot({ path: require('os').tmpdir() + '/tw-phone-hidden.png' })
  await page.locator('#view .row[data-chat="s_md"]').getByRole('button', { name: 'Bring back' }).click()
  await step('Bring back shows as asked, then the chat is on the list again', async () => {
    await page.getByText(/^Bringing it back\./).waitFor({ timeout: 5000 })
    await again('/m/hermes/one', () => page.locator('#view .row[data-chat="s_md"]').count())
  })
  await page.goto(base + '/m/hermes/one/s_md')
  await page.getByRole('button', { name: 'Chat actions' }).click()
  await page.getByRole('button', { name: 'Delete for good...' }).click()
  await step('Delete for good says exactly what goes before anything is deleted', async () => {
    await page.getByText('Delete "Markdown look" for good?').waitFor({ timeout: 3000 })
    await page.getByText(/all its messages, the earlier parts Hermes compressed it from, and the side tasks Hermes ran for it/).waitFor({ timeout: 3000 })
    await page.getByText(/no other chat, no Bot and no room/).waitFor({ timeout: 3000 })
  })
  await page.screenshot({ path: require('os').tmpdir() + '/tw-phone-delete.png' })
  await page.getByRole('button', { name: 'Keep it' }).click()
  await step('Keep it deletes nothing', async () => {
    await page.waitForTimeout(800)
    await page.goto(base + '/m/hermes/one')
    await page.locator('#view .row[data-chat="s_md"]').waitFor({ timeout: 5000 })
  })
  await page.goto(base + '/m/hermes/one/s_md')
  await page.getByRole('button', { name: 'Chat actions' }).click()
  await page.getByRole('button', { name: 'Delete for good...' }).click()
  await page.getByRole('button', { name: 'Delete for good', exact: true }).click()
  await step('a delete goes through, the chat says it is gone and offers no send, and it is off the list', async () => {
    await again('/m/hermes/one/s_md', () => page.getByText('Deleted from One for good.').count())
    if (await page.getByLabel('Message to One').count()) throw new Error('a composer on a deleted chat')
    if (await page.getByRole('button', { name: 'Chat actions' }).count()) throw new Error('actions on a deleted chat')
    await page.goto(base + '/m/hermes/one')
    await page.getByRole('button', { name: /^Plan the week/ }).waitFor({ timeout: 5000 })
    if (await page.locator('#view .row[data-chat="s_md"]').count()) throw new Error('the deleted chat is still listed')
  })
  await page.goto(base + '/m/hermes/one')
  await step("his own Bot Chat heads One's chats, marked Bot Mode", async () => {
    const first = page.locator('#view .row[data-chat]').first()
    if ((await first.getAttribute('data-chat')) !== 's_obc') throw new Error('the Bot Chat is not first')
    await first.getByText('Bot Mode').waitFor({ timeout: 3000 })
  })
  await page.goto(base + '/m/hermes/one/s_obc')
  await step('his own Bot Chat opens with its messages and offers no Rename, Hide or Delete', async () => {
    await page.getByText('Rift: load order is clean').first().waitFor({ timeout: 8000 })
    if (await page.getByRole('button', { name: 'Chat actions' }).count()) throw new Error('chat actions on his Bot Chat')
  })
  // A reply to one message (redesign step 4, reply style A): the quote goes at the top of the message as
  // sent, and the core keeps which message it answers.
  const replyBody = []
  page.on('request', q => { if (q.method() === 'POST' && /\/own\/one\/send$/.test(q.url())) replyBody.push(JSON.parse(q.postData())) })
  await page.goto(base + '/m/hermes/one/s_long')
  const last = page.locator('#view .msg.theirs', { hasText: 'message 119' })
  await last.waitFor({ timeout: 15000 })
  const lastMid = Number(await last.getAttribute('data-mid'))
  await last.locator('.bub').click()
  await page.getByRole('dialog', { name: 'Message actions' }).getByRole('button', { name: 'Reply' }).click()
  await step('Reply shows the words it quotes above the box, and Cancel takes it away', async () => {
    await page.locator('#view .replying', { hasText: 'Replying to One' }).waitFor({ timeout: 8000 })
    await page.locator('#view .replying', { hasText: 'message 119' }).waitFor({ timeout: 1000 })
    await page.getByRole('button', { name: 'Cancel the reply' }).click()
    await page.locator('#view .replying').waitFor({ state: 'detached', timeout: 8000 })
  })
  await last.locator('.bub').click()
  await page.getByRole('dialog', { name: 'Message actions' }).getByRole('button', { name: 'Reply' }).click()
  await page.locator('#view .replying').waitFor({ timeout: 8000 })
  await page.getByLabel('Message to One').fill('Thanks, that was the last one.')
  await page.getByRole('button', { name: 'Send to One' }).click()
  await step('a reply asks for exactly that message and those words, and shows the quote at the top', async () => {
    await page.locator('#view .msg.mine .quote', { hasText: 'message 119' }).first().waitFor({ timeout: 5000 })
    const b = replyBody[replyBody.length - 1] || {}
    if (b.reply_to !== lastMid || b.quote !== 'message 119' || b.text !== 'Thanks, that was the last one.' || b.session_id !== 's_long') throw new Error(JSON.stringify(b))
    if (await page.locator('#view .replying').count()) throw new Error('the reply preview stayed after sending')
  })
  await step('the chat reads itself again while its box has the focus, and the box and what is in it stay', async () => {
    // Patrick, 2 Nov: "Waiting to send" stayed on a message long after it had gone, because a chat was not
    // read again while its box had the focus, and putting the keyboard away leaves the focus there.
    const box = page.getByLabel('Message to One')
    await box.focus()
    await box.pressSequentially('half a thought')
    const handle = await box.elementHandle()
    await page.locator('#view .msg.mine[data-mid]:not([data-status])', { hasText: 'Thanks, that was the last one.' })
      .waitFor({ timeout: 45000 })
    if (!(await handle.evaluate(n => n.isConnected && n === document.activeElement && n.value === 'half a thought'))) {
      throw new Error('the box was drawn again under him')
    }
    await box.fill('')
  })
  await step("One's own copy of the reply keeps the quote, and the quote goes back to the message it answers", async () => {
    await again('/m/hermes/one/s_long', () => page.locator('#view .msg.mine[data-mid]:not([data-status]) .quote.linked').count())
    const copy = page.locator('#view .msg.mine[data-mid]:not([data-status])', { hasText: 'Thanks, that was the last one.' })
    await copy.locator('.quote.linked', { hasText: 'message 119' }).click()
    await page.locator(`#view .msg.flash[data-mid="${lastMid}"]`).waitFor({ timeout: 3000 })
  })
  await page.screenshot({ path: require('os').tmpdir() + '/tw-phone-reply.png' })
  await page.goto(base + '/m/bots/bramble')
  await page.locator('#view .msg[data-mid="70"] .bub').click()
  await step('a Bot Chat offers no reply: Hermes has none there', async () => {
    await page.getByRole('dialog', { name: 'Message actions' }).waitFor({ timeout: 3000 })
    if (await page.getByRole('dialog').getByRole('button', { name: 'Reply' }).count()) throw new Error('Reply in a Bot Chat')
    await page.keyboard.press('Escape')
  })
  await page.goto(base + '/m/bots/bramble')
  await step('a Bot Chat has no chat actions', async () => {
    await page.getByLabel('Message to Bramble').waitFor({ timeout: 5000 })
    if (await page.getByRole('button', { name: 'Chat actions' }).count()) throw new Error('chat actions on a Bot Chat')
  })
  await page.goto(base + '/m/hermes/me/s_plan')
  await step("One's chat id does not open on Hermes", () =>
    page.getByText('Hermes has not listed this chat, so it does not open here.').waitFor({ timeout: 5000 }))
  await page.goto(base + '/m/hermes/me')
  await step('Hermes on the VM, with no connector yet, says so and offers no send', async () => {
    await page.locator('#view h2', { hasText: /^Hermes$/ }).waitFor({ timeout: 5000 })
    await page.getByText('The connector on the VM is not installed yet.').waitFor({ timeout: 5000 })
    if (await page.getByLabel('Start a new chat').count()) throw new Error('a composer while it is off')
  })
  await page.getByRole('link', { name: 'Board' }).click()
  await step("the Board shows only the turns running now, in the Bots' order, as text", async () => {
    await page.getByText('Rebuild the <b>load order</b>').waitFor({ timeout: 5000 })
    const rows = await page.locator('.row.doing').evaluateAll(rs => rs.map(r => [r.dataset.bot,
      r.querySelector('.what').textContent, (r.querySelector('.t .quiet') || {}).textContent || '']))
    const want = [['rift', 'Running a command', 'Rebuild the <b>load order</b>'], ['sprocket', 'Reading a file', 'Installer log'],
      ['sprocket', 'Using the browser', 'Launcher news'], ['echo', 'Thinking', '']]
    if (JSON.stringify(rows) !== JSON.stringify(want)) throw new Error(JSON.stringify(rows))
    // Bramble, Magpie and Wisp run nothing: no row, no heading, no history.
    if (await page.locator('[data-bot="bramble"], [data-bot="magpie"], [data-bot="wisp"]').count()) throw new Error('an idle Bot')
    if (!/^(just now|\d+ min|\d+ h \d+ min)$/.test(await page.locator('.row.doing .w').first().textContent())) throw new Error('how long')
  })
  await step('the Board follows a change by itself, with no tap', async () => {
    require('fs').writeFileSync(boardNext, '')
    await page.locator('[data-bot="rift"] .what', { hasText: 'Thinking' }).waitFor({ timeout: 8000 })
  })
  await page.screenshot({ path: require('os').tmpdir() + '/tw-phone-board.png', fullPage: true })
  await page.getByRole('link', { name: 'Rooms' }).click()
  await step('the Rooms list has one button for each room', async () => {
    await page.getByText('Thornswood Workbench', { exact: true }).waitFor({ timeout: 5000 })
    await page.getByText('Second test room', { exact: true }).waitFor({ timeout: 3000 })
    await page.getByText('Bramble: <script>alert(1)</script> hello').waitFor({ timeout: 3000 })
  })
  await step("a list line is the message drawn the way it is, its sender apart, cut by lines", async () => {
    // Patrick, 2 Nov: "it should render properly everywhere in the app", not raw text and not plain words.
    const line = page.locator('#view .row', { hasText: 'Second test room' }).locator('.snip')
    await line.waitFor({ timeout: 3000 })
    if ((await line.locator('.sender').textContent()) !== 'Magpie: ') throw new Error('the sender: ' + await line.locator('.sender').textContent())
    if (await line.locator('.mention').textContent() !== '@Bramble') throw new Error('the mention')
    if (await line.locator('strong').textContent() !== "Why it isn't there yet:") throw new Error('the bold words')
    if ((await line.locator('code').allTextContents()).join('|') !== 'Launcher builds|needs: pick') throw new Error('the code')
    if (await line.locator('a').count() || await line.locator('.lnk').textContent() !== 'the guide') throw new Error('a link in a row is words, not a link')
    const text = await line.textContent()
    if (/\*\*|`|##|\]\(|^magpie/.test(text)) throw new Error('raw marks: ' + text)
    const clamp = await line.evaluate(n => getComputedStyle(n).webkitLineClamp)
    if (clamp !== '2') throw new Error('cut by lines: ' + clamp)
  })
  await step('no tab carries a line of explanation under its title', async () => {
    if (await page.locator('#view .bar .t > span').count()) throw new Error(await page.locator('#view .bar .t > span').first().textContent())
    if (/ as of |never an answer/.test(await page.locator('#view').textContent())) throw new Error('clutter')
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
    await page.getByText('Rebuild the <b>load order</b>').waitFor({ timeout: 5000 })
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
  await step('a push shows only "Hermes needs you", with the phone sound, tagged by its card', async () => {
    if (!reg) throw new Error('no service worker registration seen')
    await cdp.send('ServiceWorker.deliverPushMessage', { origin: base, registrationId: reg.registrationId,
      data: JSON.stringify({ v: 1, event_id: 'px_1', decision_id: card, attention_revision: 1, path: '/m/decisions/' + card }) })
    let list = []
    for (let i = 0; i < 50 && !list.length; i++) { list = await shown(); if (!list.length) await new Promise(ok => setTimeout(ok, 100)) }
    const want = { title: 'Hermes needs you', body: '', tag: 'decision:' + card, silent: false, path: '/m/decisions/' + card, actions: 0 }
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
  await step("the third card's alert shows", async () => {
    const list = await gone('decision:' + third, false)
    if (!list.some(n => n.tag === 'decision:' + third)) throw new Error(JSON.stringify(list))
  })
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
    await page.getByRole('heading', { name: /^Waiting on you/ }).waitFor({ timeout: 5000 })
    if (!page.url().endsWith('/m/decisions')) throw new Error('url ' + page.url())
  })
  await page.getByRole('button', { name: 'Not now' }).click()
  await step('the install hint goes away for this visit when told not now', async () => {
    if (await page.locator('#install').isVisible()) throw new Error('still shown')
  })
  // The fingerprint on the phone (Patrick, 2 Nov): the phone's own key, named in the sign-in so the phone
  // answers it itself instead of Android handing it to a passkey app. The virtual authenticator here is
  // the phone's platform one (transport internal, with user verification).
  await page.goto(base + '/m/decisions')
  await step('on a phone, Decisions offers the fingerprint', () =>
    page.getByText('Sign in with your fingerprint', { exact: true }).waitFor({ timeout: 5000 }))
  await page.getByRole('button', { name: 'Use my fingerprint' }).click()
  await step('the fingerprint is added beside the passkey, and the offer goes', async () => {
    await page.getByText('Fingerprint sign-in is on for this phone.').waitFor({ timeout: 8000 })
    if (await page.getByRole('button', { name: 'Use my fingerprint' }).count()) throw new Error('still offered')
    const k = await page.evaluate(() => JSON.parse(localStorage.getItem('hermes.phoneKey.v1') || 'null'))
    if (!k || !k.credential_id || !k.passkey_id) throw new Error('no key name kept: ' + JSON.stringify(k))
  })
  await page.goto(base + '/m/settings')
  await step('settings says the fingerprint is on, and lists it', async () => {
    await page.getByText(/^On\. This phone signs in and answers high-risk cards with your fingerprint\.$/).waitFor({ timeout: 5000 })
    await page.getByText('Fingerprint on this phone', { exact: true }).waitFor({ timeout: 3000 })
  })
  const optionBodies = []
  page.on('request', q => { if (new URL(q.url()).pathname === '/api/v1/passkeys/sign-in/options') optionBodies.push(JSON.parse(q.postData() || '{}')) })
  await page.getByText('Sign out').click()
  await step('signed out, the button says fingerprint and it signs in naming this phone\'s key', async () => {
    const button = page.getByRole('button', { name: 'Sign in with your fingerprint' })
    await button.waitFor({ timeout: 5000 })
    await button.click()
    await page.getByText('Add a passkey on this phone').waitFor({ timeout: 8000 })
    const k = await page.evaluate(() => JSON.parse(localStorage.getItem('hermes.phoneKey.v1')))
    if (JSON.stringify(optionBodies) !== JSON.stringify([{ phone_key: k.credential_id }])) throw new Error(JSON.stringify(optionBodies))
  })
  await step('high-risk answers, deletes and own-Hermes sends name the fingerprint too', async () => {
    if ((await page.evaluate(() => signInWord())) !== 'fingerprint') throw new Error('the word is not fingerprint')
  })
  // A key the core no longer has (removed with its phone): the app forgets its name and the passkey signs in.
  // (Settings drops a key name the core does not list, so the name is put back after it loads.)
  await page.goto(base + '/m/settings')
  await page.getByText('Sign out').waitFor({ timeout: 5000 })
  await step('settings forgets a fingerprint key the core no longer lists', async () => {
    await page.evaluate(() => localStorage.setItem('hermes.phoneKey.v1', JSON.stringify({ credential_id: 'AQEBAQEBAQEBAQEBAQEBAQ', passkey_id: 'pk_gone' })))
    await page.goto(base + '/m/settings')
    await page.getByRole('button', { name: 'Use my fingerprint' }).waitFor({ timeout: 5000 })
    if (await page.evaluate(() => localStorage.getItem('hermes.phoneKey.v1'))) throw new Error('kept')
  })
  await page.evaluate(() => localStorage.setItem('hermes.phoneKey.v1', JSON.stringify({ credential_id: 'AQEBAQEBAQEBAQEBAQEBAQ', passkey_id: 'pk_gone' })))
  optionBodies.length = 0
  await page.getByText('Sign out').click()
  await step('a removed fingerprint key falls back to the passkey and is forgotten', async () => {
    await page.getByRole('button', { name: 'Sign in with your fingerprint' }).click()
    await page.getByText('Add a passkey on this phone').waitFor({ timeout: 8000 })
    if (JSON.stringify(optionBodies) !== JSON.stringify([{ phone_key: 'AQEBAQEBAQEBAQEBAQEBAQ' }, {}])) throw new Error(JSON.stringify(optionBodies))
    if (await page.evaluate(() => localStorage.getItem('hermes.phoneKey.v1'))) throw new Error('the gone key is still kept')
  })
  // Nothing may widen the page (Patrick's Pixel 9a, 1 Nov): a link, a commit id or a branch name with no
  // space in it pushed the page past the phone's width, and the last tab went off-screen at normal zoom.
  // Each signed-in view gets one in every kind of text it shows, at Pixel widths and with larger text.
  const LONG = 'https://github.com/ThornswoodRP/Thornswood-Hermes/pull/44/commits/2e308db923bf40880adae89d38fedb307c18aa46'
  for (const [width, zoom] of [[412, 1], [360, 1], [412, 1.3], [1440, 1]]) {
    await page.setViewportSize({ width, height: 860 })
    for (const view of ['/m/bots', '/m/rooms', '/m/board', '/m/decisions', '/m/decisions/' + card, '/m/settings',
      '/m/hermes/one', '/m/hermes/one/s_plan', '/m/hidden/one', '/m/bots/bramble', '/m/room']) {
      await page.goto(base + view)
      await page.locator('#view').filter({ hasNotText: 'Loading...' }).waitFor({ timeout: 5000 })
      await page.waitForTimeout(300)
      await step(`a long link or id never widens ${view} (${width} wide, text at ${zoom * 100}%)`, async () => {
        const got = await page.evaluate(([long, zoom]) => {
          document.documentElement.style.zoom = String(zoom)
          for (const e of document.querySelectorAll('#view strong, #view h2, #view td, #view th, #view p, #view pre, ' +
            '#view .msg .text, #view .what, #view .quiet, #view summary, #view li')) e.append(' ' + long)
          const tabs = [...document.querySelectorAll('.tabs a')].map(a => a.getBoundingClientRect().right)
          return { page: document.documentElement.scrollWidth, screen: document.documentElement.clientWidth, tabs }
        }, [LONG, zoom])
        if (got.page > got.screen) throw new Error(`the page is ${got.page} wide on a ${got.screen} screen`)
        if (Math.max(...got.tabs) > got.screen + 1) throw new Error('a tab ends at ' + Math.max(...got.tabs))
      })
    }
  }
  // The desktop (2 Nov: One showed the phone's one column, in a browser and installed). The layout follows the
  // window's width alone: from 1000 the rail, the chat list and the chat; from 1360 Decisions on the right too.
  const layout = () => page.evaluate(() => {
    const shown = id => { const e = document.getElementById(id); return !!e && !e.hidden && getComputedStyle(e).display !== 'none' }
    const tabs = document.getElementById('tabs').getBoundingClientRect()
    return { list: shown('list'), side: shown('side'), agents: getComputedStyle(document.getElementById('agents')).display !== 'none',
      people: document.querySelectorAll('#agents .agent').length, railWidth: Math.round(tabs.width), railLeft: tabs.left,
      current: [...document.querySelectorAll('#list .row[aria-current]')].map(r => r.dataset.chat || r.dataset.bot || r.dataset.room),
      agent: (document.querySelector('#agents .agent[aria-current]') || {}).title || null,
      sideText: (document.getElementById('side').textContent || ''), view: document.getElementById('view').textContent,
      wider: document.documentElement.scrollWidth > document.documentElement.clientWidth }
  })
  await page.setViewportSize({ width: 1600, height: 900 })
  await step('a wide screen gets the desktop: the rail, the chat list with the open chat marked, the chat, and Decisions', async () => {
    await page.goto(base + '/m/hermes/one/s_plan')
    await page.locator('#list .row[data-chat="s_plan"][aria-current]').waitFor({ timeout: 5000 })
    await page.locator('#side').getByText(/^Waiting on you \(/).waitFor({ timeout: 5000 })
    const got = await layout()
    if (!got.list || !got.side || !got.agents || got.people !== 8) throw new Error(JSON.stringify(got))
    if (got.railLeft !== 0 || got.railWidth > 100) throw new Error('the tabs are not a rail on the left: ' + JSON.stringify(got))
    if (got.agent !== 'One') throw new Error('the rail marks ' + got.agent)
    if (!got.view.includes('Plan the week') || got.wider) throw new Error(JSON.stringify(got))
  })
  await step("the rail opens a Bot's chat, and the list beside it marks that Bot", async () => {
    await page.goto(base + '/m/bots')
    await page.locator('#view').getByText('Pick Hermes, One or a Bot on the left.').waitFor({ timeout: 5000 })
    await page.locator('#agents .agent[title="Bramble"]').click()
    await page.waitForURL(base + '/m/bots/bramble')
    await page.locator('#list .row[data-bot="bramble"][aria-current]').waitFor({ timeout: 5000 })
    const got = await layout()
    if (got.agent !== 'Bramble' || got.current.join() !== 'bramble') throw new Error(JSON.stringify(got))
  })
  await step('the chat list and Decisions each fold away from the rail, and come back', async () => {
    await page.goto(base + '/m/hermes/one/s_plan')
    await page.locator('#list .row[data-chat="s_plan"]').waitFor({ timeout: 5000 })
    await page.getByRole('button', { name: 'Hide the chat list' }).click()
    await page.getByRole('button', { name: 'Show the chat list' }).waitFor({ timeout: 5000 })
    await page.getByRole('button', { name: 'Hide Decisions' }).first().click()
    await page.getByRole('button', { name: 'Show Decisions' }).waitFor({ timeout: 5000 })
    let got = await layout()
    if (got.list || got.side || !got.view.includes('Plan the week')) throw new Error(JSON.stringify(got))
    await page.getByRole('button', { name: 'Show the chat list' }).click()
    await page.getByRole('button', { name: 'Show Decisions' }).click()
    await page.locator('#side').getByText(/^Waiting on you \(/).waitFor({ timeout: 5000 })
    got = await layout()
    if (!got.list || !got.side) throw new Error(JSON.stringify(got))
  })
  await step('Decisions is not shown twice: on its own tab it is the middle, with no pane on the right', async () => {
    await page.goto(base + '/m/decisions')
    await page.locator('#view').getByText(/^Waiting on you \(/).waitFor({ timeout: 5000 })
    const got = await layout()
    if (got.side || got.list) throw new Error(JSON.stringify(got))
  })
  await step('a narrower desktop window keeps the chat list and drops the right pane', async () => {
    await page.setViewportSize({ width: 1200, height: 800 })
    await page.goto(base + '/m/hermes/one/s_plan')
    await page.locator('#list .row[data-chat="s_plan"][aria-current]').waitFor({ timeout: 5000 })
    const got = await layout()
    if (!got.list || got.side || got.wider) throw new Error(JSON.stringify(got))
  })
  await step('narrowing the window to a phone redraws the phone layout, with the tabs at the foot', async () => {
    await page.setViewportSize({ width: 412, height: 860 })
    await page.waitForFunction(() => document.getElementById('list').hidden)
    const got = await layout()
    if (got.list || got.side || got.agents || got.railWidth < 400 || !got.view.includes('Plan the week')) throw new Error(JSON.stringify(got))
  })
  await page.setViewportSize({ width: 412, height: 860 })

  // ── voice ──
  const barMode = () => page.evaluate(() => { const b = document.getElementById('voicebar'); return b && !b.hidden ? b.dataset.mode : null })
  const saidAloud = text => page.evaluate(t => window.__said.includes(t), text)
  await page.goto(base + '/m/bots/bramble')
  await page.getByLabel('Message to Bramble').waitFor({ timeout: 5000 })
  await step('the microphone writes what he says into the box, with the bar saying so while it listens', async () => {
    if (await barMode()) throw new Error('the bar is up before a tap: ' + await barMode())
    await page.getByRole('button', { name: 'Speak instead of typing' }).click()
    await page.waitForFunction(() => /^dictate:/.test((document.getElementById('voicebar') || {}).dataset?.mode || ''), null, { timeout: 3000 })
    const words = await page.locator('#voicebar .vt').textContent()
    if (!/^(Listening|Hearing you)/.test(words)) throw new Error(words)
    await page.waitForFunction(w => document.querySelector('#view .composer textarea').value === w, VOICE, { timeout: 15000 })
    if (await barMode()) throw new Error('the bar stayed up: ' + await barMode())
  })
  await page.getByLabel('Message to Bramble').fill('')
  await step('voice chat sends what he says, reads the answer aloud, and listens again', async () => {
    await page.getByRole('button', { name: 'Voice chat' }).click()
    await page.waitForFunction(() => /Voice chat with Bramble/.test(document.querySelector('#voicebar .vt')?.textContent || ''), null, { timeout: 3000 })
    await page.locator('#view .msg .bub').getByText(VOICE, { exact: true }).first().waitFor({ timeout: 15000 })
    // Taken, answered and read again by itself: the chat watches a message until its turn ends.
    await page.waitForFunction(r => window.__said.includes(r), VOICE_REPLY, { timeout: 15000 })
    if (await page.evaluate(() => window.__said.some(t => /bramble \d|nightly/.test(t)))) throw new Error('an old message was read aloud')
    await page.waitForFunction(() => /^chat:(listening|writing|waiting|speaking)$/.test(document.getElementById('voicebar')?.dataset.mode || ''), null, { timeout: 5000 })
    await page.locator('#voicebar').getByRole('button', { name: 'Stop' }).click()
    if (await barMode()) throw new Error('still on after Stop: ' + await barMode())
  })
  await step("Read aloud, from a message's actions", async () => {
    await page.locator('#view .msg .bub').getByText(VOICE_REPLY, { exact: true }).click()
    await page.getByRole('button', { name: 'Read aloud' }).click()
    await page.waitForFunction(r => window.__said.filter(t => t === r).length === 2, VOICE_REPLY, { timeout: 3000 })
    await page.waitForFunction(() => document.getElementById('voicebar').hidden, null, { timeout: 3000 })
  })
  await step('"Hey Hermes" is off until turned on, and does not listen outside a chat', async () => {
    if (await page.evaluate(() => localStorage.getItem('hermes.wake.v1'))) throw new Error('on before it was turned on')
    await page.locator('#settings').click()
    await page.getByRole('button', { name: 'Turn "Hey Hermes" on' }).click()
    await page.getByText('"Hey Hermes" is on. Open a chat to use it.').waitFor({ timeout: 3000 })
    if (await barMode()) throw new Error('listening on Settings: ' + await barMode())
  })
  await step('"Hey Hermes" in an open chat: the bar says it listens, and what follows the words is sent', async () => {
    await page.locator('#tabs a[data-tab="bots"]').click()
    await page.getByText('Bramble', { exact: true }).click()
    await page.waitForFunction(() => /^wake:/.test(document.getElementById('voicebar')?.dataset.mode || ''), null, { timeout: 5000 })
    if (await barMode() === 'wake:tap') await page.locator('#voicebar').getByRole('button', { name: 'Listen' }).click()
    if (!/Hey Hermes/.test(await page.locator('#voicebar .vt').textContent())) throw new Error(await page.locator('#voicebar .vt').textContent())
    await page.locator('#view .msg .bub').getByText('what is on the board?', { exact: true }).first().waitFor({ timeout: 15000 })
    await page.locator('#voicebar').getByRole('button', { name: 'Stop' }).click()
    if (await barMode()) throw new Error('still on after Stop: ' + await barMode())
  })
  await step('hiding the app stops it at once, and coming back listens for "Hey Hermes" again', async () => {
    const seen = state => page.evaluate(v => {
      Object.defineProperty(document, 'visibilityState', { value: v, configurable: true })
      document.dispatchEvent(new Event('visibilitychange'))
    }, state)
    await seen('hidden')
    await seen('visible')
    await page.waitForFunction(() => /^wake:/.test(document.getElementById('voicebar')?.dataset.mode || ''), null, { timeout: 5000 })
    await seen('hidden')
    if (await barMode()) throw new Error('still on while hidden: ' + await barMode())
    await page.evaluate(() => { delete document.visibilityState })
    await page.locator('#settings').click()
    await page.getByRole('button', { name: 'Turn "Hey Hermes" off' }).click()
    await page.getByText('"Hey Hermes" is off.', { exact: true }).waitFor({ timeout: 3000 })
  })
  const sw = await page.evaluate(async () => { const r = await Promise.race([navigator.serviceWorker.ready, new Promise(ok => setTimeout(() => ok(null), 5000))]); return r ? r.scope : null })
  if (!sw) failed++
  console.log(sw ? 'PASS the service worker is registered for ' + sw : 'FAIL no service worker')
  const manifest = await page.evaluate(async () => { const r = await fetch('/m/manifest.webmanifest'); return { status: r.status, body: await r.json() } })
  const mOk = manifest.status === 200 && manifest.body.name === 'Hermes' && manifest.body.short_name === 'Hermes' &&
    manifest.body.start_url === '/m/decisions'
  if (!mOk) failed++
  console.log(mOk ? 'PASS manifest served: Hermes, opening on Decisions' : 'FAIL manifest ' + JSON.stringify(manifest))
  await step("signing out deletes the phone's copy of the chats", async () => {
    await page.goto(base + '/m/settings')
    if (!(await page.evaluate(() => !!localStorage.getItem('hermes.kept.v1')))) throw new Error('no copy to delete')
    await page.getByText('Sign out').click()
    await page.getByText('Signed out.').waitFor({ timeout: 5000 })
    if (await page.evaluate(() => localStorage.getItem('hermes.kept.v1'))) throw new Error('the copy is still there')
  })
  const sawOwn = shownNames.length === 0
  if (!sawOwn) failed++
  console.log(sawOwn ? 'PASS every request names the gateways me and one, never Hermes or One' : 'FAIL shown names sent: ' + shownNames.join(' | '))
  if (errors.length) failed++
  console.log(errors.length ? 'FAIL page errors: ' + errors.join(' | ') : 'PASS no page errors')
  await browser.close()
  console.log(failed ? `${failed} FAILED` : 'ALL PASS')
  process.exit(failed ? 1 : 0)
})()
