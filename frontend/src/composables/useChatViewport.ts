import { nextTick, onBeforeUnmount, onMounted, watch } from 'vue'
import { usePrivateScope } from './usePrivateScope'

interface ChatViewportOptions {
  conversationId: () => string | null
  hasMessages: () => boolean
  hold: () => void
}

// 只管理本页阅读位置；收发、分页、草稿和定时读取仍由原模块负责。
export function useChatViewport(options: ChatViewportOptions) {
  const scope = usePrivateScope(options.conversationId)
  let intent = 0
  let lastY = 0
  let requestedEnd = false
  // 主动浏览后，即使首读失败再试，也不重新获得「第一次就滚到底」的许可。
  let initialPosition = true

  function remember() {
    intent += 1
    requestedEnd = false
    initialPosition = false
    options.hold()
  }

  function scrolled() {
    const current = scrollY
    if (Math.abs(current - lastY) > 1) remember()
    lastY = current
  }

  function wheel(event: WheelEvent) {
    if (event.deltaX || event.deltaY) remember()
  }

  function keyboard(event: KeyboardEvent) {
    const target = event.target
    if (
      event.defaultPrevented ||
      (target instanceof Element && target.closest('input, textarea, select, [contenteditable]'))
    )
      return
    if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key))
      remember()
  }

  function edited(event: Event) {
    if (event.target instanceof Element && event.target.closest('.chat-composer')) remember()
  }

  function visibility() {
    if (document.hidden) remember()
  }

  function nearEnd() {
    return innerHeight + scrollY >= document.documentElement.scrollHeight - 160
  }

  function lastHidden() {
    const composer = document.querySelector('.chat-composer')?.getBoundingClientRect()
    const last = document.querySelector('.chat-slips > li:last-child')?.getBoundingClientRect()
    return !!composer && !!last && last.bottom > composer.top
  }

  function toEnd() {
    scrollTo({
      top: document.documentElement.scrollHeight,
      behavior: 'instant',
    })
    // 自己的定位不算用户又改了主意；后续真实滚动仍会更新 intent。
    lastY = scrollY
  }

  function prepareRead(first: boolean) {
    const active = scope.capture()
    const mine = intent
    const follow = first ? initialPosition : nearEnd()
    if (!follow) options.hold()
    const allowed = () =>
      active() && !document.hidden && ((follow && mine === intent) || requestedEnd)
    return async (changed: boolean) => {
      if (!changed || !allowed()) return
      await nextTick()
      if (!allowed()) return
      if (requestedEnd || !first || lastHidden()) toEnd()
      requestedEnd = false
    }
  }

  // 主动发送意味着要看自己刚递出的纸条；若首读还没回来，等它排好后也可定位。
  async function revealSent() {
    const active = scope.capture()
    intent += 1
    const mine = intent
    requestedEnd = true
    await nextTick()
    if (active() && !document.hidden && mine === intent) toEnd()
  }

  watch(
    options.conversationId,
    (_id, previous) => {
      if (!previous) return
      intent += 1
      requestedEnd = false
      initialPosition = true
    },
    { flush: 'sync' },
  )

  onMounted(() => {
    if (options.hasMessages() && lastHidden()) toEnd()
    else {
      scrollTo({
        top: 0,
        behavior: 'instant',
      })
      lastY = scrollY
    }
    window.addEventListener('scroll', scrolled, { passive: true })
    window.addEventListener('wheel', wheel, { passive: true })
    window.addEventListener('touchmove', remember, { passive: true })
    window.addEventListener('keydown', keyboard)
    window.addEventListener('input', edited)
    document.addEventListener('visibilitychange', visibility)
  })

  onBeforeUnmount(() => {
    window.removeEventListener('scroll', scrolled)
    window.removeEventListener('wheel', wheel)
    window.removeEventListener('touchmove', remember)
    window.removeEventListener('keydown', keyboard)
    window.removeEventListener('input', edited)
    document.removeEventListener('visibilitychange', visibility)
  })

  return {
    prepareRead,
    revealSent,
  }
}
