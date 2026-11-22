import { onBeforeUnmount, onMounted, ref } from 'vue'
import { api, explain } from '../api/client'
import type { Schedule } from '../api/client'

// 一分钟内读过的快照换页时直接用，不在每页都闪一次「看一眼」；更久的重新读。
const FRESH = 60_000
let recent: { schedule: Schedule; at: number } | null = null

// 只负责读取公开作息快照；刷新时保留上次快照供参考并标注更新时间，避免清空导致下方跳动。
export function useSchedule() {
  const fresh = recent && performance.now() - recent.at < FRESH ? recent.schedule : null
  const schedule = ref<Schedule | null>(fresh)
  const loading = ref(!fresh)
  const problem = ref('')
  const refreshProblem = ref('')
  let controller = new AbortController()

  async function load() {
    controller.abort()
    controller = new AbortController()
    const own = controller
    loading.value = true
    problem.value = ''
    refreshProblem.value = ''
    try {
      const result = await api.schedule(own.signal)
      if (!own.signal.aborted) {
        schedule.value = result
        recent = { schedule: result, at: performance.now() }
      }
    } catch (error) {
      if (!own.signal.aborted) {
        const message = explain(error)
        if (schedule.value) {
          refreshProblem.value = `这次没看清最新作息（${message}），下面暂留上次记录。`
        } else {
          problem.value = message
          recent = null
        }
      }
    } finally {
      if (!own.signal.aborted) loading.value = false
    }
  }

  onMounted(() => {
    if (!fresh) void load()
  })
  onBeforeUnmount(() => controller.abort())

  return {
    schedule,
    loading,
    problem,
    refreshProblem,
    load,
  }
}
