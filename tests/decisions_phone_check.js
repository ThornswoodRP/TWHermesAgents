const { chromium } = require('playwright')
// The phone app in headless Chromium with a virtual passkey (Chrome's WebAuthn
// testing API): setting up the phone (pairing and its first passkey on one
// screen), a deep link, adding a passkey, signing in with it,
// answering a card (it leaves Decisions for Answered), a high-risk card's own
// check screen, the four tabs, the Thornswood Bots list (Hermes, One, then the
// six Bots) and a chat with Patrick's own Hermes on One (as text, a new chat,
// Hermes on the VM with no connector yet, and reactions: Hermes's own, setting
// one from a message's actions and taking it off), the Rooms
// list and a room (as text), the Board,
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
  // A 401 is the signed-out app asking; a 404 is said below only when it is not a chat a gateway never listed.
  page.on('console', m => { if (m.type() === 'error' && !/status of (401|404)/.test(m.text())) errors.push(m.text()) })
  page.on('response', r => {
    if (r.status() === 404 && !/\/api\/v1\/own\/(me|one)\/chats\/[^/]+\/messages$/.test(new URL(r.url()).pathname)) {
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
    await page.getByText('Answered "Delete the old test branches?": Wait. It is under Answered below.').waitFor({ timeout: 8000 })
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
  await page.screenshot({ path: require('os').tmpdir() + '/tw-phone-bots.png', fullPage: true })
  await page.getByText('Bramble', { exact: true }).click()
  await step("Bramble's Bot Chat, as text, under Bots", async () => {
    await page.getByText('All green <i>tonight</i>.').waitFor({ timeout: 5000 })
    if (!page.url().endsWith('/m/bots/bramble')) throw new Error('url ' + page.url())
    if ((await page.locator('#tabs a[aria-current="page"]').textContent()) !== 'Bots') throw new Error('not on the Bots tab')
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
  await page.getByRole('link', { name: 'Bots' }).click()
  await page.getByText('Its chats show here once it is turned on.').waitFor({ timeout: 5000 })
  await page.getByText('One', { exact: true }).click()
  await step("One's chats, with full power for 30 minutes after the passkey", async () => {
    await page.getByText('Plan the week', { exact: true }).waitFor({ timeout: 5000 })
    await page.getByText('Untitled chat', { exact: true }).waitFor({ timeout: 3000 })
    await page.getByText(/^Full power until .+, 30 minutes after your passkey sign-in\.$/).waitFor({ timeout: 3000 })
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
    await page.getByText(/^You can send until .+ started from the phone or has \/yolo on\.$/).waitFor({ timeout: 3000 })
  })
  await page.getByLabel('Message to One').fill('Add the backup check.')
  await page.getByRole('button', { name: 'Send to One' }).click()
  await step('a message to One waits for its connector', async () => {
    await page.getByText('Add the backup check.').waitFor({ timeout: 5000 })
    await page.getByText('Waiting to send').waitFor({ timeout: 3000 })
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
  await page.goto(base + '/m/hermes/one/s_old')
  await step('a chat Hermes no longer has says so and offers no send', async () => {
    await page.getByText('This chat is gone from One. Nothing sent here would reach it.').waitFor({ timeout: 15000 })
    if (await page.getByLabel('Send to One').count()) throw new Error('a composer for a gone chat')
  })
  await page.goto(base + '/m/hermes/one/s_never')
  await step('a chat One never listed does not open', () =>
    page.getByText('One has not listed this chat, so it does not open here.').waitFor({ timeout: 5000 }))
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
  await step('a push shows only "Hermes needs you", quiet, tagged by its card', async () => {
    if (!reg) throw new Error('no service worker registration seen')
    await cdp.send('ServiceWorker.deliverPushMessage', { origin: base, registrationId: reg.registrationId,
      data: JSON.stringify({ v: 1, event_id: 'px_1', decision_id: card, attention_revision: 1, path: '/m/decisions/' + card }) })
    let list = []
    for (let i = 0; i < 50 && !list.length; i++) { list = await shown(); if (!list.length) await new Promise(ok => setTimeout(ok, 100)) }
    const want = { title: 'Hermes needs you', body: '', tag: 'decision:' + card, silent: true, path: '/m/decisions/' + card, actions: 0 }
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
    await page.getByText('Answered', { exact: true }).waitFor({ timeout: 5000 })
    if (!page.url().endsWith('/m/decisions')) throw new Error('url ' + page.url())
  })
  await page.getByRole('button', { name: 'Not now' }).click()
  await step('the install hint goes away for this visit when told not now', async () => {
    if (await page.locator('#install').isVisible()) throw new Error('still shown')
  })
  // Nothing may widen the page (Patrick's Pixel 9a, 1 Nov): a link, a commit id or a branch name with no
  // space in it pushed the page past the phone's width, and the last tab went off-screen at normal zoom.
  // Each signed-in view gets one in every kind of text it shows, at Pixel widths and with larger text.
  const LONG = 'https://github.com/ThornswoodRP/Thornswood-Hermes/pull/44/commits/2e308db923bf40880adae89d38fedb307c18aa46'
  for (const [width, zoom] of [[412, 1], [360, 1], [412, 1.3]]) {
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
  await page.setViewportSize({ width: 412, height: 860 })
  const sw = await page.evaluate(async () => { const r = await Promise.race([navigator.serviceWorker.ready, new Promise(ok => setTimeout(() => ok(null), 5000))]); return r ? r.scope : null })
  if (!sw) failed++
  console.log(sw ? 'PASS the service worker is registered for ' + sw : 'FAIL no service worker')
  const manifest = await page.evaluate(async () => { const r = await fetch('/m/manifest.webmanifest'); return { status: r.status, body: await r.json() } })
  const mOk = manifest.status === 200 && manifest.body.name === 'Hermes' && manifest.body.short_name === 'Hermes' &&
    manifest.body.start_url === '/m/decisions'
  if (!mOk) failed++
  console.log(mOk ? 'PASS manifest served: Hermes, opening on Decisions' : 'FAIL manifest ' + JSON.stringify(manifest))
  const sawOwn = shownNames.length === 0
  if (!sawOwn) failed++
  console.log(sawOwn ? 'PASS every request names the gateways me and one, never Hermes or One' : 'FAIL shown names sent: ' + shownNames.join(' | '))
  if (errors.length) failed++
  console.log(errors.length ? 'FAIL page errors: ' + errors.join(' | ') : 'PASS no page errors')
  await browser.close()
  console.log(failed ? `${failed} FAILED` : 'ALL PASS')
  process.exit(failed ? 1 : 0)
})()
