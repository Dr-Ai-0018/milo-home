import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api, ApiFailure, explain } from '../api/client'
import type { WorkTask } from '../api/client'
import { isMoving } from '../utils/work'
import { createWorkReadJob } from '../utils/workReadJob'
import { useChatPolling } from './useChatPolling'
import { usePrivateScope } from './usePrivateScope'
import { usePrivateSession } from './usePrivateSession'
import { useWorkDelivery } from './useWorkDelivery'
import { useWorkDesk } from './useWorkDesk'
import { useWorkRecords } from './useWorkRecords'

export type { StampedEvent } from './useWorkRecords'
const MAX_CLOSURE_READS = 4

// 状态决定当前动作，历史和交付区各自读取，不拖住状态与写操作的回执。
export function useWorkTask(id: () => string | null) {
  const session = usePrivateSession()
  const desk = useWorkDesk()
  const scope = usePrivateScope(id)
  const task = ref<WorkTask | null>(null)
  const coreLoading = ref(false)
  const refreshing = ref(false)
  const problem = ref('')
  const gone = ref(false)
  const remainingClosureReads = ref(MAX_CLOSURE_READS)
  let closureWindow = 0
  let closureReadPending = false
  let issued = 0
  let applied = 0
  let taskRequests = new AbortController()

  function signal() {
    return AbortSignal.any([taskRequests.signal, session.signal()])
  }

  function cancelRequests() {
    taskRequests.abort()
    taskRequests = new AbortController()
  }

  watch([id, () => session.user.value?.id], cancelRequests, { flush: 'sync' })
  onBeforeUnmount(() => taskRequests.abort())

  const delivery = useWorkDelivery({
    taskId: id,
    capture: scope.capture,
    signal,
    onTaskGone: markGone,
  })
  const records = useWorkRecords({
    taskId: id,
    capture: scope.capture,
    signal,
    onAccessError: report,
    onChanged: readRelated,
  })

  function resetClosureReads() {
    closureWindow += 1
    closureReadPending = false
    remainingClosureReads.value = MAX_CLOSURE_READS
  }

  function forget() {
    coreJob.reset()
    task.value = null
    coreLoading.value = false
    refreshing.value = false
    records.reset()
    delivery.resetDelivery()
    resetClosureReads()
  }

  function markGone() {
    const taskId = id()
    scope.invalidate()
    cancelRequests()
    gone.value = true
    forget()
    if (taskId) desk.drop(taskId)
    void desk.recheck()
  }

  watch(
    () => session.user.value?.id,
    (now, before) => {
      if (before && now !== before) forget()
    },
    { flush: 'sync' },
  )

  function report(error: unknown, own: AbortSignal) {
    if (own.aborted) return
    if (error instanceof ApiFailure && error.status === 404) markGone()
    else if (error instanceof ApiFailure && error.status === 401) session.fail(error, own)
    else problem.value = explain(error)
  }

  function readRelated(fresh = false) {
    if (!id() || !session.user.value || gone.value) return
    void records.read(fresh)
    return delivery.readAll(fresh)
  }

  const coreJob = createWorkReadJob(async () => {
    const taskId = id()
    if (!taskId || gone.value || !session.user.value) return
    const current = scope.capture()
    const own = signal()
    const active = () => current() && !own.aborted
    const ticket = ++issued
    coreLoading.value = true
    try {
      const latest = await api.workTask(taskId, own)
      if (!active() || ticket < applied) return
      applied = ticket
      const previous = task.value
      task.value = latest
      desk.remember(latest)
      problem.value = ''
      if (
        previous &&
        (previous.updated_at !== latest.updated_at ||
          previous.status !== latest.status ||
          previous.active_turn !== latest.active_turn ||
          previous.decision?.state !== latest.decision?.state ||
          previous.decision?.revision !== latest.decision?.revision)
      ) {
        readRelated()
      }
    } catch (error) {
      if (active()) {
        report(error, own)
        throw error
      }
    } finally {
      if (active()) coreLoading.value = false
    }
  })

  function sync(full: boolean, fresh = false) {
    if (!id() || !session.user.value || gone.value) return Promise.resolve()
    if (full) readRelated(fresh)
    else void records.readEvents()
    return coreJob.run(fresh)
  }

  async function open() {
    scope.invalidate()
    cancelRequests()
    forget()
    gone.value = false
    problem.value = ''
    await sync(true).catch(() => undefined)
  }

  async function refresh() {
    if (gone.value || refreshing.value) return
    const active = scope.capture()
    refreshing.value = true
    try {
      if (!(await session.verify()) || !active()) return
      rearmClosureReads()
      await sync(true)
    } catch {
      // 状态读取错误已写入 problem；历史与交付错误各留在自己的区块。
    } finally {
      if (active()) refreshing.value = false
    }
  }

  function rearmClosureReads() {
    if (remainingClosureReads.value === 0 && awaitingClosure()) resetClosureReads()
  }

  function retryDelivery(read: () => Promise<void>) {
    return async () => {
      if (gone.value) return
      const active = scope.capture()
      await read()
      if (active()) rearmClosureReads()
    }
  }

  function awaitingClosure() {
    const current = task.value
    if (!current || isMoving(current) || current.status === 'needs_reconciliation') return false
    if (delivery.receipt.value?.state === 'pending' || delivery.receiptRetryable.value) return true
    if (current.status !== 'completed') return false
    if (current.decision && current.decision.state !== 'accepted') return false
    if (delivery.attachments.value?.state === 'pending' || delivery.attachmentsRetryable.value) {
      return true
    }
    const legacy = current.deliverable
    return (
      !!legacy &&
      (legacy.state === 'pending' ||
        (legacy.state === 'saved' &&
          !delivery.artifacts.value.some((file) => file.path === legacy.path)))
    )
  }

  const closurePolling = computed(
    () => !gone.value && remainingClosureReads.value > 0 && awaitingClosure(),
  )

  function followClosure(read: Promise<unknown> | undefined, generation: number) {
    if (!read || closureReadPending || gone.value || generation !== closureWindow) return
    const active = scope.capture()
    closureReadPending = true
    void read.then(() => {
      if (!active() || generation !== closureWindow) return
      closureReadPending = false
      if (awaitingClosure()) {
        remainingClosureReads.value = Math.max(0, remainingClosureReads.value - 1)
      }
    })
  }

  const polling = useChatPolling(
    async () => {
      const finishing = awaitingClosure()
      const active = scope.capture()
      const generation = closureWindow
      const read = finishing ? readRelated() : undefined
      await sync(false)
      if (active() && finishing) followClosure(read, generation)
    },
    session.verify,
    4_000,
  )
  watch(
    () => (isMoving(task.value) || closurePolling.value) && !gone.value,
    (moving) => (moving ? polling.start(false) : polling.stop()),
  )

  function afterWrite() {
    // 挡住写入前的状态响应；每条在途通道结束后再取一份写入后的快照。
    applied = ++issued
    void sync(true, true).catch(() => undefined)
  }

  async function append(body: string, key: string, activeTurn: string | null) {
    const taskId = id()
    if (!taskId || gone.value) return null
    const active = scope.capture()
    const own = signal()
    const input = await api.appendWorkInput(taskId, body, key, own, activeTurn).catch((error) => {
      if (active() && error instanceof ApiFailure && error.status === 404) report(error, own)
      if (active() && error instanceof ApiFailure && error.status === 409) afterWrite()
      throw error
    })
    if (!active()) return null
    afterWrite()
    return input
  }

  async function stop(key: string) {
    const taskId = id()
    if (!taskId || gone.value) return null
    const active = scope.capture()
    const own = signal()
    const stopped = await api.stopWorkTask(taskId, key, own).catch((error) => {
      if (active() && error instanceof ApiFailure && error.status === 404) report(error, own)
      throw error
    })
    if (!active()) return null
    task.value = stopped
    desk.remember(stopped)
    afterWrite()
    return stopped
  }

  return {
    task,
    inputs: records.inputs,
    results: records.results,
    events: records.events,
    recordsLoading: records.loading,
    recordsProblem: records.problem,
    refreshRecords: () => records.read(),
    artifacts: delivery.artifacts,
    receipt: delivery.receipt,
    attachments: delivery.attachments,
    loading: computed(() => coreLoading.value || refreshing.value),
    receiptLoading: delivery.receiptLoading,
    attachmentsLoading: delivery.attachmentsLoading,
    problem,
    receiptProblem: delivery.receiptProblem,
    attachmentsProblem: delivery.attachmentsProblem,
    artifactsProblem: delivery.artifactsProblem,
    gone,
    closurePolling,
    failures: polling.failures,
    open,
    refresh,
    refreshReceipt: retryDelivery(delivery.refreshReceipt),
    refreshAttachments: retryDelivery(delivery.refreshAttachments),
    append,
    stop,
    markGone,
    capture: scope.capture,
    signal,
    report: (error: unknown) => report(error, session.signal()),
  }
}
