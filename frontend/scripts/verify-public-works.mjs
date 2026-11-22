import assert from 'node:assert/strict'
import { createHash, randomUUID } from 'node:crypto'
import { mkdir, readFile, unlink, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath, pathToFileURL } from 'node:url'

const [origin, directory, mode, playwright] = process.argv.slice(2)
assert.equal(new URL(origin).hostname, '127.0.0.1')
const root = resolve(directory)
const repository = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')
const { chromium } = await import(pathToFileURL(playwright).href)
const browser = await chromium.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] })
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage()
page.setDefaultTimeout(5000)
const errors = []
const writes = []
let catalogReads = 0
page.on('pageerror', (error) => errors.push(error.message))
page.on('console', (message) => {
  if (
    message.type() === 'error' &&
    !/Failed to load resource:.*\b(401|404|503)\b/.test(message.text())
  ) {
    errors.push(message.text())
  }
})
page.on('request', (request) => {
  if (request.method() !== 'GET') writes.push(request.method() + ' ' + request.url())
  if (request.url().includes('/api/works?limit=10')) catalogReads += 1
})

function cli(action, ...arguments_) {
  const result = spawnSync(
    resolve(repository, 'backend/.venv/bin/milo-home'),
    ['content', 'work', action, ...arguments_],
    {
      env: {
        ...process.env,
        MILO_DATABASE: `${root}/data/home.sqlite3`,
        MILO_PUBLIC_ASSET_ROOT: `${root}/public-assets`,
        MILO_PUBLIC_ASSET_MAX_BYTES: '268435456',
      },
      encoding: 'utf8',
      timeout: 5000,
    },
  )
  assert.equal(result.status, 0, `Isolated ${action} command failed: ${result.stderr}`)
  return JSON.parse(result.stdout)
}

async function prepareSyntheticMaterials() {
  const examples = JSON.parse(
    await readFile(resolve(repository, 'examples/public-works.json'), 'utf8'),
  )
  const pngFixture = examples.binary_fixtures.find((item) => item.content_type === 'image/png')
  const fileFixture = examples.binary_fixtures.find(
    (item) => item.content_type === 'application/octet-stream',
  )
  assert.ok(pngFixture && fileFixture, 'Synthetic binary fixtures missing')
  const materialDir = `${root}/synthetic-materials`
  await mkdir(materialDir, { recursive: true })
  const coverBytes = Buffer.from(pngFixture.body_base64, 'base64')
  const documentBytes = Buffer.from(fileFixture.body_base64, 'base64')
  const coverFilename = 'synthetic-cover.png'
  const documentFilename = 'synthetic-guide.dat'
  await writeFile(`${materialDir}/${coverFilename}`, coverBytes)
  await writeFile(`${materialDir}/${documentFilename}`, documentBytes)
  const coverAssetId = randomUUID()
  const documentAssetId = randomUUID()
  const bodyText = '这是仅供隔离组合验证的合成作品正文，不代表真实站内作品。'
  const draft = {
    title: '合成作品：隔离联验示例',
    summary: '明确标记的合成作品，仅用于隔离验证发布与读取。',
    body: bodyText,
    category: '合成样例',
    version_label: '2026 合成版',
    content_date: '2026-11-22',
    credits: '合成验证样例',
    links: [{ label: '访问示例网站', url: 'https://example.org/' }],
    files: [],
    cover: null,
  }
  const work = {
    ...draft,
    files: [documentAssetId],
    cover: {
      asset_id: coverAssetId,
      alt: '合成封面：两格横向小图',
      width: 2,
      height: 1,
    },
  }
  await writeFile(`${materialDir}/draft.json`, JSON.stringify(draft))
  await writeFile(`${materialDir}/work.json`, JSON.stringify(work))
  return {
    materialDir,
    bodyText,
    work,
    cover: { assetId: coverAssetId, filename: coverFilename },
    document: {
      assetId: documentAssetId,
      filename: documentFilename,
      sha256: createHash('sha256').update(documentBytes).digest('hex'),
    },
  }
}

async function waitText(selector, text) {
  await page.waitForFunction(
    ([target, expected]) => document.querySelector(target)?.textContent.includes(expected),
    [selector, text],
  )
}

async function saveDownload(expectedSha, name) {
  const pending = page.waitForEvent('download')
  await page.locator('.work-file-download-btn').first().click()
  const download = await pending
  const destination = `${root}/${name}`
  await download.saveAs(destination)
  assert.equal(
    createHash('sha256')
      .update(await readFile(destination))
      .digest('hex'),
    expectedSha,
  )
  return download.suggestedFilename()
}

async function sizes() {
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 900 })
    await page.emulateMedia({ reducedMotion: width === 320 ? 'reduce' : 'no-preference' })
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
    )
    await page.screenshot({
      path: `${root}/${mode}-${width}.png`,
      fullPage: true,
      animations: 'disabled',
    })
  }
  await page.setViewportSize({ width: 1440, height: 900 })
}

async function lifecycle() {
  await page.goto(origin)
  await waitText('.home-works-section', '还没有公开作品')
  const synthetic = await prepareSyntheticMaterials()
  const identity = randomUUID()
  cli('create', '--id', identity, '--file', `${synthetic.materialDir}/draft.json`)
  assert.equal((await page.request.get(`${origin}/api/works/${identity}`)).status(), 404)
  for (const asset of [synthetic.cover, synthetic.document]) {
    cli(
      'asset',
      'import',
      '--id',
      identity,
      '--asset-id',
      asset.assetId,
      '--file',
      `${synthetic.materialDir}/${asset.filename}`,
    )
  }
  let state = cli(
    'revise',
    '--id',
    identity,
    '--file',
    `${synthetic.materialDir}/work.json`,
    '--expected-version',
    '1',
  )
  state = cli(
    'publish',
    '--id',
    identity,
    '--expected-version',
    String(state.version),
    '--confirm-public',
  )
  await page.locator('.home-works-reload').click()
  await page.locator('.home-works-all-link').click()
  await page.locator('.work-title-link').click()
  await waitText('.work-detail-body', synthetic.bodyText)
  const details = await (await page.request.get(`${origin}/api/works/${identity}`)).json()
  const oldDownload = details.files[0].download_url
  const oldCover = details.cover.url
  await page.waitForFunction(() => document.querySelector('.work-cover-img')?.naturalWidth > 0)
  assert.equal(
    await saveDownload(synthetic.document.sha256, 'actual-guide.dat'),
    synthetic.document.filename,
  )
  await sizes()
  const readsBeforeReturn = catalogReads
  await page.goBack()
  await page.waitForURL('**/#/works')
  await page.waitForFunction(() => document.activeElement?.classList.contains('work-title-link'))
  assert.equal(catalogReads, readsBeforeReturn)
  await page.locator('.work-title-link').click()
  await waitText('.work-detail-body', synthetic.bodyText)

  await writeFile(`${root}/cover-busy`, 'synthetic')
  await page.reload()
  await page.locator('.work-cover-fallback').waitFor()
  await unlink(`${root}/cover-busy`)
  await page.locator('.works-refresh-btn').click()
  await page.waitForFunction(() => document.querySelector('.work-cover-img')?.naturalWidth > 0)

  const changed = { ...synthetic.work, body: '仅供隔离组合验证的新稿', cover: null, files: [] }
  const newAsset = randomUUID()
  const raw = Buffer.from('<script>synthetic-download-only</script>')
  await writeFile(`${root}/selected.html`, raw)
  cli(
    'asset',
    'import',
    '--id',
    identity,
    '--asset-id',
    newAsset,
    '--file',
    `${root}/selected.html`,
  )
  changed.files = [newAsset]
  await writeFile(`${root}/revision.json`, JSON.stringify(changed))
  state = cli(
    'revise',
    '--id',
    identity,
    '--file',
    `${root}/revision.json`,
    '--expected-version',
    String(state.version),
  )
  await page.locator('.works-refresh-btn').click()
  await waitText('.work-detail-body', synthetic.bodyText)
  state = cli(
    'publish',
    '--id',
    identity,
    '--expected-version',
    String(state.version),
    '--confirm-public',
  )
  await page.locator('.work-file-download-btn').click()
  await waitText('.work-file-error', '当前不在作品的公开版本中')
  assert.equal(await page.locator('.work-detail-body').count(), 1)
  assert.equal((await page.request.get(origin + oldDownload)).status(), 404)
  assert.equal((await page.request.get(origin + oldCover)).status(), 404)
  await page.locator('.work-file-reload-btn').click()
  await waitText('.work-detail-body', changed.body)
  assert.equal(await page.locator('.work-cover-img').count(), 0)
  await saveDownload(createHash('sha256').update(raw).digest('hex'), 'actual-html.bin')
  await writeFile(`${root}/json-busy`, 'synthetic')
  await page.locator('.works-refresh-btn').click()
  await waitText('.works-inline-notice', '暂留')
  await waitText('.work-detail-body', changed.body)
  assert.equal(
    await page.locator('.works-refresh-btn').evaluate((el) => el === document.activeElement),
    true,
  )
  await unlink(`${root}/json-busy`)
  await page.locator('.works-refresh-btn').click()
  await page.waitForFunction(
    () => document.querySelector('.works-refresh-btn')?.getAttribute('aria-busy') === 'false',
  )
  cli('withdraw', '--id', identity, '--expected-version', String(state.version))
  await page.locator('.works-refresh-btn').click()
  await waitText('.works-state-box', '这件作品当前不可读取')
  assert.equal(await page.locator('.work-detail-body').count(), 0)
  assert.equal((await page.request.get(origin + oldDownload)).status(), 404)
  await page.locator('.works-back-link').click()
  await waitText('.works-state-box', '还没有公开作品')
}

async function history() {
  await page.goto(`${origin}/#/works`)
  await page.waitForFunction(() => document.querySelectorAll('.works-list-item').length === 10)
  const id = randomUUID()
  await writeFile(
    `${root}/new.json`,
    JSON.stringify({
      title: '合成后来新发',
      summary: '仅供隔离',
      body: '仅供隔离',
      content_date: '2026-11-22',
      links: [{ label: '访问示例', url: 'https://example.org/' }],
    }),
  )
  cli('create', '--id', id, '--file', `${root}/new.json`)
  cli('publish', '--id', id, '--expected-version', '1', '--confirm-public')
  cli('withdraw', '--id', '00000000-0000-0000-0000-000000000003', '--expected-version', '2')
  await page.locator('.works-more-btn').click()
  await page.waitForFunction(() => document.querySelectorAll('.works-list-item').length === 13)
  assert.equal(await page.locator('.works-more-btn').count(), 0)
  assert.equal((await page.locator('.works-list').innerText()).includes('合成后来新发'), false)
  await page.locator('.work-title-link').last().click()
  await page.locator('.work-detail-body').waitFor()
  await page.goBack()
  await page.waitForURL('**/#/works')
  assert.equal(await page.locator('.works-list-item').count(), 13)
  await page.locator('.works-refresh-btn').click()
  await page.waitForFunction(() => document.querySelectorAll('.works-list-item').length === 10)
  await waitText('.works-list', '合成后来新发')
  await sizes()
}

async function legacy() {
  await page.goto(origin)
  await waitText('.home-works-section', '作品还没接到这里')
  assert.equal(await page.locator('.home-works-all-link').count(), 0)
  await page.goto(`${origin}/#/works`)
  await waitText('.works-state-box', '作品还没接到这里')
  assert.equal(await page.locator('.work-title-link').count(), 0)
  await sizes()
}

try {
  await { lifecycle, history, legacy }[mode]()
  assert.deepEqual(writes, [])
  assert.deepEqual(errors, [])
  assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0])
  await writeFile(
    `${root}/browser.json`,
    `${JSON.stringify({ passed: true, real_http: true, mode, errors, writes, model_calls: 0 })}\n`,
  )
  console.log(JSON.stringify({ passed: true, real_http: true, mode }))
} finally {
  await browser.close()
}
