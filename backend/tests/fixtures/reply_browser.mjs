import assert from 'node:assert/strict'
import { readFile, rename, writeFile } from 'node:fs/promises'
import { pathToFileURL } from 'node:url'
import { randomUUID } from 'node:crypto'

const settings = JSON.parse(await readFile(process.argv[2], 'utf8'))
assert.equal(settings.synthetic, true)
assert.equal(new URL(settings.origin).hostname, '127.0.0.1')
const { chromium } = await import(pathToFileURL(settings.playwright).href)
const browser = await chromium.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] })
let sequence = 0
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function service(action) {
  const id = ++sequence
  await writeFile(`${settings.command}.tmp`, JSON.stringify({ id, action }))
  await rename(`${settings.command}.tmp`, settings.command)
  const end = Date.now() + 10000
  while (Date.now() < end) {
    try {
      const response = JSON.parse(await readFile(settings.response, 'utf8'))
      if (response.id === id) {
        assert.equal(response.ok, true)
        return
      }
    } catch (error) {
      if (!['ENOENT'].includes(error.code) && !(error instanceof SyntaxError)) throw error
    }
    await pause(40)
  }
  throw new Error(`Synthetic service action ${action} timed out`)
}

try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const login = await context.request.post(`${settings.origin}/api/auth/login`, {
    headers: { Origin: settings.origin },
    data: { handle: settings.handle, password: settings.password },
  })
  assert.equal(login.status(), 200)
  const created = await context.request.post(
    `${settings.origin}/api/threads/${settings.thread}/conversations`,
    {
      headers: { Origin: settings.origin, 'Idempotency-Key': randomUUID() },
      data: { title: '【合成A4】独立回信组合验收' },
    },
  )
  assert.equal(created.status(), 201)
  const letter = await created.json()
  await service('check')
  const page = await context.newPage()
  page.setDefaultTimeout(6000)
  const errors = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.clock.install()
  await page.goto(`${settings.origin}/#/letters/${letter.id}`)
  await page.locator('#message:not([readonly])').fill('【合成】网页交给独立服务的一封信')
  await page.getByRole('button', { name: '贴进本子', exact: true }).click()
  await page.locator('.composer-done.is-visible').waitFor()
  await page.locator('#message').fill('后续草稿要留在这里')
  await page.locator('.conv-status').filter({ hasText: '等待下一次自动检查' }).waitFor()
  await page.screenshot({ path: `${settings.screens}/reply-letter-pending-desktop.png` })
  await service('disable')
  await page.getByRole('button', { name: '重新读取', exact: true }).click()
  await page.locator('.conv-status').filter({ hasText: '没有开启' }).waitFor()
  assert.equal(await page.locator('#message').inputValue(), '后续草稿要留在这里')
  await service('fault')
  await page.getByRole('button', { name: '重新读取', exact: true }).click()
  await page.locator('.conv-status').filter({ hasText: '故障' }).waitFor()
  await service('expire')
  await page.getByRole('button', { name: '再核对状态', exact: true }).click()
  await page.locator('.conv-status').filter({ hasText: '自动处理情况目前看不清' }).waitFor()
  await service('resume')
  await page.getByRole('button', { name: '再核对状态', exact: true }).click()
  await service('run')
  await page.clock.fastForward(20001)
  await page.getByText('合成服务回信', { exact: true }).first().waitFor()
  assert.equal(await page.locator('#message').inputValue(), '后续草稿要留在这里')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto(`${settings.origin}/#/chat`)
  await page.locator('#chat-input').fill('【合成】换到纸条也回原处')
  await page.locator('#chat-input').press('Enter')
  await page.getByText('【合成】换到纸条也回原处', { exact: true }).first().waitFor()
  await service('run')
  await page.clock.fastForward(20001)
  await page.getByText('合成服务回信', { exact: true }).first().waitFor()
  await service('fault')
  await page.getByRole('button', { name: '重新读取', exact: true }).click()
  await page.locator('.conv-status').filter({ hasText: '故障' }).waitFor()
  await page.locator('#chat-input').fill('手机草稿也保留')
  await page.screenshot({ path: `${settings.screens}/reply-chat-fault-mobile.png` })
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 })
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
    )
  }
  assert.equal(await page.locator('#chat-input').inputValue(), '手机草稿也保留')
  await page.getByRole('button', { name: '退出登录', exact: true }).click()
  await page.getByRole('button', { name: '进入我的对话', exact: true }).waitFor()
  assert.equal(await page.locator('#chat-input').count(), 0)
  assert.deepEqual(errors, [])
  console.log(
    JSON.stringify({ synthetic: true, real_api: true, separate_service: true, passed: true }),
  )
} finally {
  await browser.close()
}
