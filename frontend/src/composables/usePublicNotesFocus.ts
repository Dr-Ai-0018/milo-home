import { nextTick, watch } from 'vue'
import type { Ref, WatchSource } from 'vue'

/** A retry or last-page button may disappear; keep its keyboard focus in the same section. */
export function usePublicNotesFocus(
  root: Ref<HTMLElement | null>,
  sources: WatchSource[],
  fallback: string,
) {
  watch(sources, async () => {
    const section = root.value
    const active = document.activeElement
    if (!(active instanceof HTMLElement) || !section?.contains(active)) return
    await nextTick()
    if (
      section.isConnected &&
      !active.isConnected &&
      (document.activeElement === document.body || document.activeElement === null)
    ) {
      section.querySelector<HTMLElement>(fallback)?.focus({ preventScroll: true })
    }
  })
}
