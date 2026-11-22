import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { pathToFileURL } from 'node:url'

// Run against an isolated dev-fixture API and the production UI build that proxies it.
// Usage: node scripts/verify-letter-directory.mjs ORIGIN LOGIN_JSON PLAYWRIGHT_MODULE
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
assert.ok(login.handle && login.password && login.thread_id, 'Incomplete synthetic login file')
const { chromium } = await import(pathToFileURL(playwrightPath).href)
const browser = await chromium.launch({ args: ['--disable-dev-shm-usage'] })

function barrier() {
  let release
  const promise = new Promise((resolve) => {
    release = resolve
  })
  return { promise, release }
}

async function openContext() {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    reducedMotion: 'reduce',
  })
  try {
    const response = await context.request.post(`${origin.origin}/api/auth/login`, {
      headers: { Origin: origin.origin },
      data: {
        handle: login.handle,
        password: login.password,
      },
    })
    assert.equal(response.status(), 200, 'Synthetic login failed')
    const directory = await context.request.get(
      `${origin.origin}/api/threads/${login.thread_id}/conversations`,
    )
    assert.equal(directory.status(), 200, 'Synthetic directory must be readable')
    const conversations = await directory.json()
    const count = conversations.filter((item) => item.kind === 'letter').length
    assert.ok(count > 0, 'Fixture needs at least one real stored letter')
    const page = await context.newPage()
    page.setDefaultTimeout(8000)
    return { context, page, count }
  } catch (error) {
    await context.close()
    throw error
  }
}

try {
  for (const mode of [
    'runs-held',
    'runs-503',
    'workspace-held',
    'workspace-503',
    'directory-503',
  ]) {
    const { context, page, count } = await openContext()
    const gate = barrier()
    let auxiliaryRequests = 0
    const errors = []
    page.on('pageerror', (error) => errors.push(error.message))
    const endpoint = mode.startsWith('runs')
      ? '**/api/threads/*/runs*'
      : mode.startsWith('workspace')
        ? '**/api/workspaces'
        : '**/api/threads/*/conversations'
    await page.route(endpoint, async (route) => {
      auxiliaryRequests += 1
      if (mode.endsWith('503')) {
        await route.fulfill({
          status: 503,
          json: {
            error: {
              code: 'synthetic_read_failure',
              message: '合成读取故障',
            },
          },
        })
        return
      }
      const response = await route.fetch()
      assert.equal(response.status(), 200)
      await gate.promise
      await route.fulfill({ response }).catch(() => {})
    })
    try {
      await page.goto(`${origin.origin}/#/letters`)
      const opener = page.getByRole('button', { name: '＋ 开一封新信', exact: true })
      if (mode === 'directory-503') {
        await page
          .getByText('这会儿没读出你的信，读取失败不会弄丢它们。', { exact: true })
          .waitFor()
        assert.equal(await page.locator('.letter-envelope').count(), 0)
        assert.equal(await opener.count(), 0)
        assert.equal(await page.getByText('暂时没有等回信的信。', { exact: true }).count(), 0)
      } else {
        await page.locator('.letter-envelope').first().waitFor()
        assert.equal(await page.locator('.letter-envelope').count(), count)
        await opener.click()
        await page.locator('#letter-title').fill('【合成样例】辅助记录不挡写信')
        if (mode.startsWith('runs')) {
          assert.equal(
            (await page.locator('.run-status').textContent()).includes('还没读过'),
            false,
          )
          if (mode.endsWith('503')) await page.locator('.run-status [role="alert"]').waitFor()
        }
        if (mode === 'runs-held') {
          await page.getByRole('button', { name: '重新读取', exact: true }).click()
          await page.waitForFunction(
            () =>
              document.querySelector('.account-bar button')?.getAttribute('aria-disabled') ===
              'false',
          )
          assert.equal(auxiliaryRequests, 1, 'Directory reload must reuse held run-history read')
          assert.equal(
            await page.locator('#letter-title').inputValue(),
            '【合成样例】辅助记录不挡写信',
          )
        }
        assert.equal(
          await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
          false,
        )
      }
      assert.deepEqual(errors, [])
      console.log(`PASS ${mode}`)
    } finally {
      gate.release()
      await context.close()
    }
  }
} finally {
  await browser.close()
}
