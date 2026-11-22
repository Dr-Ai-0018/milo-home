import type { Activity, Reception, ReplyQueue, ReplySummary, RevisitSummary } from '../api/client'
import type { EffectiveReplyService } from '../composables/useActivity'
import { describeReception, describeRevisitSummary } from './noncompulsory'
import { clockTime, stampTime } from './time'

export type Processing = Activity['processing']

export interface ConversationStatusView {
  headline: string
  tone: 'live' | 'waiting' | 'warn' | 'muted'
  detail: string
  receptionNote: string
  revisitNote: string
  serviceNote: string
}

export interface ConversationStatusInput {
  subject: string
  unit: string
  processing: Processing
  replyQueue: ReplyQueue | null
  replyService: EffectiveReplyService
  replySummary?: ReplySummary | null
  reception?: Reception | null
  revisits?: RevisitSummary | null
  fresh: boolean
  /** 仅在明确需要展示「看不清」时（如聊天页、或跟读/曾读过状态但已过期）输出未知提示 */
  showUnknown?: boolean
}

export function emptyConversationStatus(): ConversationStatusView {
  return {
    headline: '',
    tone: 'muted',
    detail: '',
    receptionNote: '',
    revisitNote: '',
    serviceNote: '',
  }
}

// 只有服务器在有效期内给出的排队、运行、停止中才算「有事在发生」。
export function inFlight(processing: Processing) {
  return processing === 'queued' || processing === 'running' || processing === 'stopping'
}

// 把会话的处理状态说成人话；subject 是「这封信」或「这段聊天」。空闲时不说话。
export function describeProcessing(processing: Processing, subject: string) {
  switch (processing) {
    case 'running':
      return `米洛正在读${subject}。`
    case 'queued':
      return `${subject}排上队了，等米洛来读。`
    case 'stopping':
      return '这次读信正在停下，还没确认结束。'
    case 'idle':
      return ''
    default:
      return `${subject}现在的处理情况看不清。`
  }
}

const ACTIVE_WAIT_REASONS = new Set<ReplyQueue['reason']>([
  'waiting',
  'retry_wait',
  'execution_busy',
  'processing',
  'stopping',
  'automatic_unknown',
])

// 本会话里是否还有可继续进入后续处理的待回消息（不含纯阻断条目）。
export function hasActionablePending(queue: ReplyQueue | null) {
  if (!queue || queue.clock_invalid || queue.pending_count <= 0) return false
  return queue.available_count + queue.reserved_count + queue.retry_wait_count > 0
}

// 仅在存在真实可处理待回、或有已到点待续的回访且持续服务证据有效时，才允许有限等待自动回信；
// 对未来才到点的暂缓/回访、被阻挡回访与待核对状态，绝不永久轮询。
export function shouldWaitForScheduled(
  queue: ReplyQueue | null,
  service: EffectiveReplyService,
  revisits?: RevisitSummary | null,
) {
  if (service.state !== 'scheduled') return false
  if (revisits && revisits.ready_count > 0) return true
  if (!queue || !hasActionablePending(queue)) return false
  return ACTIVE_WAIT_REASONS.has(queue.reason)
}

function countText(count: number, unit: string) {
  return count > 1 ? `这 ${count} ${unit}` : `这 1 ${unit}`
}

function describeMixedCounts(queue: ReplyQueue, unit: string) {
  const parts: string[] = []
  const queueable = queue.available_count + queue.retry_wait_count
  if (queue.reserved_count > 0 && queue.blocked_count > 0) {
    parts.push(`${queue.reserved_count} ${unit}正在处理或待核对`)
  }
  if (queueable > 0 && queue.blocked_count > 0) {
    parts.push(`${queueable} ${unit}仍可排队等待处理`)
  }
  if (queue.blocked_count > 0 && (queueable > 0 || queue.reserved_count > 0)) {
    parts.push(`${queue.blocked_count} ${unit}因先前阻断需要恢复（不影响新留言）`)
  }
  if (queue.retry_wait_count > 0 && queue.retry_after) {
    parts.push(`最早可在 ${clockTime(queue.retry_after)} 后再次尝试`)
  }
  return parts.length ? `${parts.join('；')}。` : ''
}

function describeServiceNote(
  service: EffectiveReplyService,
  queue: ReplyQueue | null,
  processing: Processing,
  revisits: RevisitSummary | null | undefined,
): string {
  const hasActionable = hasActionablePending(queue) || (revisits?.ready_count ?? 0) > 0
  const hasPending = (queue?.pending_count ?? 0) > 0 || (revisits?.ready_count ?? 0) > 0
  if (service.state === 'scheduled') {
    return hasActionable || inFlight(processing)
      ? '自动读信已安排，近期检查有效（不代表在线或已读）。'
      : ''
  }
  if (service.state === 'disabled') {
    return hasPending ? '自动回信服务当前已停用，留言会一直安全存着。' : ''
  }
  if (service.state === 'faulted') {
    return '自动回信服务当前有故障，需要恢复后再继续；留言不会丢失，也不代表米洛拒绝回复。'
  }
  if (service.reason === 'observation_expired' && hasPending) {
    return '自动服务的上次检查已过期，目前看不清是否仍在运行。'
  }
  if (service.reason === 'clock_or_boot_invalid' && hasPending) {
    return '自动服务的时间状态暂不可靠，目前看不清。'
  }
  if (queue?.reason === 'waiting' && hasActionable) {
    return '尚不能确认持续自动服务是否已安排。'
  }
  return ''
}

// 统一将原 processing、本会话 reply_queue 事实、真实决定/回访/接收安排与独立 reply_service 证据翻译成温和的人话。
// 绝不把缺失写成 0 或停用，不把未知/作息冒充在线，也不把暂缓或无需回复画成故障。
export function describeConversationStatus(input: ConversationStatusInput): ConversationStatusView {
  const {
    subject,
    unit,
    processing,
    replyQueue,
    replyService,
    replySummary = null,
    reception = null,
    revisits = null,
    fresh,
    showUnknown = false,
  } = input

  // 过期或读取失败：不保留旧排队计数、旧接收安排或旧“已安排”结论，统一退回看不清。
  if (!fresh) {
    if (!showUnknown) return emptyConversationStatus()
    return {
      ...emptyConversationStatus(),
      headline: describeProcessing('unknown', subject),
    }
  }

  const receptionNote = describeReception(reception, 'conversation')
  const revisitNote = describeRevisitSummary(revisits, reception)

  // 旧后端缺字段兼容路径：只按原 processing 展示，不猜排队计数、新决定或服务状态。
  if (!replyQueue) {
    const line =
      processing === 'idle' && !showUnknown ? '' : describeProcessing(processing, subject)
    const serviceNote = describeServiceNote(replyService, null, processing, revisits)
    return {
      headline: line,
      tone: inFlight(processing)
        ? 'live'
        : serviceNote && replyService.state === 'faulted'
          ? 'warn'
          : 'muted',
      detail: '',
      receptionNote,
      revisitNote,
      serviceNote,
    }
  }

  const serviceNote = describeServiceNote(replyService, replyQueue, processing, revisits)
  const detail = describeMixedCounts(replyQueue, unit)
  const count = countText(replyQueue.pending_count, unit)
  const hasPartialBlocked =
    replyQueue.blocked_count > 0 &&
    replyQueue.available_count + replyQueue.reserved_count + replyQueue.retry_wait_count > 0

  const build = (
    headline: string,
    tone: ConversationStatusView['tone'],
    extra: Partial<Pick<ConversationStatusView, 'detail' | 'revisitNote'>> = {},
  ): ConversationStatusView => ({
    headline,
    tone,
    detail: extra.detail ?? detail,
    receptionNote,
    revisitNote: extra.revisitNote ?? revisitNote,
    serviceNote,
  })

  // 正在处理或停止优先于安静等待。
  if (replyQueue.reason === 'stopping' || processing === 'stopping') {
    return build(describeProcessing('stopping', subject), 'live')
  }
  if (replyQueue.reason === 'processing' || inFlight(processing)) {
    return build(describeProcessing(inFlight(processing) ? processing : 'queued', subject), 'live')
  }

  if (replyQueue.clock_invalid || replyQueue.reason === 'clock_invalid') {
    return build(`${subject}已保存，但时间记录异常，自动处理情况目前看不清。`, 'muted')
  }

  const waitingHeadline = {
    resting: `米洛现在在休息，${count}已经存好，不会吵醒他。`,
    paused: `安排目前暂停中，${count}已经存好。`,
    winding_down: `米洛正在收尾，${count}已经存好，等下次读信时再看。`,
    schedule_unconfigured: `作息安排还没定下来，${count}已经存好。`,
    execution_busy: `${count}已经存好，正在等当前手头的工作结束。`,
    retry_wait: `${count}已经存好，正在等待原规则允许的下一次尝试。`,
  } as const

  switch (replyQueue.reason) {
    case 'nothing_pending': {
      if (revisits && revisits.reconciliation_count > 0) {
        return build(
          `有 ${revisits.reconciliation_count} 次回访的处理情况尚待核对，暂时不能确认是否结束。`,
          'warn',
          { detail: '', revisitNote: '' },
        )
      }
      if (revisits && revisits.ready_count > 0) {
        return build(
          '有到点的回访正等着米洛续聊。',
          replyService.state === 'faulted' ? 'warn' : 'waiting',
          { detail: '', revisitNote: '' },
        )
      }
      return build('', replyService.state === 'faulted' ? 'warn' : 'muted', { detail: '' })
    }
    case 'needs_reconciliation': {
      const headline =
        replyQueue.pending_count > 0
          ? `${count}的处理情况尚待核对，暂时不能确认是否结束。`
          : revisits && revisits.reconciliation_count > 0
            ? `有 ${revisits.reconciliation_count} 次回访的处理情况尚待核对，暂时不能确认是否结束。`
            : `${subject}的处理情况尚待核对，暂时不能确认是否结束。`
      return build(headline, 'warn', {
        revisitNote: replyQueue.pending_count > 0 ? revisitNote : '',
      })
    }
    case 'blocked':
      return build(`${count}尚未回复，这次处理遇到阻断，需要恢复。`, 'warn')
    case 'resting':
    case 'paused':
    case 'winding_down':
    case 'schedule_unconfigured':
    case 'execution_busy':
    case 'retry_wait':
      return build(waitingHeadline[replyQueue.reason], hasPartialBlocked ? 'warn' : 'waiting')
    case 'waiting': {
      // 若全会话汇总显示没有未读新留言，而是米洛明确留作暂缓回访，就按真实决定温和说明，不冒充“未处理排队”。
      if (replySummary && replySummary.unprocessed_count === 0) {
        if (replySummary.deferred_count > 0) {
          const deferredText = countText(replySummary.deferred_count, unit)
          if (revisits && revisits.blocked_count > 0) {
            return build(`${deferredText}已到原定回访时间，目前暂缓继续。`, 'waiting')
          }
          const due = revisits?.next_due_at
            ? `，计划 ${stampTime(revisits.next_due_at)} 左右回访`
            : ''
          return build(`米洛留着${deferredText}稍后再看${due}。`, 'muted', { revisitNote: '' })
        }
        if (replySummary.unknown_count > 0) {
          const unknownText = countText(replySummary.unknown_count, unit)
          return build(`${unknownText}是更早的记录，缺少当时的回复决定。`, 'muted')
        }
      }
      return build(`${count}已经存好，正在等待处理。`, hasPartialBlocked ? 'warn' : 'waiting')
    }
    case 'automatic_unknown':
    default: {
      if (replyService.state === 'scheduled') {
        return build(
          `${count}已经存好，等待下一次自动检查。`,
          hasPartialBlocked ? 'warn' : 'waiting',
        )
      }
      if (replyService.state === 'disabled') {
        return build(
          `${count}已经存好，自动回信目前没有开启。`,
          hasPartialBlocked ? 'warn' : 'muted',
        )
      }
      if (replyService.state === 'faulted') {
        return build(`${count}已经存好，自动回信目前遇到故障。`, 'warn')
      }
      return build(
        `${count}已保存，但自动处理情况目前看不清。`,
        hasPartialBlocked ? 'warn' : 'muted',
      )
    }
  }
}
