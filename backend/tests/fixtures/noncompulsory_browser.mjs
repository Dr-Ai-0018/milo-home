import assert from 'node:assert/strict'
import { readFile, rename, writeFile } from 'node:fs/promises'
import { pathToFileURL } from 'node:url'
import { randomUUID } from 'node:crypto'

const s = JSON.parse(await readFile(process.argv[2], 'utf8'))
assert.equal(s.synthetic, true)
assert.equal(new URL(s.origin).hostname, '127.0.0.1')
const { chromium } = await import(pathToFileURL(s.playwright).href)
const browser = await chromium.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] })
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
let sequence = 0
let context, page
async function request(path, data, method = 'post') {
  const response = await context.request[method](`${s.origin}/api${path}`, {
    headers: { Origin: s.origin, 'Idempotency-Key': randomUUID() }, data,
  })
  assert.ok(response.ok(), `${path}: ${response.status()} ${await response.text()}`)
  return response.json()
}
async function service(action, fields = {}) {
  const id = ++sequence
  await writeFile(`${s.command}.tmp`, JSON.stringify({ id, action, ...fields }))
  await rename(`${s.command}.tmp`, s.command)
  for (let i = 0; i < 150; i++) {
    let result
    try { result = JSON.parse(await readFile(s.response, 'utf8')) } catch (e) {
      if (e.code !== 'ENOENT') throw e
    }
    if (result?.id === id) {
      assert.equal(result.ok, true)
      await page.clock.setFixedTime(result.now * 1000)
      return
    }
    await pause(40)
  }
  throw Error(`Consumer timed out: ${action}`)
}
async function visit(path) {
  await page.goto(`${s.origin}/#${path}`)
  await page.getByRole('button', { name: '退出登录', exact: true }).waitFor()
}
async function checkLayout(name) {
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: width === 1440 ? 900 : 844 })
    await page.emulateMedia({ reducedMotion: width === 320 ? 'reduce' : 'no-preference' })
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
    if (width !== 320) await page.screenshot({ path: `${s.screens}/${name}-${width}.png` })
  }
}
try {
  context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  await request('/auth/login', { handle: s.handle, password: s.password })
  page = await context.newPage()
  page.setDefaultTimeout(4500)
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('console', (e) => { if (e.type() === 'error') errors.push(e.text()) })
  await page.clock.install({ time: s.now * 1000 })
  if (s.area === 'reply') {
    const chat = await request(`/threads/${s.thread}/chat`, undefined, 'put')
    for (let i = 1; i <= 4; i++) await request(`/conversations/${chat.id}/messages`, { body: `合成纸条${i}` })
    await visit('/chat')
    await page.locator('#chat-input').fill('联调草稿保留')
    await service('reply', { scenario: 'auto-reply-mixed' })
    await page.getByRole('button', { name: '重新读取', exact: true }).click()
    await page.getByText('合成原生合回。', { exact: true }).waitFor()
    assert.equal(await page.getByText('合成原生合回。', { exact: true }).count(), 1)
    assert.equal(await page.locator('#chat-input').inputValue(), '联调草稿保留')
    const mixed = await request(`/conversations/${chat.id}/messages`, undefined, 'get')
    assert.equal(mixed.messages.filter((m) => m.reply_decision?.state === 'no_reply').length, 1)
    assert.equal((await page.locator('main').innerText()).includes('正存着，等米洛来看'), false)
    await checkLayout('mixed-chat')
    await service('return')
    await page.getByRole('button', { name: '重新读取', exact: true }).click()
    await page.getByText('合成到期补话。', { exact: true }).waitFor()
    const messages = await request(`/conversations/${chat.id}/messages`, undefined, 'get')
    assert.equal(messages.messages.filter((m) => m.role === 'visitor').length, 4)
    const letter = await request(`/threads/${s.thread}/conversations`, { title: 'F6 合成长信' })
    await request(`/conversations/${letter.id}/messages`, { body: '合成接收安排' })
    await service('reply', { scenario: 'auto-reply-reception' })
    await visit(`/letters/${letter.id}`)
    await page.getByText('合成原生回信。', { exact: true }).waitFor()
    await page.locator('.conv-status').filter({ hasText: '休息' }).waitFor()
    await checkLayout('reception-letter')
    await visit('/letters')
    await page.getByText('F6 合成长信', { exact: true }).waitFor()
  } else {
    async function task(label, scenario) {
      const task = await request(`/work-sessions/${s.session}/tasks`, { body: label })
      await service('work', { task: task.id, scenario })
      await visit(`/work/${task.id}`)
      return task
    }
    const deferred = await task('合成暂缓后回访', 'auto-work-deferred')
    await page.getByText('米洛暂缓处理这件', { exact: true }).waitFor()
    await page.getByRole('button', { name: '停下这件', exact: true }).waitFor()
    await service('work-return')
    await page.locator('.detail-head').getByRole('button', { name: '重新读取', exact: true }).click()
    await page.getByText('这次处理结束了', { exact: true }).waitFor()
    await page.locator('.detail-records > summary').click()
    await page.locator('.turn-head').filter({ hasText: '安排好的回访：这一轮结束了' }).waitFor()
    assert.equal(await page.getByText(/补过 \d+ 段话/).count(), 0)
    const inputs = await request(`/work-tasks/${deferred.id}/inputs`, undefined, 'get')
    assert.equal(inputs.inputs.at(-1).source, 'revisit')
    const declined = await task('合成婉拒', 'auto-work-declined')
    await page.getByText('米洛婉拒了这件交办', { exact: true }).waitFor()
    assert.equal(await page.getByRole('button', { name: '停下这件', exact: true }).count(), 0)
    const clarification = await task('合成澄清原单', 'auto-work-clarify')
    await page.getByText('米洛想先问清楚，再继续做', { exact: true }).waitFor()
    await checkLayout('clarification-work')
    const posts = []
    page.on('request', (r) => { if (r.method() === 'POST') posts.push(new URL(r.url()).pathname) })
    await page.locator('textarea').fill('合成澄清：只读，不改文件')
    const appended = page.waitForResponse((r) => r.request().method() === 'POST' && r.url().endsWith(`/work-tasks/${clarification.id}/inputs`))
    await page.getByRole('button', { name: '补上说明', exact: true }).click()
    assert.equal((await appended).status(), 202)
    assert.ok(posts.includes(`/api/work-tasks/${clarification.id}/inputs`))
    assert.equal(posts.filter((p) => p.endsWith('/tasks')).length, 0)
    await service('work', { task: clarification.id, scenario: 'auto-work-accepted' })
    const stopped = await task('合成暂缓叫停', 'auto-work-deferred')
    await page.getByRole('button', { name: '停下这件', exact: true }).click()
    await page.getByRole('button', { name: '确定停下', exact: true }).click()
    await page.getByText('停下了', { exact: true }).first().waitFor()
    assert.equal((await request(`/work-tasks/${stopped.id}`, undefined, 'get')).status, 'cancelled')
    await visit('/work')
    await page.getByRole('button', { name: '已结束', exact: true }).click()
    await page.getByText('合成婉拒', { exact: true }).waitFor()
    assert.ok(declined.id)
  }
  assert.deepEqual(errors, [])
  console.log(JSON.stringify({ passed: true, real_api: true, actual_consumers: true, area: s.area }))
} finally {
  await browser.close()
}
