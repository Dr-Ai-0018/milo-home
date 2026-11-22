import type { Conversation } from '../api/client'
import { dayLabel, sameDay } from './time'

// 书信主题的显示名：标题是来访者自己写的纯文本；默认那叠承载全部旧信件。
export function letterTitle(letter: Conversation) {
  if (letter.title) return letter.title
  return letter.is_default ? '第一叠信' : '没写标题的信'
}

// 最近有往来的信在前；空信按开信的时间算。默认那叠也按往来排，不再固定放最后。
export function lastActive(letter: Conversation) {
  return letter.last_message_at ?? letter.created_at
}

export function sortLetters(conversations: readonly Conversation[]) {
  return conversations
    .filter((item) => item.kind === 'letter')
    .sort((a, b) => lastActive(b) - lastActive(a) || b.created_at - a.created_at)
}

// 最近往来的日子：今天、昨天，其余写日期；日期后自带空格，接在中文后面不显得挤。
export function nearDay(seconds: number, now = Date.now() / 1000) {
  if (sameDay(seconds, now)) return '今天'
  if (sameDay(seconds, now - 86_400)) return '昨天'
  return `${dayLabel(seconds, now)} `
}

// 信封上的一行：最后一封是谁、哪天写的，加上一共几封；空信只说哪天开的。
export function envelopeLine(letter: Conversation) {
  if (!letter.message_count || !letter.last_message_at) {
    return `${nearDay(letter.created_at)}开的，还空着`
  }
  const day = nearDay(letter.last_message_at)
  const who = letter.last_message_role === 'milo' ? `米洛 ${day}回了` : `你 ${day}写的`
  return `${who} · 共 ${letter.message_count} 封`
}

// 没有在等的信时说明为什么：有全会话汇总时按真实决定说，旧后端缺字段时沿用旧语义。
export function quietLine(letter: Conversation) {
  if (!letter.message_count) return ''
  const summary = letter.reply_summary
  if (summary) {
    if (
      summary.unprocessed_count === 0 &&
      summary.deferred_count === 0 &&
      summary.unknown_count === 0
    ) {
      if (summary.replied_count > 0 && summary.no_reply_count === 0) return '都回过了'
      if (summary.no_reply_count > 0 && summary.replied_count === 0) return '已收下'
      if (summary.replied_count > 0 && summary.no_reply_count > 0) return '都看过也收下了'
      return '没有在等的'
    }
  }
  return letter.last_message_role === 'milo' ? '都回过了' : '没有在等的'
}

export interface LetterStatusBadge {
  label: string
  tone: 'waiting' | 'quiet'
}

// 信封右下角状态：只把真正待回的盖成提醒章；暂缓回访与旧未记记录安静写明，不催促。
export function letterStatusBadge(letter: Conversation): LetterStatusBadge | null {
  const summary = letter.reply_summary
  if (summary) {
    if (summary.unprocessed_count > 0) {
      return { label: `${summary.unprocessed_count} 封等回信`, tone: 'waiting' }
    }
    if (summary.deferred_count > 0) {
      return { label: `${summary.deferred_count} 封留待回访`, tone: 'quiet' }
    }
    if (summary.unknown_count > 0) {
      return { label: `${summary.unknown_count} 封旧信未记`, tone: 'quiet' }
    }
    return null
  }
  if (letter.pending_count > 0) {
    return { label: `${letter.pending_count} 封等回信`, tone: 'waiting' }
  }
  return null
}

// 单封长信顶部的简明状态短语，按全会话汇总呈现，不靠当前页猜测。
export function letterHeaderStatus(letter: Conversation) {
  if (!letter.message_count) return '还空着'
  const summary = letter.reply_summary
  if (summary) {
    const parts: string[] = []
    if (summary.unprocessed_count > 0) parts.push(`${summary.unprocessed_count} 封在等回信`)
    if (summary.deferred_count > 0) parts.push(`${summary.deferred_count} 封留待回访`)
    if (!parts.length && summary.unknown_count > 0) {
      parts.push(`${summary.unknown_count} 封旧记录暂未见回复`)
    }
    return parts.length ? parts.join(' · ') : quietLine(letter)
  }
  return letter.pending_count ? `${letter.pending_count} 封在等回信` : quietLine(letter)
}

// 信箱门牌汇总行：区分待回、留待回访与旧记录未知，不把暂缓或无需回复算进催回计数。
export function mailboxPendingLine(letters: readonly Conversation[]) {
  let unprocessed = 0
  let deferred = 0
  let unknown = 0
  for (const letter of letters) {
    if (letter.reply_summary) {
      unprocessed += letter.reply_summary.unprocessed_count
      deferred += letter.reply_summary.deferred_count
      unknown += letter.reply_summary.unknown_count
    } else {
      unprocessed += letter.pending_count
    }
  }
  if (unprocessed > 0 && deferred > 0) {
    return `有 ${unprocessed} 封信还在等回信，另有 ${deferred} 封留待回访。`
  }
  if (unprocessed > 0) return `有 ${unprocessed} 封信还在等回信。`
  if (deferred > 0) return `暂时没有等回信的信，有 ${deferred} 封米洛留着稍后回访。`
  if (unknown > 0) return `有 ${unknown} 封更早的信缺少当时的回复记录。`
  return '暂时没有等回信的信。'
}
