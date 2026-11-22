import { onBeforeUnmount, ref, watch } from 'vue'
import { api, ApiFailure, explain } from '../api/client'
import type { Knock } from '../api/client'
import { usePrivateScope } from './usePrivateScope'
import { usePrivateSession } from './usePrivateSession'

export type KnockState = 'ready' | 'sending' | 'saved' | 'failed' | 'unknown'

// 敲门：一次只敲一声，保存成功才算记下。
// 断网、超时或服务端出错时结果不明，沿原键再试，存过的会原样返回；确定被拒的才换键。
// 敲过后在服务器给的合并窗口内按钮先歇着：只是界面节制，按本地单调时钟倒数，不当权限。
export function useKnock(conversationId: () => string | null) {
  const session = usePrivateSession()
  const scope = usePrivateScope(conversationId)
  const state = ref<KnockState>('ready')
  const receipt = ref<Knock | null>(null)
  const reason = ref('')
  const cooling = ref(false)
  let key: string | null = null
  let timer: ReturnType<typeof setTimeout> | undefined

  function reset() {
    clearTimeout(timer)
    state.value = 'ready'
    receipt.value = null
    reason.value = ''
    cooling.value = false
    key = null
  }

  async function knock() {
    const id = conversationId()
    if (!id || state.value === 'sending' || cooling.value) return
    const active = scope.capture()
    const own = session.signal()
    key ??= crypto.randomUUID()
    const started = performance.now()
    state.value = 'sending'
    reason.value = ''
    try {
      const result = await api.knock(id, key, own)
      if (!active()) return
      key = null
      receipt.value = result
      state.value = 'saved'
      const wait = Math.max(0, result.next_allowed_at - result.received_at) * 1000
      clearTimeout(timer)
      cooling.value = wait > 0
      timer = setTimeout(
        () => {
          cooling.value = false
        },
        Math.max(0, started + wait - performance.now()),
      )
    } catch (error) {
      if (!active()) return
      if (error instanceof ApiFailure && error.status === 401) {
        session.fail(error, own)
        return
      }
      const unknown = !(error instanceof ApiFailure) || error.status === 0 || error.status >= 500
      if (!unknown) key = null
      state.value = unknown ? 'unknown' : 'failed'
      reason.value = explain(error)
    }
  }

  watch([conversationId, () => session.user.value?.id], reset, { flush: 'sync' })
  onBeforeUnmount(() => clearTimeout(timer))

  return { state, receipt, reason, cooling, knock }
}
