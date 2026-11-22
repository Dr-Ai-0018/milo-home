import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { pathToFileURL } from 'node:url'

// Isolated dev-fixture API + production UI: a saved letter must release its editor.
// Usage: node scripts/verify-letter-saving.mjs ORIGIN LOGIN_JSON PLAYWRIGHT_MODULE
const [originInput, loginPath, playwrightPath] = process.argv.slice(2)
assert.ok(originInput && loginPath && playwrightPath, 'Expected origin, login file and module path')
const origin = new URL(originInput)
assert.ok(
  origin.protocol === 'http:' &&
    ['127.0.0.1', '[::1]'].includes(origin.hostname) &&
    origin.href === `${origin.origin}/`,
  'Use an explicit loopback HTTP origin without credentials, path or query',
)
const login = JSON.parse(await readFile(loginPath, 'utf8'))
assert.equal(login.synthetic, true, 'Only dev-fixture credentials are accepted')
assert.ok(login.handle && login.password && login.thread_id, 'Incomplete synthetic login')
const { chromium } = await import(pathToFileURL(playwrightPath).href)
const browser = await chromium.launch({ args: ['--disable-dev-shm-usage'] })

function barrier() {
  let release
  const promise = new Promise((resolve) => {
    release = resolve
  })
  return { promise, release }
}

try {
  for (const mode of ['directory-held', 'activity-held', 'directory-503', 'activity-503']) {
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      reducedMotion: 'reduce',
    })
    const gate = barrier()
    try {
      const signedIn = await context.request.post(`${origin.origin}/api/auth/login`, {
        headers: { Origin: origin.origin },
        data: {
          handle: login.handle,
          password: login.password,
        },
      })
      assert.equal(signedIn.status(), 200, 'Synthetic login failed')
      const created = await context.request.post(
        `${origin.origin}/api/threads/${login.thread_id}/conversations`,
        {
          headers: { Origin: origin.origin, 'Idempotency-Key': randomUUID() },
          data: { title: '【合成续写验收】辅助读取不锁保存' },
        },
      )
      assert.equal(created.status(), 201)
      const letter = await created.json()
      const page = await context.newPage()
      page.setDefaultTimeout(7000)
      const errors = []
      page.on('pageerror', (error) => errors.push(error.message))
      await page.goto(`${origin.origin}/#/letters/${letter.id}`)
      await page.locator('#message:not([readonly])').waitFor()
      await page.waitForLoadState('networkidle')
      const reached = barrier()
      let postCount = 0
      let activityReads = 0
      page.on('request', (request) => {
        if (request.url().endsWith(`/conversations/${letter.id}/messages`)) {
          if (request.method() === 'POST') postCount += 1
        }
        if (request.url().endsWith(`/conversations/${letter.id}/activity`)) activityReads += 1
      })
      const endpoint = mode.startsWith('directory')
        ? `**/api/threads/${login.thread_id}/conversations`
        : `**/api/conversations/${letter.id}/activity`
      await page.route(endpoint, async (route) => {
        if (mode.endsWith('503')) {
          await route.fulfill({
            status: 503,
            json: { error: { code: 'synthetic_read_failure', message: '合成读取故障' } },
          })
          reached.release()
          return
        }
        const response = await route.fetch()
        assert.equal(response.status(), 200)
        reached.release()
        await gate.promise
        await route.fulfill({ response }).catch(() => {})
      })
      const firstBody = `【合成续写】第一封 ${randomUUID()}`
      await page.locator('#message').fill(firstBody)
      await page.getByRole('button', { name: '贴进本子', exact: true }).click()
      await reached.promise
      await page.locator('#message:not([readonly])').waitFor()
      assert.equal(await page.locator('#message').inputValue(), '')
      assert.equal(await page.locator('.composer-send').innerText(), '贴进本子')
      assert.ok((await page.locator('.composer-done').innerText()).includes('已保存'))
      await page.waitForFunction(() => document.querySelector('.composer-send'))
      assert.ok(activityReads > 0, 'Directory latency must not hold the activity refresh')
      const secondBody = `【合成续写】第二封 ${randomUUID()}`
      await page.locator('#message').fill(secondBody)
      await page.getByRole('button', { name: '贴进本子', exact: true }).click()
      await page.locator('#message:not([readonly])').waitFor()
      assert.equal(await page.locator('#message').inputValue(), '')
      assert.equal(postCount, 2)
      await page.locator('#message').fill('【合成新草稿】辅助回应不准清掉我')
      gate.release()
      await page.waitForLoadState('networkidle')
      assert.equal(await page.locator('#message').inputValue(), '【合成新草稿】辅助回应不准清掉我')
      const stored = await context.request.get(
        `${origin.origin}/api/conversations/${letter.id}/messages?latest=true`,
      )
      assert.equal(stored.status(), 200)
      const messages = (await stored.json()).messages
      assert.equal(messages.filter((item) => item.body === firstBody).length, 1)
      assert.equal(messages.filter((item) => item.body === secondBody).length, 1)
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
        false,
      )
      assert.deepEqual(errors, [])
      console.log(`PASS saved editor and subsequent letters independent of ${mode}`)
    } finally {
      gate.release()
      await context.close()
    }
  }
} finally {
  await browser.close()
}
