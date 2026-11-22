import { computed, ref } from 'vue'
import { api, ApiFailure, explain } from '../api/client'
import type { WorkEvent, WorkInput, WorkResult } from '../api/client'
import { createWorkReadJob } from '../utils/workReadJob'

export type StampedEvent = WorkEvent & { fresh: boolean }

interface WorkRecordsOptions {
  taskId: () => string | null
  capture: () => () => boolean
  signal: () => AbortSignal
  onAccessError: (error: unknown, own: AbortSignal) => void
  onChanged: () => void
}

// 原要求之外的历史独立于当前任务状态读取，失败时保留上次快照。
export function useWorkRecords(options: WorkRecordsOptions) {
  const inputs = ref<WorkInput[]>([])
  const results = ref<WorkResult[]>([])
  const events = ref<StampedEvent[]>([])
  const eventsLoading = ref(false)
  const historyLoading = ref(false)
  const eventsProblem = ref('')
  const historyProblem = ref('')
  let cursor = 0
  let opened = false
  let seen = new Set<number>()

  function failed(error: unknown, own: AbortSignal) {
    if (error instanceof ApiFailure && (error.status === 401 || error.status === 404)) {
      options.onAccessError(error, own)
    }
    return explain(error)
  }

  const eventJob = createWorkReadJob(async () => {
    const taskId = options.taskId()
    if (!taskId) return
    const current = options.capture()
    const own = options.signal()
    const active = () => current() && !own.aborted
    eventsLoading.value = true
    try {
      let retried = false
      let added = false
      for (let page = 0; page < 40; page += 1) {
        let result
        try {
          result = await api.workEvents(taskId, cursor, own)
        } catch (error) {
          if (!active()) return
          if (error instanceof ApiFailure && error.code === 'work_cursor_ahead' && !retried) {
            events.value = []
            seen = new Set()
            cursor = 0
            retried = true
            continue
          }
          throw error
        }
        if (!active()) return
        for (const event of result.events) {
          if (seen.has(event.sequence)) continue
          if (!added) for (const earlier of events.value) earlier.fresh = false
          seen.add(event.sequence)
          events.value.push({ ...event, fresh: opened })
          added = true
        }
        cursor = Math.max(cursor, result.next_after)
        if (!result.has_more) break
      }
      if (active()) {
        if (opened && added) options.onChanged()
        opened = true
        eventsProblem.value = ''
      }
    } catch (error) {
      if (active()) {
        const message = failed(error, own)
        if (active()) eventsProblem.value = message
      }
    } finally {
      if (active()) eventsLoading.value = false
    }
  })

  const historyJob = createWorkReadJob(async () => {
    const taskId = options.taskId()
    if (!taskId) return
    const current = options.capture()
    const own = options.signal()
    const active = () => current() && !own.aborted
    historyLoading.value = true
    try {
      async function checked<T>(request: Promise<T>) {
        try {
          return await request
        } catch (error) {
          if (
            active() &&
            error instanceof ApiFailure &&
            (error.status === 401 || error.status === 404)
          )
            options.onAccessError(error, own)
          if (active()) historyProblem.value = explain(error)
          throw error
        }
      }
      async function readInputs() {
        const collected: WorkInput[] = []
        let after = 0
        for (let page = 0; page < 3; page += 1) {
          const result = await checked(api.workInputs(taskId!, after, own))
          if (!active()) return collected
          collected.push(...result.inputs)
          if (result.inputs.length < 50 || result.next_after <= after) break
          after = result.next_after
        }
        return collected
      }
      // 一个分支失败时也等另一支收尾，避免重试复用槽过早释放后重复拉历史。
      const [inputRead, resultRead] = await Promise.allSettled([
        readInputs(),
        checked(api.workResults(taskId, own)),
      ])
      if (!active()) return
      if (inputRead.status === 'rejected') throw inputRead.reason
      if (resultRead.status === 'rejected') throw resultRead.reason
      inputs.value = inputRead.value
      results.value = resultRead.value.results
      historyProblem.value = ''
    } catch (error) {
      if (active()) {
        const message = failed(error, own)
        if (active()) historyProblem.value = message
      }
    } finally {
      if (active()) historyLoading.value = false
    }
  })

  function read(fresh = false) {
    return Promise.all([eventJob.run(fresh), historyJob.run(fresh)])
  }

  function reset() {
    eventJob.reset()
    historyJob.reset()
    inputs.value = []
    results.value = []
    events.value = []
    eventsLoading.value = false
    historyLoading.value = false
    eventsProblem.value = ''
    historyProblem.value = ''
    cursor = 0
    opened = false
    seen = new Set()
  }

  return {
    inputs,
    results,
    events,
    loading: computed(() => eventsLoading.value || historyLoading.value),
    problem: computed(() => [eventsProblem.value, historyProblem.value].filter(Boolean).join('；')),
    read,
    readEvents: eventJob.run,
    reset,
  }
}
