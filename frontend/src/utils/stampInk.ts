// 橡皮章每次盖下去的样子：轻重、缺口、墨量、重影、溅点都不一样。
// 只由种子决定，同一条记录在任何设备、刷新多少次都盖成同一个样子。

export type Rng = () => number

// 字符串种子（任务 + 第几条记录）先做 FNV-1a，再交给 Park–Miller。
export function seedOf(text: string) {
  let hash = 0x811c9dc5
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index)
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return hash % 2147483646 || 7
}

export function rng(seed: number): Rng {
  let state = seed % 2147483647 || 7
  return () => (state = (state * 16807) % 2147483647) / 2147483647
}

// 同一张单子上越往后盖墨越干；结论章重新蘸过墨。
export function inkAt(index: number) {
  return Math.max(0.3, [1, 0.7, 0.45][index] ?? 0.3)
}

// 外框 1–3 处小缺口，配合 pathLength=100 使用。
function nicks(random: Rng) {
  const gaps = Array.from({ length: 1 + Math.floor(random() * 3) }, () => random() * 100)
  gaps.sort((a, b) => a - b)
  const dashes: string[] = []
  let at = 0
  for (const gap of gaps) {
    if (gap - at < 4) continue
    const width = 0.8 + random() * 2.6
    dashes.push((gap - at).toFixed(1), width.toFixed(1))
    at = gap + width
  }
  dashes.push((100 - at + 50).toFixed(1), '0')
  return dashes.join(' ')
}

const fixed = (value: number, digits = 2) => Number(value.toFixed(digits))

export interface StampLook {
  rotation: number
  noise: number
  rough: number
  nicks: string
  // 漏白阈值：越小越干，白点越多。
  dry: number
  // 按压方向：一边实一边虚，偶尔一角几乎没印上。
  pressAngle: number
  pressMid: number
  pressLow: number
  inkOpacity: number
  poolOpacity: number
  ghost: { x: number; y: number } | null
  // 章座边缘印出来的一小段淡线，从第几分之一处开始。
  baseEdge: number | null
  dots: { angle: number; reach: number; size: number }[]
  // 纸面压痕的深浅，和墨量无关。
  dent: number
  // 摆放时的错位，单位 px。
  nudge: { x: number; y: number }
}

export function stampLook(seed: number, ink: number, tilt = 5): StampLook {
  const random = rng(seed)
  const rotation = fixed(random() * tilt * 2 - tilt, 1)
  const noise = Math.floor(random() * 900) + 1
  const rough = fixed(1.4 + random() * 1.4, 1)
  const cuts = nicks(random)
  const dry = fixed(2.3 + ink * 0.45 + random() * 0.3)
  const pressAngle = Math.floor(random() * 360)
  const pressLow = fixed(random() < 0.2 ? 0.12 : 0.38 + random() * 0.35)
  const pressMid = fixed(0.35 + random() * 0.35)
  const inkOpacity = fixed(0.72 + ink * 0.2 + random() * 0.06)
  const poolOpacity = fixed(0.35 + ink * 0.3)
  const ghost =
    random() < 0.28
      ? { x: fixed(random() * 2.4 - 1.2, 1), y: fixed(random() * 2.4 - 1.2, 1) }
      : null
  const baseEdge = random() < 0.3 ? Math.floor(random() * 4) / 4 : null
  const dotCount = random() < 0.45 ? 1 + Math.floor(random() * 3) : 0
  const dots = Array.from({ length: dotCount }, () => ({
    angle: fixed(random() * Math.PI * 2, 3),
    reach: fixed(3 + random() * 5, 1),
    size: fixed(0.5 + random() * 0.9, 1),
  }))
  const dent = fixed(0.08 + random() * 0.08)
  const nudge = { x: Math.round(random() * 10 - 6), y: Math.round(random() * 10 - 4) }
  return {
    rotation,
    noise,
    rough,
    nicks: cuts,
    dry,
    pressAngle,
    pressMid,
    pressLow,
    inkOpacity,
    poolOpacity,
    ghost,
    baseEdge,
    dots,
    dent,
    nudge,
  }
}
