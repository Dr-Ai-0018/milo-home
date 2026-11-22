import { ApiFailure } from '../api/client'
import type { PublicNoteSummary } from '../api/client'
import { clockTime } from './time'

const beijingYearFormatter = new Intl.DateTimeFormat('en-US', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
})

const beijingDateParts = new Intl.DateTimeFormat('en-US', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: 'numeric',
  day: 'numeric',
})

function currentBeijingYear(nowMs = Date.now()): number {
  return Number(beijingYearFormatter.format(nowMs))
}

/**
 * 格式化作者显式填写的北京时间内容日期（YYYY-MM-DD）。
 * 历史页与详情页始终显示明确年份；首页在同年可略去年份，跨年自动补全。
 */
export function formatNoteDate(
  isoDate: string,
  options: { withYear?: boolean; nowMs?: number } = {},
): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate.trim())
  if (!match) return isoDate
  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  const showYear = options.withYear !== false || year !== currentBeijingYear(options.nowMs)
  return showYear ? `${year}年${month}月${day}日` : `${month}月${day}日`
}

/**
 * 仅在明确发布过后续修订（revision > 1 且 updated_at 晚于首次 published_at）时，
 * 返回带明确年份的北京时间修订时刻；首版或未变时不堆叠内部时间戳。
 */
export function formatNoteRevisedAt(
  note: Pick<PublicNoteSummary, 'revision' | 'published_at' | 'updated_at'>,
): string | null {
  if (note.revision <= 1 || note.updated_at <= note.published_at) return null
  const values: Record<string, string> = {}
  for (const part of beijingDateParts.formatToParts(note.updated_at * 1000)) {
    values[part.type] = part.value
  }
  const year = Number(values.year)
  const month = Number(values.month)
  const day = Number(values.day)
  return `${year}年${month}月${day}日 ${clockTime(note.updated_at)}`
}

/** 仅当列表接口明确返回 404 时才判定为旧后端缺端点，允许降级到随站旧近况。 */
export function isLegacyNotesMissing(error: unknown): boolean {
  return error instanceof ApiFailure && error.status === 404
}

/** 详情接口 404 表示该条目已撤下、未公开或不存在，绝不回退静态正文。 */
export function isNoteNotFound(error: unknown): boolean {
  return error instanceof ApiFailure && error.status === 404
}
