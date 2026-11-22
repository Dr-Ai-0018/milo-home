import { onBeforeUnmount, watch } from 'vue'
import { usePrivateSession } from './usePrivateSession'

// 请求属于一次页面身份；同一封信离开后再回来，也不能接收上一次的结果。
export function usePrivateScope(identity: () => string | null) {
  const session = usePrivateSession()
  let revision = 0
  let disposed = false

  function invalidate() {
    revision += 1
  }

  function capture() {
    const version = revision
    const own = session.signal()
    const id = identity()
    return () => !disposed && !own.aborted && version === revision && id === identity()
  }

  watch([identity, () => session.user.value?.id], invalidate, { flush: 'sync' })
  onBeforeUnmount(() => {
    disposed = true
    invalidate()
  })

  return { capture, invalidate }
}
