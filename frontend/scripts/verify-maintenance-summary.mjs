import assert from 'node:assert/strict'
import { readFile, rename, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { spawnSync } from 'node:child_process'
import { pathToFileURL } from 'node:url'

const [origin, output, mode, playwright] = process.argv.slice(2)
assert.equal(new URL(origin).hostname, '127.0.0.1')
const root = resolve(output)
const login = JSON.parse(await readFile(`${root}/login.private.json`, 'utf8'))
assert.equal(login.synthetic, true)
const source = JSON.parse(await readFile(`${root}/published/summary.json`, 'utf8'))
const { chromium } = await import(pathToFileURL(playwright).href)
const browser = await chromium.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] })
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage()
page.setDefaultTimeout(6000)
const errors = []
page.on('pageerror', (error) => errors.push(error.message))
page.on('console', (message) => {
  if (message.type() !== 'error') return
  if (/Failed to load resource:.*\b(401|403|503)\b/.test(message.text())) return
  errors.push(message.text())
})
let reads = 0
page.on('request', (request) => {
  if (new URL(request.url()).pathname === '/api/maintenance') reads += 1
})

async function cookie(index) {
  await context.addCookies([
    { name: 'milo_session', value: login.users[index].cookie, url: origin },
  ])
}

async function publish(change) {
  const doc = structuredClone(source)
  const now = Math.floor(Date.now() / 1000)
  doc.summary.observed_at = now
  doc.summary.expires_at = now + 180
  for (const fact of doc.summary.backups) {
    if (fact.state === 'normal') fact.fresh_until = now + 600
  }
  change?.(doc.summary, now)
  const temporary = `${root}/published/.next.json`
  await writeFile(temporary, JSON.stringify(doc), { mode: 0o640 })
  await rename(temporary, `${root}/published/summary.json`)
}

async function open() {
  await cookie(0)
  await publish()
  await page.goto(`${origin}/#/work`)
  await page.locator('.maintenance-notice').waitFor()
  await page.locator('.maintenance-toggle').click()
  await page.locator('.maintenance-panel').waitFor()
}

async function reload() {
  const button = page.locator('.maintenance-reload')
  await button.click()
  await page.waitForFunction(
    () => document.querySelector('.maintenance-reload')?.getAttribute('aria-busy') === 'false',
  )
}

function permission(action) {
  const code =
    'import sys; from pathlib import Path; from milo_home.database import Database; ' +
    'from milo_home.work_access import WorkAccess; ' +
    `WorkAccess(Database(Path(sys.argv[1]))).${action}(sys.argv[2])`
  const result = spawnSync('backend/.venv/bin/python', [
    '-c',
    code,
    `${root}/home.sqlite3`,
    login.users[0].id,
  ])
  assert.equal(result.status, 0, 'Isolated permission mutation failed')
}

try {
  await open()
  const notice = page.locator('.maintenance-notice')
  if (mode === 'core') {
    const api = await context.request.get(`${origin}/api/maintenance`)
    assert.equal(api.status(), 200)
    assert.equal((await api.json()).backups.find((f) => f.kind === 'paired').state, 'normal')
    assert.match(await page.locator('.maintenance-headline').innerText(), /部分.*待核对/)
    assert.equal(await notice.locator('.maintenance-item.is-normal').count(), 4)
    for (const width of [1440, 390, 320]) {
      await page.setViewportSize({ width, height: 900 })
      if (width === 320) await page.emulateMedia({ reducedMotion: 'reduce' })
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
        true,
      )
      if (width !== 320)
        await page.screenshot({ path: `${root}/expanded-${width}.png`, fullPage: true })
    }
    await page.locator('.maintenance-reload').focus()
    await page.keyboard.press('Escape')
    assert.equal(await page.locator('.maintenance-panel').count(), 0)
    assert.equal(
      await page.locator('.maintenance-toggle').evaluate((el) => el === document.activeElement),
      true,
    )
    await page.screenshot({ path: `${root}/collapsed-mobile.png`, fullPage: true })
    await page.locator('.maintenance-toggle').click()
    await writeFile(`${root}/published/summary.json`, '{broken-cache')
    await reload()
    assert.match(await notice.innerText(), /保留上次读到/)
    assert.match(await notice.innerText(), /已用 4\.0 KiB/)
    assert.equal(
      await page.locator('.maintenance-reload').evaluate((el) => el === document.activeElement),
      true,
    )
    await publish((summary, now) => {
      summary.backups.find((f) => f.kind === 'paired').fresh_until = now + 2
    })
    await reload()
    const priorReads = reads
    await page.waitForFunction(() =>
      [...document.querySelectorAll('.maintenance-item')].some(
        (el) => el.textContent.includes('完整配套采集') && el.classList.contains('is-attention'),
      ),
    )
    assert.equal(reads, priorReads)
    assert.equal(await page.locator('.maintenance-expired-tag').count(), 0)
    await publish((summary, now) => {
      summary.expires_at = now + 3
    })
    await reload()
    await page.evaluate(() => {
      const frozen = Date.now()
      Date.now = () => frozen
    })
    const topReads = reads
    await page.locator('.maintenance-expired-tag').waitFor()
    assert.equal(reads, topReads)
    assert.match(await notice.innerText(), /已用 4\.0 KiB/)
  } else {
    permission('revoke')
    assert.equal((await context.request.get(`${origin}/api/maintenance`)).status(), 403)
    await page.locator('.maintenance-reload').click()
    await notice.waitFor({ state: 'hidden' })
    assert.equal(
      await page.locator('#private-title').evaluate((el) => el === document.activeElement),
      true,
      'Permission removal should return focused summary controls to the page title',
    )
    permission('grant')
    await page.reload()
    await notice.waitFor()
    await page.locator('.maintenance-toggle').click()
    await cookie(1)
    assert.equal((await context.request.get(`${origin}/api/maintenance`)).status(), 403)
    await page.locator('.maintenance-reload').click()
    await notice.waitFor({ state: 'hidden' })
    await page.waitForFunction(() => !document.querySelector('.new-task'))
    await cookie(0)
    await page.reload()
    await notice.waitFor()
    await page.locator('.maintenance-toggle').click()
    let release
    const held = new Promise((resolve) => {
      release = resolve
    })
    let reached
    const arrived = new Promise((resolve) => {
      reached = resolve
    })
    await page.route('**/api/maintenance', async (route) => {
      const response = await route.fetch()
      assert.equal(response.status(), 200)
      reached()
      await held
      await route.fulfill({ response }).catch(() => {})
    })
    await page.locator('.maintenance-reload').click()
    await arrived
    await page.getByRole('button', { name: '退出登录', exact: true }).click()
    await page.locator('#handle').waitFor()
    release()
    await page.waitForLoadState('networkidle')
    assert.equal(await notice.count(), 0)
    assert.equal((await context.request.get(`${origin}/api/maintenance`)).status(), 401)
  }
  assert.deepEqual(errors, [])
  await writeFile(
    `${root}/browser.json`,
    JSON.stringify({ mode, passed: true, real_api: true, reads, page_errors: errors }),
  )
  console.log(JSON.stringify({ mode, passed: true, real_api: true, page_errors: 0 }))
} finally {
  await browser.close()
}
