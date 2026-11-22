import { ref } from 'vue'
import { ApiFailure, explain } from '../api/client'
import type { ConversationMessage } from '../api/client'

export interface OutgoingSlip {
  id: string
  body: string
  key: string
  state: 'sending' | 'failed' | 'unknown'
  reason: string
}

// 还没确认保存的纸条：每张带自己的幂等键，重试沿用原键，服务器确认后从这里移走。
export function useChatOutbox(
  post: (body: string, key: string) => Promise<ConversationMessage | null>,
  onAuthLost: (error: unknown) => void,
) {
  const slips = ref<OutgoingSlip[]>([])

  async function deliver(slip: OutgoingSlip) {
    slip.state = 'sending'
    slip.reason = ''
    try {
      const saved = await post(slip.body, slip.key)
      if (saved) slips.value = slips.value.filter((item) => item.id !== slip.id)
    } catch (error) {
      if (error instanceof ApiFailure && error.status === 401) {
        slips.value = []
        onAuthLost(error)
        return
      }
      // 超时或断线时不知道服务器有没有存下，只说不确定。
      slip.state = error instanceof ApiFailure && error.status === 0 ? 'unknown' : 'failed'
      slip.reason = explain(error)
    }
  }

  function send(body: string) {
    const slip: OutgoingSlip = {
      id: crypto.randomUUID(),
      body,
      key: crypto.randomUUID(),
      state: 'sending',
      reason: '',
    }
    slips.value.push(slip)
    // 取回响应式代理再改状态，界面才会跟着变。
    return deliver(slips.value.at(-1) ?? slip)
  }

  function retry(id: string) {
    const slip = slips.value.find((item) => item.id === id)
    if (slip && slip.state !== 'sending') return deliver(slip)
  }

  function discard(id: string) {
    slips.value = slips.value.filter((item) => item.id !== id)
  }

  function clear() {
    slips.value = []
  }

  return { slips, send, retry, discard, clear }
}
