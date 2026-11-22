import { mkdtemp, readFile, unlink, rmdir } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { execFileSync } from 'node:child_process'

const directory = await mkdtemp(join(tmpdir(), 'milo-api-types-'))
const generated = join(directory, 'schema.d.ts')
try {
  execFileSync(
    'pnpm',
    ['exec', 'openapi-typescript', '../contracts/openapi.json', '-o', generated],
    { stdio: 'inherit' },
  )
  const expected = await readFile('src/api/schema.d.ts', 'utf8')
  const actual = await readFile(generated, 'utf8')
  if (actual !== expected) throw new Error('API types differ; run pnpm api:generate')
  console.log('OpenAPI types match the committed contract')
} finally {
  await unlink(generated).catch((error) => {
    if (error.code !== 'ENOENT') throw error
  })
  await rmdir(directory)
}
