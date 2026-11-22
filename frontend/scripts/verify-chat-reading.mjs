import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { pathToFileURL } from 'node:url'

// Production UI + isolated real dev-fixture API; deliberately holds only the first GET.
// Usage: node scripts/verify-chat-reading.mjs ORIGIN LOGIN_JSON PLAYWRIGHT_MODULE
const [originInput, loginPath, playwrightPath] = process.argv.slice(2)
assert.ok(originInput && loginPath && playwrightPath, 'Expected origin, login file and module path')
const url = new URL(originInput)
assert.ok(
  url.protocol === 'http:' &&
    ['127.0.0.1', '[::1]'].includes(url.hostname) &&
    url.href === `${url.origin}/`,
  'Use an explicit loopback HTTP origin without credentials, path or query',
)
const origin = url.origin
const login = JSON.parse(await readFile(loginPath, 'utf8'))
assert.equal(login.synthetic, true, 'Only synthetic fixture credentials are accepted')
assert.ok(login.handle && login.password && login.thread_id, 'Incomplete synthetic login')
const { chromium } = await import(pathToFileURL(playwrightPath).href)
const barrier = () => {
  let release
  return {
    promise: new Promise((resolve) => {
      release = resolve
    }),
    release: () => release(),
  }
}

;(async () => {
  const browser = await chromium.launch({ args: ['--disable-dev-shm-usage'] })
  const auth = await browser.newContext()
  let storage
  let data
  try {
    const signedIn = await auth.request.post(origin + '/api/auth/login', {
      headers: { Origin: origin },
      data: {
        handle: login.handle,
        password: login.password,
      },
    })
    assert.equal(signedIn.status(), 200, 'Synthetic login failed')
    storage = await auth.storageState()
    const directory = await auth.request.get(
      origin + '/api/threads/' + login.thread_id + '/conversations',
    )
    assert.equal(directory.status(), 200)
    const chat = (await directory.json()).find((item) => item.kind === 'chat')
    assert.ok(chat, 'Use a dual-entry dev-fixture with stored chat history')
    data = { chat: chat.id }
  } catch (error) {
    await browser.close()
    throw error
  } finally {
    await auth.close()
  }
  try {
    for (const mode of [
      'untouched',
      'wheel',
      'keyboard',
      'typing',
      'failure-retry',
      'send',
      'leave',
    ]) {
      const context = await browser.newContext({
        viewport: { width: 390, height: 844 },
        reducedMotion: 'reduce',
      })
      const held = barrier(),
        ready = barrier()
      try {
        await context.addCookies(storage.cookies)
        const page = await context.newPage()
        page.setDefaultTimeout(9000)
        const errors = []
        page.on('pageerror', (e) => errors.push(e.message))
        let activity = 0
        page.on('request', (request) => {
          if (request.url().includes('/activity')) activity++
        })
        await page.route(
          '**/api/conversations/' + data.chat + '/messages*',
          async (route) => {
            if (route.request().method() !== 'GET') return route.continue()
            const response = await route.fetch()
            assert.equal(response.status(), 200)
            ready.release()
            await held.promise
            if (mode === 'failure-retry') {
              await route.fulfill({
                status: 503,
                json: {
                  error: {
                    code: 'synthetic_first_read_failure',
                    message: '合成首读故障',
                  },
                },
              })
            } else await route.fulfill({ response }).catch(() => {})
          },
          { times: 1 },
        )
        await page.goto(origin + '/#/chat')
        await ready.promise
        await page.locator('#chat-input:not([disabled])').waitFor()
        if (['wheel', 'typing', 'failure-retry'].includes(mode))
          await page.locator('#chat-input').fill('【合成草稿】不要替我跳走')
        if (['wheel', 'failure-retry'].includes(mode)) {
          await page.mouse.wheel(0, -600)
          await page.evaluate(() => scrollTo(0, 0))
        }
        if (mode === 'keyboard') {
          await page.locator('#private-title').focus()
          await page.keyboard.press('Home')
        }
        if (mode === 'typing') await page.evaluate(() => scrollTo(0, 0))
        if (mode === 'send') {
          await page.locator('#chat-input').fill('【合成发送】首读在途也能保存和看见自己')
          const [saved] = await Promise.all([
            page.waitForResponse(
              (r) => r.request().method() === 'POST' && r.url().includes('/messages'),
            ),
            page.getByRole('button', { name: '递出去', exact: true }).click(),
          ])
          assert.equal(saved.status(), 201)
        }
        if (mode === 'leave') {
          await page.getByRole('link', { name: '书信', exact: true }).click()
          await page.locator('.letter-envelope').first().waitFor()
          await page.evaluate(() => scrollTo(0, 300))
        }
        await page.waitForTimeout(80)
        const before = await page.evaluate(() => scrollY)
        held.release()
        if (mode === 'failure-retry') {
          await page.locator('.chat-workspace [role="alert"]').waitFor()
          await page.getByRole('button', { name: '重新读取', exact: true }).click()
        }
        if (mode === 'leave') {
          await page.waitForTimeout(150)
          assert.equal(await page.evaluate(() => scrollY), before)
          assert.equal(activity, 0, 'A disposed tick must not start activity reads')
        } else {
          await page.locator('.chat-slips .chat-slip').first().waitFor()
          await page.waitForTimeout(100)
          const after = await page.evaluate(() => ({
            y: scrollY,
            gap: document.documentElement.scrollHeight - innerHeight - scrollY,
          }))
          if (['wheel', 'keyboard', 'typing', 'failure-retry'].includes(mode)) {
            assert.ok(
              Math.abs(after.y - before) <= 1,
              mode + ' moved from ' + before + ' to ' + after.y,
            )
            if (mode !== 'keyboard')
              assert.equal(
                await page.locator('#chat-input').inputValue(),
                '【合成草稿】不要替我跳走',
              )
          } else if (mode === 'send')
            assert.ok(after.gap <= 2, mode + ' failed to land at latest: ' + JSON.stringify(after))
          else {
            const visible = await page.locator('.chat-slips > li:last-child').evaluate((last) => {
              const composer = document.querySelector('.chat-composer').getBoundingClientRect()
              return last.getBoundingClientRect().bottom <= Math.min(composer.top, innerHeight) + 1
            })
            assert.equal(
              visible,
              true,
              'Latest slip should be visible; short chats may keep the header',
            )
          }
          if (mode === 'send')
            assert.equal(
              await page
                .locator('.chat-slip')
                .last()
                .textContent()
                .then((x) => x.includes('【合成发送】首读在途也能保存和看见自己')),
              true,
            )
        }
        assert.deepEqual(errors, [])
        console.log('PASS first-read ' + mode)
      } finally {
        held.release()
        await context.close()
      }
    }
  } finally {
    await browser.close()
  }
})().catch((e) => {
  console.error(e)
  process.exitCode = 1
})
