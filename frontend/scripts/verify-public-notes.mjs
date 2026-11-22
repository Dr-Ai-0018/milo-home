import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { writeFile, unlink } from 'node:fs/promises'
import { resolve } from 'node:path'
import { spawnSync } from 'node:child_process'
import { pathToFileURL } from 'node:url'

const [origin, output, mode, playwright] = process.argv.slice(2)
assert.equal(new URL(origin).hostname, '127.0.0.1')
const root = resolve(output)
const { chromium } = await import(pathToFileURL(playwright).href)
const browser = await chromium.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] })
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage()
page.setDefaultTimeout(5000)
const errors = []
page.on('pageerror', (error) => errors.push(error.message))
page.on('console', (message) => {
  if (
    message.type() === 'error' &&
    !/Failed to load resource:.*\b(401|404|503)\b/.test(message.text())
  ) {
    errors.push(message.text())
  }
})
const writes = []
page.on('request', (request) => {
  if (request.method() !== 'GET') writes.push(request.method() + ' ' + request.url())
})

function cli(action, ...args) {
  const result = spawnSync('backend/.venv/bin/milo-home', ['content', 'note', action, ...args], {
    env: { ...process.env, MILO_DATABASE: `${root}/home.sqlite3` },
    encoding: 'utf8',
    timeout: 5000,
  })
  assert.equal(result.status, 0, `Isolated content command ${action} failed: ${result.stderr}`)
  return JSON.parse(result.stdout)
}

async function content(body, title = '合成近况验收') {
  const path = `${root}/input.json`
  await writeFile(
    path,
    JSON.stringify({
      title,
      summary: '明确公开的合成摘要。',
      body,
      category: null,
      content_date: '2026-11-22',
    }),
  )
  return path
}

async function ready(selector) {
  await page.waitForFunction(
    (selector) => document.querySelector(selector)?.getAttribute('aria-busy') === 'false',
    selector,
  )
}

async function focused(locator, message) {
  assert.equal(await locator.evaluate((el) => el === document.activeElement), true, message)
}

async function responsive(selector) {
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 900 })
    if (width === 320) await page.emulateMedia({ reducedMotion: 'reduce' })
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
    )
    if (width !== 320)
      await page.screenshot({ path: `${root}/${selector}-${width}.png`, fullPage: true })
  }
}

async function lifecycle() {
  await writeFile(`${root}/storage-busy`, 'synthetic')
  await page.goto(`${origin}/`)
  await page.locator('.home-notes-retry').waitFor()
  await unlink(`${root}/storage-busy`)
  await page.locator('.home-notes-retry').click()
  await page.getByText('还没有公开近况。', { exact: true }).waitFor()
  assert.notEqual(
    await page.evaluate(() => document.activeElement.tagName),
    'BODY',
    'A recovered initial read retains focus in the home notes section',
  )
  assert.equal(await page.locator('.home-lead-note').count(), 0)
  assert.equal((await context.request.get(`${origin}/api/auth/me`)).status(), 401)
  const id = randomUUID()
  const original =
    '合成首版正文。\n<script>window.unwanted = true</script>\n' + '暖纸长段落。'.repeat(180)
  let saved = cli('create', '--id', id, '--file', await content(original))
  assert.equal((await context.request.get(`${origin}/api/notes/${id}`)).status(), 404)
  assert.deepEqual((await (await context.request.get(`${origin}/api/notes`)).json()).notes, [])
  saved = cli(
    'publish',
    '--id',
    id,
    '--expected-version',
    String(saved.version),
    '--confirm-public',
  )
  await page.locator('.home-notes-reload').click()
  await page.getByRole('link', { name: '全部近况' }).click()
  await page.locator('.notes-item-link').click()
  assert.equal(await page.locator('.note-detail-body').innerText(), original)
  assert.equal(await page.evaluate(() => window.unwanted), undefined)
  assert.match(await page.locator('.notes-item-meta').innerText(), /2026年11月22日/)
  saved = cli(
    'revise',
    '--id',
    id,
    '--expected-version',
    String(saved.version),
    '--file',
    await content('合成公开第二版。'),
  )
  await page.getByRole('button', { name: '重读', exact: true }).click()
  await ready('.note-detail-page')
  assert.equal(await page.locator('.note-detail-body').innerText(), original)
  saved = cli(
    'publish',
    '--id',
    id,
    '--expected-version',
    String(saved.version),
    '--confirm-public',
  )
  await page.getByRole('button', { name: '重读', exact: true }).click()
  await page.getByText('合成公开第二版。', { exact: true }).waitFor()
  await writeFile(`${root}/storage-busy`, 'synthetic')
  const retry = page.getByRole('button', { name: '重读', exact: true })
  await retry.click()
  await page.locator('.notes-inline-notice.is-warn').waitFor()
  assert.equal((await context.request.get(`${origin}/api/notes`)).status(), 503)
  assert.equal(await page.locator('.note-detail-body').innerText(), '合成公开第二版。')
  await focused(retry, 'A failed refresh retains keyboard focus')
  await unlink(`${root}/storage-busy`)
  await retry.click()
  await ready('.note-detail-page')
  await focused(retry)
  await responsive('detail')
  cli('withdraw', '--id', id, '--expected-version', String(saved.version))
  await page.getByRole('button', { name: '重读', exact: true }).click()
  await page.getByRole('heading', { name: '这条近况当前不可读取' }).waitFor()
  assert.equal(await page.locator('.note-detail-body').count(), 0)
  assert.notEqual(
    await page.evaluate(() => document.activeElement.tagName),
    'BODY',
    'Withdrawing content should transfer focus from the removed reload button',
  )
  await page.locator('.notes-back-link').click()
  await page.getByText('还没有公开近况。', { exact: true }).waitFor()
  assert.equal(await page.locator('.notes-item-link').count(), 0)
}

async function history() {
  await page.goto(`${origin}/#/notes`)
  await page.waitForFunction(() => document.querySelectorAll('.notes-item-link').length === 10)
  const firstPage = await page
    .locator('.notes-item-link')
    .evaluateAll((els) => els.map((el) => el.hash))
  const oldPage = await (await context.request.get(`${origin}/api/notes?before=5`)).json()
  const removed = oldPage.notes[0].id
  cli('withdraw', '--id', removed, '--expected-version', '2')
  const added = randomUUID()
  cli('create', '--id', added, '--file', await content('翻页期间新发的合成近况。', '合成最新插入'))
  cli('publish', '--id', added, '--expected-version', '1', '--confirm-public')
  await page.getByRole('button', { name: '翻看更早的近况' }).click()
  await page.waitForFunction(() => document.querySelectorAll('.notes-item-link').length === 13)
  await focused(
    page.locator('.notes-page-title'),
    'The final-page button hands focus to the list title',
  )
  const all = await page.locator('.notes-item-link').evaluateAll((els) => els.map((el) => el.hash))
  assert.deepEqual(all.slice(0, 10), firstPage)
  assert.equal(new Set(all).size, 13)
  assert.equal(
    all.some((hash) => hash.includes(removed) || hash.includes(added)),
    false,
  )
  const link = page.locator('.notes-item-link').nth(10)
  await link.scrollIntoViewIfNeeded()
  const y = await page.evaluate(() => scrollY)
  await link.click()
  await page.locator('.note-detail-body').waitFor()
  await page.locator('.notes-back-link').click()
  await page.waitForFunction((y) => Math.abs(scrollY - y) < 10, y)
  assert.equal(await page.locator('.notes-item-link').count(), 13)
  await page.getByRole('button', { name: '刷新', exact: true }).click()
  await page.getByRole('link', { name: '合成最新插入', exact: true }).waitFor()
  assert.equal(await page.locator('.notes-item-link').count(), 10)
  await responsive('history')
  await page.goto(`${origin}/`)
  await page.locator('.home-note-link').first().waitFor()
  assert.equal(await page.locator('.home-note-link').count(), 3)
  await responsive('home')
}

async function legacy() {
  await page.goto(`${origin}/`)
  await page.getByText('随站保留的旧近况，暂不能读取更新', { exact: true }).waitFor()
  assert.equal(await page.locator('.home-note-link').count(), 0)
  await page.goto(`${origin}/#/notes`)
  await page.locator('.notes-legacy-block').waitFor()
  assert.equal(await page.locator('.notes-list-item.is-legacy').count(), 3)
  assert.equal(await page.locator('.notes-item-link').count(), 0)
  await page.goto(`${origin}/#/notes/${randomUUID()}`)
  await page.getByRole('heading', { name: '这条近况当前不可读取' }).waitFor()
  assert.equal(await page.locator('.note-detail-body').count(), 0)
}

try {
  await { lifecycle, history, legacy }[mode]()
  assert.deepEqual(writes, [])
  assert.deepEqual(errors, [])
  assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0])
  const result = { mode, passed: true, real_http: true, model_calls: 0, errors }
  await writeFile(`${root}/browser.json`, JSON.stringify(result))
  console.log(JSON.stringify(result))
} catch (error) {
  await page.screenshot({ path: `${root}/failure.png`, fullPage: true }).catch(() => {})
  await writeFile(`${root}/failure.json`, JSON.stringify({ mode, error: error.message, errors }))
  throw error
} finally {
  await browser.close()
}
