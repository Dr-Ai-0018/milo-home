import { onBeforeUnmount, ref } from 'vue'

const BASE = 20_000
const LONGEST = 160_000

// 聊天的前台刷新：页面可见时每 20 秒读一次；失败后 40、80、160 秒退避。
// 页面进入后台就停；回到前台先由 resume 确认登录，再立刻读一次。
export function useChatPolling(
  tick: () => Promise<void>,
  resume: () => Promise<boolean>,
  interval = BASE,
) {
  const failures = ref(0)
  const waiting = ref(0)
  let timer: ReturnType<typeof setTimeout> | undefined
  let running = false
  let stopped = true
  let needsVerification = false
  let generation = 0

  function cancel() {
    clearTimeout(timer)
    timer = undefined
  }

  function schedule() {
    cancel()
    if (stopped || document.hidden) return
    waiting.value = failures.value ? Math.min(BASE * 2 ** failures.value, LONGEST) : interval
    timer = setTimeout(() => void now(), waiting.value)
  }

  async function now() {
    if (stopped || running || document.hidden) return
    const own = generation
    cancel()
    running = true
    try {
      if (needsVerification) {
        if (!(await resume())) throw new Error('Identity verification unavailable')
        if (own !== generation || stopped || document.hidden) return
        needsVerification = false
      }
      await tick()
      if (own === generation) failures.value = 0
    } catch {
      if (own === generation) failures.value += 1
    } finally {
      running = false
      schedule()
    }
  }

  async function visibility() {
    needsVerification = true
    if (document.hidden) return cancel()
    if (!stopped) await now()
  }

  function start(immediate = true) {
    if (!stopped) return
    stopped = false
    needsVerification = document.hidden
    failures.value = 0
    document.addEventListener('visibilitychange', visibility)
    if (immediate) void now()
    else schedule()
  }

  function stop() {
    stopped = true
    generation += 1
    cancel()
    document.removeEventListener('visibilitychange', visibility)
  }

  // 手动刷新时清掉退避，马上读。
  function retry() {
    failures.value = 0
    return now()
  }

  onBeforeUnmount(stop)
  return { failures, start, stop, retry }
}
