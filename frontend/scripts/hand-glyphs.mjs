// 输出手写体子集需要覆盖的字符：可打印 ASCII 加上固定文案里的全部非 ASCII 字符。
import { readFileSync } from 'node:fs'

const sources = ['src/theme/handCopy.ts', 'src/theme/inks.ts']
const glyphs = new Set()

for (let code = 0x20; code < 0x7f; code += 1) {
  glyphs.add(String.fromCharCode(code))
}

for (const source of sources) {
  const text = readFileSync(new URL(`../${source}`, import.meta.url), 'utf8')
  const code = text
    .split('\n')
    .filter((line) => !line.trim().startsWith('//'))
    .join('\n')
  for (const char of code) {
    if (char.codePointAt(0) > 0x7f) glyphs.add(char)
  }
}

process.stdout.write([...glyphs].sort().join(''))
