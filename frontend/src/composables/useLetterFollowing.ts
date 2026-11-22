import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { Ref } from 'vue'
import { ApiFailure } from '../api/client'
import {
  describeConversationStatus,
  emptyConversationStatus,
  inFlight,
  shouldWaitForScheduled,
} from '../utils/activity'
import type { ConversationStatusView } from '../utils/activity'
import { useActivity } from './useActivity'
import { useChatPolling } from './useChatPolling'
import { usePrivateScope } from './usePrivateScope'
import { usePrivateSession } from './usePrivateSession'

// 仅在有有效自动服务安排但尚未真正开跑时，最多有限等待 15 轮（约 5 分钟），绝不永久轮询。
const MAX_SCHEDULED_WAIT_TICKS = 15

// 书信只在确认有处理、或有真实待回/到点回访且持续服务证据有效时有限跟读；读完、停用、阻断、暂缓或休息后收口。
export function useLetterFollowing(
  letterId: () => string | null,
  catchUp: () => Promise<void>,
  behind: Ref<boolean>,
) {
  const session = usePrivateSession()
  const scope = usePrivateScope(letterId)
  const {
    processing,
    replyQueue,
    replyService,
    replySummary,
    reception,
    revisits,
    fresh,
    read,
    lose,
    forget,
  } = useActivity(letterId)
  const following = ref(false)
  const checking = ref(false)
  const hadNotableState = ref(false)
  const readFailed = ref(false)
  let deferred = false
  let idleWaitTicks = 0

  function recordObservation() {
    readFailed.value = false
    const queue = replyQueue.value
    const service = replyService.value
    const rev = revisits.value
    const rec = reception.value
    const hasRevisitSignal =
      !!rev &&
      (rev.pending_count > 0 ||
        rev.ready_count > 0 ||
        rev.active_count > 0 ||
        rev.blocked_count > 0 ||
        rev.reconciliation_count > 0)
    const hasReceptionSignal = !!rec && rec.mode !== 'available'
    if (
      inFlight(processing.value) ||
      (queue !== null && queue.pending_count > 0) ||
      hasRevisitSignal ||
      hasReceptionSignal ||
      service.state === 'faulted' ||
      service.state === 'disabled'
    ) {
      hadNotableState.value = true
    } else if (queue?.reason === 'nothing_pending' && processing.value === 'idle') {
      hadNotableState.value = false
    }
  }

  function needsFollowing() {
    if (inFlight(processing.value)) return true
    return shouldWaitForScheduled(replyQueue.value, replyService.value, revisits.value)
  }

  async function tick() {
    const active = scope.capture()
    const own = session.signal()
    if (!active() || document.hidden) return
    try {
      await catchUp()
      if (!active() || document.hidden) return
      if (!(await read()) || !active() || document.hidden) return
      recordObservation()

      if (inFlight(processing.value)) {
        idleWaitTicks = 0
        return
      }
      if (shouldWaitForScheduled(replyQueue.value, replyService.value, revisits.value)) {
        idleWaitTicks += 1
        if (idleWaitTicks <= MAX_SCHEDULED_WAIT_TICKS) return
      } else if (processing.value === 'unknown' && !replyQueue.value) {
        // 未知不是完成证据；有限轮次内继续核对，避免无限轮询。
        idleWaitTicks += 1
        if (idleWaitTicks <= MAX_SCHEDULED_WAIT_TICKS) return
      }

      await catchUp()
      if (!active() || document.hidden || behind.value) return
    } catch (error) {
      if (!active()) return
      lose()
      readFailed.value = true
      if (error instanceof ApiFailure && error.status === 401) {
        reset()
        session.fail(error, own)
        return
      }
      throw error
    }
    following.value = false
    idleWaitTicks = 0
    polling.stop()
    await session.revalidate()
  }

  const polling = useChatPolling(tick, () => session.verify())

  function reset() {
    scope.invalidate()
    deferred = false
    following.value = false
    checking.value = false
    hadNotableState.value = false
    readFailed.value = false
    idleWaitTicks = 0
    polling.stop()
    forget()
  }

  // 打开、手动重读或寄出后查一次，空闲且无有效自动等待时不建立周期请求。
  async function check() {
    const active = scope.capture()
    const own = session.signal()
    if (!active() || !letterId()) return
    if (document.hidden) {
      deferred = true
      return
    }
    deferred = false
    checking.value = true
    try {
      if (!(await read()) || !active()) return
      recordObservation()
    } catch (error) {
      if (!active()) return
      lose()
      readFailed.value = true
      if (error instanceof ApiFailure && error.status === 401) session.fail(error, own)
      return
    } finally {
      if (active()) checking.value = false
    }
    if (document.hidden) {
      deferred = true
      return
    }
    if (needsFollowing()) {
      if (!following.value) {
        idleWaitTicks = 0
        following.value = true
        polling.start()
      }
    } else if (following.value) {
      following.value = false
      idleWaitTicks = 0
      polling.stop()
    }
  }

  async function visibility() {
    if (document.hidden || !deferred) return
    if (following.value) {
      deferred = false
      return
    }
    const active = scope.capture()
    if ((await session.verify()) && active() && !document.hidden) await check()
  }

  document.addEventListener('visibilitychange', visibility)
  watch([letterId, () => session.user.value?.id], reset, { flush: 'sync' })
  onBeforeUnmount(() => {
    document.removeEventListener('visibilitychange', visibility)
    reset()
  })

  // 跟读断线时只说连接，不再叠一句「看不清」。
  const offline = computed(() => following.value && polling.failures.value > 0)
  const status = computed<ConversationStatusView>(() => {
    if (offline.value) return emptyConversationStatus()
    return describeConversationStatus({
      subject: '这封信',
      unit: '封信',
      processing: processing.value,
      replyQueue: replyQueue.value,
      replyService: replyService.value,
      replySummary: replySummary.value,
      reception: reception.value,
      revisits: revisits.value,
      fresh: fresh.value,
      showUnknown: following.value || hadNotableState.value || readFailed.value,
    })
  })
  const connectionLine = computed(() =>
    offline.value ? '暂时连不上，会隔一会儿自动再试，也可以点「重新读取」。' : '',
  )
  const canRecheck = computed(
    () =>
      !offline.value &&
      !inFlight(processing.value) &&
      ((!fresh.value && (hadNotableState.value || readFailed.value)) ||
        replyService.value.state === 'faulted' ||
        replyService.value.reason === 'observation_expired'),
  )

  return {
    processing,
    replyQueue,
    replyService,
    replySummary,
    reception,
    revisits,
    status,
    connectionLine,
    checking,
    canRecheck,
    check,
  }
}
