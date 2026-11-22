import { computed, onBeforeUnmount, ref } from 'vue'
import { api } from '../api/client'
import type {
  Activity,
  Reception,
  ReplyQueue,
  ReplyService,
  ReplySummary,
  RevisitSummary,
} from '../api/client'
import { resolveReceptionLifetime } from '../utils/noncompulsory'
import { usePrivateSession } from './usePrivateSession'
import { usePrivateScope } from './usePrivateScope'

export type EffectiveReplyService = {
  state: ReplyService['state']
  reason: ReplyService['reason']
}

// 解析持续服务独立证据：缺失、已过期或时间自相矛盾一律降为 unknown，不靠顶层健康推断正常。
function resolveServiceLifetime(next: Activity): {
  service: EffectiveReplyService
  lifeMs: number
} {
  const raw = next.reply_service
  if (!raw) {
    return {
      service: { state: 'unknown', reason: 'observation_missing' },
      lifeMs: 0,
    }
  }
  if (raw.state === 'unknown') {
    return {
      service: { state: 'unknown', reason: raw.reason },
      lifeMs: 0,
    }
  }
  const observedAt = raw.observed_at
  const validUntil = raw.valid_until
  if (
    typeof observedAt !== 'number' ||
    typeof validUntil !== 'number' ||
    validUntil <= observedAt ||
    observedAt > next.observed_at
  ) {
    return {
      service: { state: 'unknown', reason: 'clock_or_boot_invalid' },
      lifeMs: 0,
    }
  }
  if (validUntil <= next.observed_at) {
    return {
      service: { state: 'unknown', reason: 'observation_expired' },
      lifeMs: 0,
    }
  }
  return {
    service: { state: raw.state, reason: raw.reason },
    lifeMs: Math.max(0, validUntil - next.observed_at) * 1000,
  }
}

// 会话活动：只按服务器给的有效期展示，到期立刻降为「看不清」。
// 有效期按请求发出时刻起算的本地单调时钟倒数，不拿浏览器墙钟去比服务器秒数。
export function useActivity(conversationId: () => string | null) {
  const session = usePrivateSession()
  const scope = usePrivateScope(conversationId)
  const activity = ref<Activity | null>(null)
  const expired = ref(true)
  const serviceExpired = ref(true)
  const receptionExpired = ref(true)
  const serviceSnapshot = ref<EffectiveReplyService>({
    state: 'unknown',
    reason: 'observation_missing',
  })
  const receptionSnapshot = ref<Reception | null>(null)
  const attempted = ref(false)
  let timer: ReturnType<typeof setTimeout> | undefined
  let serviceTimer: ReturnType<typeof setTimeout> | undefined
  let receptionTimer: ReturnType<typeof setTimeout> | undefined

  function clearTimers() {
    clearTimeout(timer)
    clearTimeout(serviceTimer)
    clearTimeout(receptionTimer)
    timer = undefined
    serviceTimer = undefined
    receptionTimer = undefined
  }

  function forget() {
    scope.invalidate()
    clearTimers()
    activity.value = null
    expired.value = true
    serviceExpired.value = true
    receptionExpired.value = true
    serviceSnapshot.value = { state: 'unknown', reason: 'observation_missing' }
    receptionSnapshot.value = null
    attempted.value = false
  }

  async function read() {
    const id = conversationId()
    if (!id) return false
    scope.invalidate()
    const active = scope.capture()
    if (!active()) return false
    const started = performance.now()
    const own = session.signal()
    let next: Activity
    try {
      next = await api.activity(id, own)
    } catch (error) {
      if (!active()) return false
      throw error
    }
    if (!active()) return false
    clearTimers()
    attempted.value = true
    activity.value = next

    const life = Math.max(0, next.valid_until - next.observed_at) * 1000
    const remaining = Math.max(0, started + life - performance.now())
    expired.value = remaining === 0
    if (remaining > 0) {
      timer = setTimeout(() => {
        expired.value = true
      }, remaining)
    }

    const resolved = resolveServiceLifetime(next)
    serviceSnapshot.value = resolved.service
    if (resolved.service.state !== 'unknown') {
      const serviceRemaining = Math.max(0, started + resolved.lifeMs - performance.now())
      serviceExpired.value = serviceRemaining === 0
      if (serviceRemaining > 0) {
        serviceTimer = setTimeout(() => {
          serviceExpired.value = true
        }, serviceRemaining)
      }
    } else {
      serviceExpired.value = false
    }

    const resolvedReception = resolveReceptionLifetime(next.reception, next.observed_at)
    receptionSnapshot.value = resolvedReception.reception
    if (resolvedReception.reception) {
      const receptionRemaining = Math.max(0, started + resolvedReception.lifeMs - performance.now())
      receptionExpired.value = receptionRemaining === 0
      if (receptionRemaining > 0) {
        receptionTimer = setTimeout(() => {
          receptionExpired.value = true
        }, receptionRemaining)
      }
    } else {
      receptionExpired.value = true
    }
    return true
  }

  // 读取失败时保留最近回信这一历史事实，处理状态、排队结论、接收安排与服务证据全部降为未知。
  function lose() {
    clearTimers()
    expired.value = true
    serviceExpired.value = true
    receptionExpired.value = true
    attempted.value = true
  }

  const fresh = computed(() => !!activity.value && !expired.value)
  const processing = computed<Activity['processing']>(() =>
    !fresh.value ? 'unknown' : activity.value!.processing,
  )
  const latestReply = computed(() => activity.value?.latest_reply_at ?? null)
  const replyQueue = computed<ReplyQueue | null>(() =>
    fresh.value ? (activity.value?.reply_queue ?? null) : null,
  )
  const replySummary = computed<ReplySummary | null>(() =>
    fresh.value ? (activity.value?.reply_summary ?? null) : null,
  )
  const reception = computed<Reception | null>(() =>
    fresh.value && !receptionExpired.value ? receptionSnapshot.value : null,
  )
  const revisits = computed<RevisitSummary | null>(() =>
    fresh.value ? (activity.value?.revisits ?? null) : null,
  )
  const replyService = computed<EffectiveReplyService>(() => {
    if (!fresh.value) {
      return {
        state: 'unknown',
        reason: activity.value ? 'observation_expired' : 'observation_missing',
      }
    }
    if (serviceSnapshot.value.state !== 'unknown' && serviceExpired.value) {
      return { state: 'unknown', reason: 'observation_expired' }
    }
    return serviceSnapshot.value
  })

  onBeforeUnmount(forget)
  return {
    processing,
    latestReply,
    replyQueue,
    replyService,
    replySummary,
    reception,
    revisits,
    fresh,
    attempted,
    read,
    lose,
    forget,
  }
}
