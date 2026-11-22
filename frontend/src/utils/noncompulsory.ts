import type {
  ConversationMessage,
  MessageDecision,
  Reception,
  Revisit,
  RevisitSummary,
} from '../api/client'
import { stampTime } from './time'

export type EffectiveMessageState =
  'replied' | 'no_reply' | 'deferred' | 'unprocessed' | 'legacy_replied' | 'legacy_unreplied'

export interface ResolvedReception {
  reception: Reception | null
  lifeMs: number
}

// 校验接收安排的独立观测有效期：缺失、过期或时间戳矛盾时降为 null，不保留过期承诺。
export function resolveReceptionLifetime(
  raw: Reception | null | undefined,
  parentObservedAt?: number,
): ResolvedReception {
  if (!raw) return { reception: null, lifeMs: 0 }
  const observedAt = raw.observed_at
  const validUntil = raw.valid_until
  const referenceNow = typeof parentObservedAt === 'number' ? parentObservedAt : observedAt
  if (
    typeof observedAt !== 'number' ||
    typeof validUntil !== 'number' ||
    validUntil <= observedAt ||
    observedAt > referenceNow ||
    validUntil <= referenceNow
  ) {
    return { reception: null, lifeMs: 0 }
  }
  let endAt = validUntil
  if (raw.mode !== 'available' && raw.resume_on === 'at_time' && typeof raw.until === 'number') {
    if (raw.until <= referenceNow) {
      return { reception: null, lifeMs: 0 }
    }
    endAt = Math.min(endAt, raw.until)
  }
  return {
    reception: raw,
    lifeMs: Math.max(0, endAt - referenceNow) * 1000,
  }
}

const RECEPTION_MODE_LABEL: Record<Exclude<Reception['mode'], 'available'>, string> = {
  focusing: '米洛这会儿在专注手头的事',
  resting: '米洛这会儿在休息',
  later: '米洛暂时把接收调到了稍后再看',
}

function receptionScopeApplies(scope: Reception['scope'], context: 'conversation' | 'work') {
  if (scope === 'all') return true
  return context === 'work' ? scope === 'work' : scope === 'reply'
}

// 把米洛本人的临时接收安排翻译成克制的人话；available 时静默，绝不冒充在线、常规作息或服务故障。
export function describeReception(
  reception: Reception | null | undefined,
  context: 'conversation' | 'work',
): string {
  if (!reception || reception.mode === 'available') return ''
  if (!receptionScopeApplies(reception.scope, context)) return ''

  const head = RECEPTION_MODE_LABEL[reception.mode]
  const scopeHint =
    reception.scope === 'work'
      ? '（仅交办）'
      : reception.scope === 'reply'
        ? '（仅纸条与书信）'
        : ''

  if (reception.resume_on === 'at_time' && typeof reception.until === 'number') {
    return `${head}${scopeHint}，预计 ${stampTime(reception.until)} 后再看新内容；你照常留言或交办都会安全存好。`
  }
  if (reception.resume_on === 'new_input') {
    return `${head}${scopeHint}，有新内容时会再看；你随时可以写，不会弄丢。`
  }
  if (reception.resume_on === 'explicit') {
    return `${head}${scopeHint}，等他自己恢复接收后再看；这段时间写下的内容都会安全存好。`
  }
  return `${head}${scopeHint}，你写下的内容都会安全存好。`
}

const BLOCKED_REASON_LABEL: Record<NonNullable<Revisit['blocked_reason']>, string> = {
  schedule: '当前不在作息时段',
  reception: '按米洛当前的接收安排暂缓',
  budget: '暂时受限于当前额度',
  service: '自动服务目前不可用',
  occupancy: '米洛手头正占着别的事',
}

export function hasActiveRevisit(revisit: Revisit | null | undefined): revisit is Revisit {
  return !!revisit && revisit.state !== 'cancelled' && revisit.state !== 'completed'
}

// 单条消息或单件交办上的回访意图：已取消/已完成不再显示为未来安排。
export function describeRevisit(revisit: Revisit | null | undefined): string {
  if (!hasActiveRevisit(revisit)) return ''
  switch (revisit.state) {
    case 'scheduled':
      return `计划 ${stampTime(revisit.due_at)} 左右回访`
    case 'running':
      return '正在回访续聊'
    case 'blocked': {
      const reason = revisit.blocked_reason ? BLOCKED_REASON_LABEL[revisit.blocked_reason] : ''
      return reason
        ? `原定 ${stampTime(revisit.due_at)} 的回访已暂缓（${reason}）`
        : `原定 ${stampTime(revisit.due_at)} 的回访目前暂缓`
    }
    case 'needs_reconciliation':
      return '这次回访的处理情况尚待核对'
    default:
      return ''
  }
}

// 会话级回访汇总：只在确有未结束回访时补一句温和说明，不把已取消回访复活成未来安排。
export function describeRevisitSummary(
  summary: RevisitSummary | null | undefined,
  reception: Reception | null | undefined,
): string {
  if (!summary) return ''
  if (summary.reconciliation_count > 0) {
    return `有 ${summary.reconciliation_count} 次回访尚待核对，核对前不判定为已完成或暂缓。`
  }
  if (summary.active_count > 0) {
    return '米洛正在按原定回访计划续看这段对话。'
  }
  if (summary.blocked_count > 0) {
    const dueText = summary.next_due_at ? `（原定 ${stampTime(summary.next_due_at)}）` : ''
    if (reception && reception.mode !== 'available') {
      return `有 ${summary.blocked_count} 次到点回访${dueText}因米洛当前的接收安排暂缓。`
    }
    return `有 ${summary.blocked_count} 次到点回访${dueText}目前被阻挡，暂未继续。`
  }
  if (summary.ready_count > 0) {
    return `有 ${summary.ready_count} 次回访已到点，正等待接续。`
  }
  if (summary.pending_count > 0) {
    return summary.next_due_at
      ? `米洛留了后续回访，计划在 ${stampTime(summary.next_due_at)} 左右再来看看。`
      : `米洛留了 ${summary.pending_count} 次后续回访。`
  }
  return ''
}

// 提取一条米洛回信覆盖的来访者消息 ID：明确给出 reply_to_ids 数组时以它为准（含空数组），旧数据缺省/null 时才退回 in_reply_to。
export function replyTargetIds(message: ConversationMessage): string[] {
  if (Array.isArray(message.reply_to_ids)) {
    return message.reply_to_ids
  }
  return message.in_reply_to ? [message.in_reply_to] : []
}

// 汇总当前已读消息中已被任意米洛回信覆盖到的来访者消息 ID（兼容单回与合回）。
export function coveredVisitorIds(messages: readonly ConversationMessage[]): Set<string> {
  const covered = new Set<string>()
  for (const message of messages) {
    if (message.role !== 'milo') continue
    for (const id of replyTargetIds(message)) {
      covered.add(id)
    }
  }
  return covered
}

// 判定单条来访者消息的真实决定状态：有新字段绝不退回猜测；旧后端缺失/null 时保留旧兼容语义。
export function effectiveMessageState(
  message: ConversationMessage,
  coveredIds: ReadonlySet<string>,
): EffectiveMessageState {
  const decision: MessageDecision | null | undefined = message.reply_decision
  if (decision) return decision.state
  return coveredIds.has(message.id) ? 'legacy_replied' : 'legacy_unreplied'
}
