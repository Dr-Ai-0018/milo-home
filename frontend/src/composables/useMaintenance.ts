import { computed, ref } from 'vue'
import { api, ApiFailure, explain } from '../api/client'
import type { Maintenance } from '../api/client'
import { buildMaintenanceSummary, resolveMaintenanceLifetimeMs } from '../utils/maintenance'
import { usePrivateSession } from './usePrivateSession'

// 本人只读维护摘要状态：纯按需读取与手动重读，绝不持续轮询；
// 404 静默视为旧后端/未配置；503 保留上次值并明确提示未核对；退出/换号/撤权立即清零并作废在途响应。
const session = usePrivateSession()
const snapshot = ref<Maintenance | null>(null)
const clockNow = ref(Math.floor(Date.now() / 1000))
const expiredLocally = ref(false)
const supported = ref(true)
const forbidden = ref(false)
const loadedOnce = ref(false)
const loading = ref(false)
const problem = ref('')

let controller: AbortController | null = null
let expiryTimer: ReturnType<typeof setTimeout> | null = null
let ticket = 0
let receivedAt = 0
let receivedClock = 0

function clearExpiryTimer() {
  if (expiryTimer !== null) {
    clearTimeout(expiryTimer)
    expiryTimer = null
  }
}

function reset() {
  controller?.abort()
  controller = null
  clearExpiryTimer()
  ticket += 1
  snapshot.value = null
  expiredLocally.value = false
  supported.value = true
  forbidden.value = false
  loadedOnce.value = false
  loading.value = false
  problem.value = ''
  receivedAt = 0
  receivedClock = 0
}

session.onClear(reset)

function effectiveNow() {
  const wall = Date.now() / 1000
  if (snapshot.value && wall < snapshot.value.observed_at) return wall
  return Math.max(wall, receivedClock + (performance.now() - receivedAt) / 1000)
}

function armExpiry(view: Maintenance) {
  clearExpiryTimer()
  const nowSec = effectiveNow()
  clockNow.value = nowSec
  const lifeMs = resolveMaintenanceLifetimeMs(view, nowSec)
  if (lifeMs <= 0) {
    expiredLocally.value = true
    return
  }
  expiredLocally.value = false
  const deadlines = (view.backups ?? [])
    .filter((item) => item.state === 'normal' && item.fresh_until && item.fresh_until > nowSec)
    .map((item) => (item.fresh_until! - nowSec) * 1000)
  // 整体观察和各项备份证据分别到期；单调计时保证调慢系统时钟不会延长有效期。
  // 到期只在本地改状态，不发网络请求。
  const waitMs = Math.min(lifeMs, ...deadlines, 24 * 60 * 60 * 1000)
  expiryTimer = setTimeout(() => {
    expiryTimer = null
    if (snapshot.value) armExpiry(snapshot.value)
  }, Math.ceil(waitMs))
}

async function fetchMaintenance(verifyFirst: boolean) {
  if (!session.user.value) return
  const own = session.signal()
  const reqController = new AbortController()
  controller?.abort()
  controller = reqController
  const signal = AbortSignal.any([own, reqController.signal])
  const myTicket = ++ticket
  const current = () => !own.aborted && !reqController.signal.aborted && myTicket === ticket

  loading.value = true
  problem.value = ''
  try {
    if (verifyFirst) {
      const ok = await session.verify()
      if (!ok || !current()) return
    }
    const data = await api.maintenance(signal)
    if (!current()) return
    supported.value = true
    forbidden.value = false
    loadedOnce.value = true
    snapshot.value = data
    receivedAt = performance.now()
    receivedClock = Date.now() / 1000
    problem.value = ''
    armExpiry(data)
  } catch (error) {
    if (!current()) return
    if (error instanceof ApiFailure && error.status === 401) {
      session.fail(error, own)
      return
    }
    if (error instanceof ApiFailure && error.status === 403) {
      clearExpiryTimer()
      snapshot.value = null
      expiredLocally.value = false
      forbidden.value = true
      loadedOnce.value = true
      problem.value = ''
      return
    }
    if (error instanceof ApiFailure && error.status === 404) {
      clearExpiryTimer()
      snapshot.value = null
      expiredLocally.value = false
      supported.value = false
      loadedOnce.value = true
      problem.value = ''
      return
    }
    // 503 或网络中断：保留上次成功值并明确标注本次未核对；若此前已过期仍按过期降级。
    clockNow.value = effectiveNow()
    if (snapshot.value && resolveMaintenanceLifetimeMs(snapshot.value, clockNow.value) <= 0) {
      expiredLocally.value = true
    }
    loadedOnce.value = true
    problem.value = snapshot.value
      ? `这次未能核对最新维护摘要（${explain(error)}），下方保留上次读到的记录。`
      : explain(error)
  } finally {
    if (controller === reqController) controller = null
    if (myTicket === ticket) loading.value = false
  }
}

const summary = computed(() =>
  snapshot.value
    ? buildMaintenanceSummary(snapshot.value, clockNow.value, expiredLocally.value)
    : null,
)

const visible = computed(
  () =>
    !!session.user.value &&
    supported.value &&
    !forbidden.value &&
    (loading.value || !!summary.value || !!problem.value),
)

export function useMaintenance() {
  return {
    summary,
    visible,
    loading,
    problem,
    loadedOnce,
    load: () => fetchMaintenance(false),
    reload: () => fetchMaintenance(true),
    reset,
  }
}
