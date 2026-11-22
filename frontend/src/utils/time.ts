// 页面统一按北京时间显示：「10月16日 10:43」，跨年时才带年份。
const zone = 'Asia/Shanghai'

const partsFormatter = new Intl.DateTimeFormat('en-US', {
  timeZone: zone,
  year: 'numeric',
  month: 'numeric',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
})

interface ClockParts {
  year: number
  month: number
  day: number
  hour: number
  minute: number
}

function parts(seconds: number): ClockParts {
  const values: Record<string, string> = {}
  for (const part of partsFormatter.formatToParts(seconds * 1000)) values[part.type] = part.value
  return {
    year: Number(values.year),
    month: Number(values.month),
    day: Number(values.day),
    hour: Number(values.hour),
    minute: Number(values.minute),
  }
}

const pad = (value: number) => String(value).padStart(2, '0')

export function clockTime(seconds: number) {
  const { hour, minute } = parts(seconds)
  return `${pad(hour)}:${pad(minute)}`
}

export function dayLabel(seconds: number, now = Date.now() / 1000) {
  const { year, month, day } = parts(seconds)
  const prefix = year === parts(now).year ? '' : `${year}年`
  return `${prefix}${month}月${day}日`
}

export function stampTime(seconds: number, now = Date.now() / 1000) {
  return `${dayLabel(seconds, now)} ${clockTime(seconds)}`
}

export function postmarkDay(seconds: number) {
  const { month, day } = parts(seconds)
  return `${month}.${pad(day)}`
}

// 章面上的日期：方章「10.17 14:05」，圆章带年份「2026.10.17 14:31」。
export function stampDate(seconds: number, withYear = false) {
  const { year, month, day } = parts(seconds)
  const date = `${pad(month)}.${pad(day)} ${clockTime(seconds)}`
  return withYear ? `${year}.${date}` : date
}

export function isoTime(seconds: number) {
  return new Date(seconds * 1000).toISOString()
}

export function sameDay(a: number, b: number) {
  const left = parts(a)
  const right = parts(b)
  return left.year === right.year && left.month === right.month && left.day === right.day
}

// 一天中的小时数（含分钟小数），用于作息圆环的指针。
export function dayHours(seconds: number) {
  const { hour, minute } = parts(seconds)
  return hour + minute / 60
}

// 「21:30」这样的窗口时刻转成小时数；契约允许 24:00 表示当天结束。
export function windowHours(value: string) {
  const [hour = '0', minute = '0'] = value.split(':')
  return Number(hour) + Number(minute) / 60
}
