import { computed, ref } from 'vue'
import { api, ApiFailure, explain } from '../api/client'
import type { WorkArtifact, WorkAttachmentList, WorkReceipt } from '../api/client'
import { createWorkReadJob } from '../utils/workReadJob'
import { usePrivateSession } from './usePrivateSession'

interface UseWorkDeliveryOptions {
  taskId: () => string | null
  capture: () => () => boolean
  signal: () => AbortSignal
  onTaskGone: () => void
}

// 回执、封存附件与旧成果各用一条读取通道，轮询和手动重试共享在途请求。
export function useWorkDelivery(options: UseWorkDeliveryOptions) {
  const session = usePrivateSession()
  const artifacts = ref<WorkArtifact[]>([])
  const receipt = ref<WorkReceipt | null>(null)
  const attachments = ref<WorkAttachmentList | null>(null)
  const receiptLoading = ref(false)
  const attachmentsLoading = ref(false)
  const receiptProblem = ref('')
  const attachmentsProblem = ref('')
  const artifactsProblem = ref('')
  const receiptRetryable = ref(false)
  const attachmentsRetryable = ref(false)
  const refreshingReceipt = ref(false)
  const refreshingAttachments = ref(false)

  function channel<T>(
    fetcher: (taskId: string, own: AbortSignal) => Promise<T>,
    onLoading: (value: boolean) => void,
    onSuccess: (value: T) => void,
    onError: (error?: unknown) => void,
  ) {
    return createWorkReadJob(async () => {
      const taskId = options.taskId()
      if (!taskId) return
      const current = options.capture()
      const own = options.signal()
      const active = () => current() && !own.aborted
      onLoading(true)
      try {
        const value = await fetcher(taskId, own)
        if (!active()) return
        onSuccess(value)
        onError()
      } catch (error) {
        if (!active()) return
        if (error instanceof ApiFailure && error.status === 401) {
          session.fail(error, own)
          return
        }
        if (
          error instanceof ApiFailure &&
          error.status === 404 &&
          error.code === 'work_not_found'
        ) {
          options.onTaskGone()
          return
        }
        if (error instanceof ApiFailure && error.status === 404) {
          // 旧后端可能没有交付接口，先核对原单，不能把普通 404 当作撤权。
          try {
            await api.workTask(taskId, own)
          } catch (probe) {
            if (!active()) return
            if (probe instanceof ApiFailure && probe.status === 404) {
              options.onTaskGone()
              return
            }
            if (probe instanceof ApiFailure && probe.status === 401) {
              session.fail(probe, own)
              return
            }
          }
        }
        if (active()) onError(error)
      } finally {
        if (active()) onLoading(false)
      }
    })
  }

  function retryable(error: unknown) {
    return error instanceof ApiFailure && (error.status === 0 || error.status >= 500)
  }

  const receiptJob = channel(
    api.workReceipt,
    (value) => {
      receiptLoading.value = value
    },
    (value) => {
      receipt.value = value
    },
    (error) => {
      receiptProblem.value = error === undefined ? '' : explain(error)
      receiptRetryable.value = retryable(error)
    },
  )
  const attachmentJob = channel(
    api.workAttachments,
    (value) => {
      attachmentsLoading.value = value
    },
    (value) => {
      attachments.value = value
    },
    (error) => {
      attachmentsProblem.value = error === undefined ? '' : explain(error)
      attachmentsRetryable.value = retryable(error)
    },
  )
  const artifactJob = channel(
    api.workArtifacts,
    () => undefined,
    (value) => {
      artifacts.value = value.artifacts
    },
    (error) => {
      artifactsProblem.value = error === undefined ? '' : explain(error)
    },
  )

  function resetDelivery() {
    receiptJob.reset()
    attachmentJob.reset()
    artifactJob.reset()
    artifacts.value = []
    receipt.value = null
    attachments.value = null
    receiptLoading.value = false
    attachmentsLoading.value = false
    receiptProblem.value = ''
    attachmentsProblem.value = ''
    artifactsProblem.value = ''
    receiptRetryable.value = false
    attachmentsRetryable.value = false
    refreshingReceipt.value = false
    refreshingAttachments.value = false
  }

  function readAll(fresh = false) {
    return Promise.all([receiptJob.run(fresh), attachmentJob.run(fresh), artifactJob.run(fresh)])
  }

  async function refreshReceipt() {
    if (!options.taskId() || receiptLoading.value || refreshingReceipt.value) return
    const active = options.capture()
    refreshingReceipt.value = true
    try {
      if (!(await session.verify()) || !active()) return
      await receiptJob.run()
    } finally {
      if (active()) refreshingReceipt.value = false
    }
  }

  async function refreshAttachments() {
    if (!options.taskId() || attachmentsLoading.value || refreshingAttachments.value) return
    const active = options.capture()
    refreshingAttachments.value = true
    try {
      if (!(await session.verify()) || !active()) return
      await Promise.all([attachmentJob.run(), artifactJob.run()])
    } finally {
      if (active()) refreshingAttachments.value = false
    }
  }

  return {
    artifacts,
    receipt,
    attachments,
    receiptLoading: computed(() => receiptLoading.value || refreshingReceipt.value),
    attachmentsLoading: computed(() => attachmentsLoading.value || refreshingAttachments.value),
    receiptProblem,
    attachmentsProblem,
    artifactsProblem,
    receiptRetryable,
    attachmentsRetryable,
    resetDelivery,
    readAll,
    refreshReceipt,
    refreshAttachments,
  }
}
