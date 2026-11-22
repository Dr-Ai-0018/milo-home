import { ref } from 'vue'
import { api, ApiFailure, explain } from '../api/client'
import type { Run } from '../api/client'

interface RunHistoryOptions {
  threadId: () => string | null
  signal: () => AbortSignal
  onUnauthorized: (error: unknown, own: AbortSignal) => void
}

interface PendingRunRead {
  id: string
  own: AbortSignal
  promise: Promise<void>
}

// 读信执行记录只是信箱的辅助信息，不决定目录是否读到或是否可以写信。
export function useRunHistory(options: RunHistoryOptions) {
  const runs = ref<Run[]>([])
  const known = ref(false)
  const loading = ref(false)
  const problem = ref('')
  let version = 0
  let pending: PendingRunRead | null = null

  function reset() {
    version += 1
    pending = null
    runs.value = []
    known.value = false
    loading.value = false
    problem.value = ''
  }

  function read(): Promise<void> {
    const id = options.threadId()
    if (!id) return Promise.resolve()
    const own = options.signal()
    if (own.aborted) return Promise.resolve()
    if (pending?.id === id && pending.own === own) return pending.promise
    const mine = ++version
    const active = () => !own.aborted && mine === version && id === options.threadId()
    loading.value = true
    const promise = api
      .runs(id, own)
      .then((value) => {
        if (!active()) return
        runs.value = value
        known.value = true
        problem.value = ''
      })
      .catch((error: unknown) => {
        if (!active()) return
        if (error instanceof ApiFailure && error.status === 401) {
          options.onUnauthorized(error, own)
          return
        }
        problem.value = explain(error)
      })
      .finally(() => {
        if (active()) loading.value = false
        if (pending?.promise === promise) pending = null
      })
    pending = {
      id,
      own,
      promise,
    }
    return promise
  }

  return {
    runs,
    known,
    loading,
    problem,
    read,
    reset,
  }
}
