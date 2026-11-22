import { readonly, ref } from 'vue'

// 心情油墨：两色 Riso 套印。文字只用 ink/deep/soft/atext，浅色 accent 只做底色、套印和装饰。
export type InkKey =
  | 'blue-orange'
  | 'teal-sun'
  | 'ink-yellow'
  | 'green-red'
  | 'navy-mint'
  | 'purple-lime'
  | 'blue-pink'

export type DoodleName = 'sun' | 'cloud' | 'pencil' | 'leaf' | 'moon' | 'bulb' | 'heart'

export interface InkTheme {
  key: InkKey
  mood: string
  inks: string
  ink: string
  deep: string
  soft: string
  accent: string
  stamp: string
  atext: string
  paper: string
  doodle: DoodleName
  egg: string
}

export interface DoodleArt {
  tint: string
  ink: string
  accentStroke?: string
}

// 24×24 双色套印手绘涂鸦：浅层彩墨错位底晕 + 细腻主笔触，保留 doodlePaths 供轻量回退。
export const doodleArt: Record<DoodleName, DoodleArt> = {
  sun: {
    tint: 'M13 7.6a4.8 4.8 0 1 1-4.8 4.8A4.8 4.8 0 0 1 13 7.6Z',
    ink: 'M12 7.2c2.8 0 4.8 2 4.8 4.8s-2 4.8-4.8 4.8-4.8-2-4.8-4.8 2-4.8 4.8-4.8ZM12 2.2Q12.3 3.6 12 4.9M12 19.1Q11.7 20.5 12 21.8M2.2 12Q3.6 11.7 4.9 12M19.1 12Q20.5 12.3 21.8 12M5 5Q6.2 6.1 7.2 7.2M16.8 16.8Q17.9 17.9 19 19M5 19Q6.1 17.8 7.2 16.8M16.8 7.2Q17.9 6.1 19 5',
    accentStroke: 'M10.4 9.5a2.8 2.8 0 0 1 3.4-.2',
  },
  cloud: {
    tint: 'M16.6 5.2a3 3 0 1 1-3 3 3 3 0 0 1 3-3Zm-9 12.6h9.6a3.8 3.8 0 0 0 .2-7.6A5.4 5.4 0 0 0 7 11.3a3.2 3.2 0 0 0 .6 6.5Z',
    ink: 'M7 17.2h9.8a3.8 3.8 0 0 0 .3-7.6A5.3 5.3 0 0 0 6.8 10.9 3.2 3.2 0 0 0 7 17.2ZM15.2 6.6a2.6 2.6 0 0 1 3.9 2.4',
    accentStroke: 'M6 20.2q3-.8 6 0t6 0',
  },
  pencil: {
    tint: 'M5.5 18.8l1-4.2L16.8 4.3l3.2 3.2L9.7 17.8Z',
    ink: 'M4.5 19.5l1.1-4.8L16.2 4.1a1.2 1.2 0 0 1 1.7 0l2 2a1.2 1.2 0 0 1 0 1.7L9.3 18.4ZM14.2 6.1l3.7 3.7M5.6 14.7l3.7 3.7',
    accentStroke: 'M4.5 19.5c3.5 1.8 7.5 1.6 11 .1 2.2-.9 3.8.5 5 1.2',
  },
  leaf: {
    tint: 'M6.8 18.6C6.4 11.4 11.8 5.6 19.8 5.2c-.3 7.8-5.7 13.5-13 13.4Z',
    ink: 'M5.5 18.6C5.1 10.6 10.9 4.5 19.6 4.2c-.3 8.4-6 14.2-14.1 14.4ZM4.2 20c3.6-4.2 8.2-9 14.2-14.6M9.8 14.4Q8.6 11.6 9 9.5M13.2 11.1Q11.9 8.9 12.2 7.2M11 13.4q2.8.5 4.8-.2',
  },
  moon: {
    tint: 'M14.4 4.8a7.4 7.4 0 1 0 5.4 12 6.6 6.6 0 0 1-5.4-12Z',
    ink: 'M14.4 4a7.8 7.8 0 1 0 5.4 13.1A6.8 6.8 0 0 1 14.4 4Z',
    accentStroke:
      'M19 3.5q.3 1.7 2 2-1.7.3-2 2-.3-1.7-2-2 1.7-.3 2-2ZM20 10.5q.2 1 1.2 1.2-1 .2-1.2 1.2-.2-1-1.2-1.2 1-.2 1.2-1.2Z',
  },
  bulb: {
    tint: 'M12.8 4.4a5.5 5.5 0 0 0-3.2 10c.5.5.8 1.1.9 1.9h4.4c.1-.8.4-1.4.9-1.9a5.5 5.5 0 0 0-3-10Z',
    ink: 'M9.3 17.5h5.4M10.1 20.3h3.8M12 3.8a5.6 5.6 0 0 0-3.3 10.1c.6.5.9 1.1 1 2h4.6c.1-.9.4-1.5 1-2A5.6 5.6 0 0 0 12 3.8ZM10.4 12.5c.6-1.4 1-2.8 1.6-2.8s1 1.4 1.6 2.8',
    accentStroke: 'M4.5 9.2l1.6.5M19.5 9.2l-1.6.5M6.3 4.6l1.2 1.2M17.7 4.6l-1.2 1.2',
  },
  heart: {
    tint: 'M12.6 20s-6.8-4.4-6.8-9.5a3.8 3.8 0 0 1 6.8-2.3 3.8 3.8 0 0 1 6.8 2.3c0 5.1-6.8 9.5-6.8 9.5Z',
    ink: 'M12 19.6s-7-4.5-7-9.8a3.9 3.9 0 0 1 7-2.4 3.9 3.9 0 0 1 7 2.4c0 5.3-7 9.8-7 9.8Z',
    accentStroke: 'M8.2 8.9a1.8 1.8 0 0 1 1.8-1.4',
  },
}

// 24×24 描边小图标，保留路径导出以兼容旧调用。
export const doodlePaths: Record<DoodleName, string> = {
  sun: doodleArt.sun.ink,
  cloud: doodleArt.cloud.ink,
  pencil: doodleArt.pencil.ink,
  leaf: doodleArt.leaf.ink,
  moon: doodleArt.moon.ink,
  bulb: doodleArt.bulb.ink,
  heart: doodleArt.heart.ink,
}

export const inkThemes: readonly InkTheme[] = [
  {
    key: 'blue-orange',
    mood: '元气',
    inks: 'Blue × Fluorescent Orange',
    ink: '#2f4aa8',
    deep: '#1d2c66',
    soft: '#4b5a92',
    accent: '#ff7a2e',
    stamp: '#e0561a',
    atext: '#b4430c',
    paper: '#f3f1ec',
    doodle: 'sun',
    egg: '今天电量满格！',
  },
  {
    key: 'teal-sun',
    mood: '晴天',
    inks: 'Teal × Sunflower',
    ink: '#00727a',
    deep: '#0b4247',
    soft: '#3d6a6e',
    accent: '#ffc62e',
    stamp: '#b07a00',
    atext: '#8a5f00',
    paper: '#f2f2ed',
    doodle: 'cloud',
    egg: '天气不错，信也拿出来晒一晒',
  },
  {
    key: 'ink-yellow',
    mood: '专注',
    inks: 'Black × Yellow',
    ink: '#1d1f24',
    deep: '#111216',
    soft: '#50535c',
    accent: '#ffd200',
    stamp: '#9c7a00',
    atext: '#7a5f00',
    paper: '#f1f0ea',
    doodle: 'pencil',
    egg: '专心模式：不吵，但信照收',
  },
  {
    key: 'green-red',
    mood: '秋天',
    inks: 'Green × Bright Red',
    ink: '#2b6449',
    deep: '#173a2a',
    soft: '#4a6b5b',
    accent: '#f0533a',
    stamp: '#d63f22',
    atext: '#b0321b',
    paper: '#f3f1ec',
    doodle: 'leaf',
    egg: '一片叶子落在信上了',
  },
  {
    key: 'navy-mint',
    mood: '夜读',
    inks: 'Federal Blue × Mint',
    ink: '#22307a',
    deep: '#151d4d',
    soft: '#4a5486',
    accent: '#4fd1a0',
    stamp: '#178a5e',
    atext: '#136e4b',
    paper: '#f2f3ee',
    doodle: 'moon',
    egg: '夜里也留着一盏小灯',
  },
  {
    key: 'purple-lime',
    mood: '怪点子',
    inks: 'Purple × Fluorescent Green',
    ink: '#5a3a9e',
    deep: '#33205e',
    soft: '#5f5280',
    accent: '#b8e04a',
    stamp: '#5f8a00',
    atext: '#4d7000',
    paper: '#f2f1ee',
    doodle: 'bulb',
    egg: '叮——冒出一个怪点子',
  },
  {
    key: 'blue-pink',
    mood: '初版',
    inks: 'Blue × Fluorescent Pink',
    ink: '#2b4bb3',
    deep: '#1f2f6e',
    soft: '#4a5d9e',
    accent: '#ff4f9a',
    stamp: '#e8337f',
    atext: '#c0185f',
    paper: '#f3f1ec',
    doodle: 'heart',
    egg: '第一版油墨，留个纪念',
  },
]

// 后端提供“当前主题”字段前，正式页面固定使用默认主题；来访者不能更换。
export const defaultInkKey: InkKey = 'blue-orange'

const previewStorageKey = 'milo-home:ink-preview'
const inkVariables = ['ink', 'deep', 'soft', 'accent', 'stamp', 'atext', 'paper'] as const

function findInk(key: string | null | undefined): InkTheme | undefined {
  return inkThemes.find((theme) => theme.key === key)
}

const current = ref<InkTheme>(findInk(defaultInkKey) as InkTheme)

export function applyInk(key: InkKey): InkTheme {
  const theme = findInk(key) ?? (findInk(defaultInkKey) as InkTheme)
  const root = document.documentElement
  for (const name of inkVariables) {
    root.style.setProperty(`--${name}`, theme[name])
  }
  root.dataset.ink = theme.key
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme.paper)
  current.value = theme
  return theme
}

// 仅开发预览使用：记住上次预览的主题，读写失败时回到默认主题。
export function previewInk(key: InkKey) {
  applyInk(key)
  try {
    localStorage.setItem(previewStorageKey, key)
  } catch {
    // 浏览器禁止存储时只影响预览记忆。
  }
}

export function startInk() {
  let key: InkKey = defaultInkKey
  if (import.meta.env.DEV) {
    try {
      key = findInk(localStorage.getItem(previewStorageKey))?.key ?? defaultInkKey
    } catch {
      key = defaultInkKey
    }
  }
  applyInk(key)
}

export function useInk() {
  return { ink: readonly(current) }
}
