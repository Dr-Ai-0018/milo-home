import type { Schedule } from '../api/client'
import type { InkKey } from '../theme/inks'

export { HOME_RECENT_NOTES, SCHEDULE_BOUNDARY_COPY } from './homeNotes'
export type { HomeRecentNote } from './homeNotes'

export type HomePreviewScenario =
  'guest' | 'signed-no-work' | 'signed-work' | 'work-failed' | 'schedule-slow' | 'schedule-failed'

export const COMBINED_PREVIEW_SUMMARY =
  '窗景开篇首屏（淡标题重影、留白给窗景与入口）× 家刊式近况排法（一条信纸主稿 + 两则日期短讯）。'

export const SCENARIO_OPTIONS: readonly {
  id: HomePreviewScenario
  label: string
  shortNote: string
}[] = [
  { id: 'guest', label: '访客（未登录）', shortNote: '明确邀请制，突出写信与纸条' },
  { id: 'signed-no-work', label: '已登录 · 无交办权', shortNote: '熟人回归直进书信或纸条' },
  { id: 'signed-work', label: '已登录 · 有交办权', shortNote: '主入口推去交办，顶栏同步展示' },
  { id: 'work-failed', label: '交办权限核对失败', shortNote: '书信纸条稳住不跳，附局部重试' },
  { id: 'schedule-slow', label: '作息慢响应（900ms）', shortNote: '预留空表盘骨架，到达零位移' },
  { id: 'schedule-failed', label: '作息读取失败', shortNote: '保住公开文案与入口，提示再看一眼' },
]

export const PREVIEW_INK_CHOICES: readonly { key: InkKey; label: string }[] = [
  { key: 'blue-orange', label: '元气（蓝×橙）' },
  { key: 'green-red', label: '秋天（绿×红）' },
]

// 2026-11-14 15:20 CST（北京时间）示例作息，仅作双版静态/慢网演示。
export const PREVIEW_SCHEDULE_SAMPLE: Schedule = {
  timezone: 'Asia/Shanghai',
  automatic_wakeup: false,
  configured: true,
  paused: false,
  phase: 'working',
  source: 'weekly',
  observed_at: 1794640800,
  revision: 24,
  next_change_at: 1794650400,
  next_fallback_at: null,
  override_ends_at: null,
  today: {
    windows: [
      { start: '09:30', end: '12:00', phase: 'working' },
      { start: '14:00', end: '18:00', phase: 'working' },
      { start: '18:00', end: '19:30', phase: 'winding_down' },
    ],
    fallback: ['09:30', '14:00'],
  },
}

export const HOME_DESIGN_RULES: readonly { title: string; rule: string }[] = [
  {
    title: '标题、橙色重心与字重',
    rule: '首页 H1 为 clamp(36px, 4.8vw, 56px)、字重 700（舍弃易糊的 900 伪粗体），蓝橙重影淡收至 24%，把最暖的橙留给右面窗景。',
  },
  {
    title: '人话欢迎语与静默权限',
    rule: '平常不显示权限检测条或能力清单；有权态只写欢迎语，仅在访客或权限核对失败时说明原因并给本地重试。',
  },
  {
    title: '无硬框纸面与近况主次',
    rule: '作息与主近况复用无边框 PaperSheet（胶带、横线、轻纸影），下接两栏虚线分隔日期短讯，不套多余胶囊小框。',
  },
  {
    title: '零跳动占位（Zero-CLS）',
    rule: '作息圆环桌面（210px）与窄屏（136px）固定尺寸虚线空盘占位；失败重试槽独立于按钮下方，异步到达零位移。',
  },
  {
    title: '控件与焦点保位',
    rule: '按钮保持 44px+ 点击高度，第三入口收敛为轻量文字链接；等待中仅标 aria-disabled，回车焦点不掉回 body。',
  },
  {
    title: '窄屏阅读顺序',
    rule: '<=800px 下顺序固定为「标题与入口 → 米洛现在 → 家里最近 → 260px 轻量窗景」，320px/390px 零横向溢出。',
  },
]
