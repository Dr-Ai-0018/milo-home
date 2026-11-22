import { computed, ref, watch, type Ref } from 'vue'
import { api, ApiFailure, explain } from '../api/client'
import type { WorkAttachment } from '../api/client'
import { usePrivateSession } from './usePrivateSession'

interface UseWorkDownloadsOptions {
  taskId: () => string | null
  attachments: Ref<WorkAttachment[]>
  capture: () => () => boolean
  signal: () => AbortSignal
  onTaskGone: () => void
  onRefreshAttachments: () => void
}

/** Manage sealed attachment multi-selection, single download, and zip archive downloads. */
export function useWorkDownloads(options: UseWorkDownloadsOptions) {
  const session = usePrivateSession()
  const selectedIds = ref<string[]>([])
  const downloadingTarget = ref<string | null>(null)
  const downloadProblem = ref('')
  const downloadNotice = ref('')

  const selectedSet = computed(() => new Set(selectedIds.value))
  const selectedCount = computed(() => selectedIds.value.length)
  const allSelected = computed(
    () =>
      options.attachments.value.length > 0 &&
      options.attachments.value.every((item) => selectedSet.value.has(item.id)),
  )

  watch(
    options.attachments,
    (items) => {
      const valid = new Set(items.map((item) => item.id))
      const next = selectedIds.value.filter((itemId) => valid.has(itemId))
      if (next.length !== selectedIds.value.length) selectedIds.value = next
    },
    { flush: 'sync' },
  )

  function toggleSelect(attachmentId: string) {
    downloadProblem.value = ''
    if (selectedSet.value.has(attachmentId)) {
      selectedIds.value = selectedIds.value.filter((item) => item !== attachmentId)
    } else {
      selectedIds.value = [...selectedIds.value, attachmentId]
    }
  }

  function toggleAll() {
    downloadProblem.value = ''
    selectedIds.value = allSelected.value ? [] : options.attachments.value.map((item) => item.id)
  }

  function resetDownloads() {
    selectedIds.value = []
    downloadingTarget.value = null
    downloadProblem.value = ''
    downloadNotice.value = ''
  }

  async function runDownload(
    target: string,
    runner: (currentId: string, own: AbortSignal) => Promise<string>,
    successText: (filename: string) => string,
  ) {
    const currentId = options.taskId()
    if (!currentId || downloadingTarget.value) return
    downloadingTarget.value = target
    downloadProblem.value = ''
    downloadNotice.value = ''
    const active = options.capture()
    const own = options.signal()
    try {
      const filename = await runner(currentId, own)
      if (!active()) return
      downloadNotice.value = successText(filename)
    } catch (error) {
      if (!active()) return
      if (own.aborted) return
      if (error instanceof ApiFailure && error.status === 401) {
        session.fail(error, own)
        return
      }
      if (error instanceof ApiFailure && error.status === 404) {
        try {
          await api.workTask(currentId, own)
          if (!active()) return
          downloadProblem.value =
            error.code === 'work_not_found'
              ? '附件清单可能刚更新，已重新读取；请再试一次。'
              : explain(error)
          options.onRefreshAttachments()
        } catch (probeError) {
          if (!active() || own.aborted) return
          if (probeError instanceof ApiFailure && probeError.status === 401) {
            session.fail(probeError, own)
            return
          }
          if (probeError instanceof ApiFailure && probeError.status === 404) {
            options.onTaskGone()
            return
          }
          downloadProblem.value = explain(error)
        }
        return
      }
      downloadProblem.value = explain(error)
    } finally {
      if (active() && downloadingTarget.value === target) downloadingTarget.value = null
    }
  }

  function downloadOne(item: WorkAttachment) {
    return runDownload(
      item.id,
      (currentId, own) => api.downloadWorkAttachment(currentId, item.id, item.filename, own),
      (filename) => `已开始保存「${filename}」。`,
    )
  }

  function downloadSelected() {
    const picked = options.attachments.value
      .filter((item) => selectedSet.value.has(item.id))
      .map((item) => item.id)
    if (!picked.length) return Promise.resolve()
    return runDownload(
      'selected',
      (currentId, own) =>
        api.downloadWorkArchive(currentId, picked, `work-${currentId}-selected.zip`, own),
      (filename) => `已打包所选 ${picked.length} 份文件（${filename}）。`,
    )
  }

  function downloadAll() {
    if (!options.attachments.value.length) return Promise.resolve()
    return runDownload(
      'all',
      (currentId, own) =>
        api.downloadWorkArchive(currentId, null, `work-${currentId}-attachments.zip`, own),
      (filename) => `已打包全部文件（${filename}）。`,
    )
  }

  return {
    selectedIds,
    selectedSet,
    selectedCount,
    allSelected,
    downloadingTarget,
    downloadProblem,
    downloadNotice,
    toggleSelect,
    toggleAll,
    downloadOne,
    downloadSelected,
    downloadAll,
    resetDownloads,
  }
}
