import { onBeforeUnmount, ref, watch } from 'vue'
import type { Ref } from 'vue'
import { api, explain } from '../api/client'
import type { PublicWorkDetail, PublicWorkFile } from '../api/client'
import { isWorkAssetNotFound, isWorkNotFound, stripApiPrefix } from '../utils/publicWorks'
import { removePublicWorkFromMemory, syncPublicWorkInMemory } from './usePublicWorks'

export interface WorkFileFeedback {
  message: string
  staleAsset: boolean
}

/** 作品详情与单文件独立下载：资产 404 只影响该文件并提供重读作品，不判整件撤下。 */
export function usePublicWorkDetail(workId: Ref<string>) {
  const work = ref<PublicWorkDetail | null>(null)
  const loading = ref(true)
  const notFound = ref(false)
  const problem = ref('')
  const refreshProblem = ref('')

  const downloadingId = ref<string | null>(null)
  const fileErrors = ref<Record<string, WorkFileFeedback>>({})
  const fileDone = ref<Record<string, string>>({})

  let detailController: AbortController | null = null
  let downloadController: AbortController | null = null
  let detailToken = 0
  let downloadToken = 0

  function resetDownloadState() {
    downloadToken += 1
    downloadController?.abort()
    downloadController = null
    downloadingId.value = null
    fileErrors.value = {}
    fileDone.value = {}
  }

  async function load(id: string, preserveCurrent: boolean) {
    if (preserveCurrent && loading.value && detailController) return
    detailController?.abort()
    const own = new AbortController()
    detailController = own
    const currentToken = ++detailToken
    loading.value = true
    notFound.value = false
    problem.value = ''
    refreshProblem.value = ''
    if (!preserveCurrent) {
      work.value = null
      resetDownloadState()
    }
    try {
      const result = await api.publicWork(id, own.signal)
      if (own.signal.aborted || currentToken !== detailToken) return
      work.value = result
      notFound.value = false
      resetDownloadState()
      syncPublicWorkInMemory(result)
    } catch (error) {
      if (own.signal.aborted || currentToken !== detailToken) return
      if (isWorkNotFound(error)) {
        work.value = null
        notFound.value = true
        resetDownloadState()
        removePublicWorkFromMemory(id)
        return
      }
      const message = explain(error)
      if (work.value) {
        refreshProblem.value = `这次没能核对到最新版本（${message}），下面暂留刚才读到的内容。`
      } else {
        problem.value = message
      }
    } finally {
      if (!own.signal.aborted && currentToken === detailToken) {
        loading.value = false
        detailController = null
      }
    }
  }

  async function downloadFile(file: PublicWorkFile) {
    if (downloadingId.value !== null) return
    downloadController?.abort()
    const own = new AbortController()
    downloadController = own
    const currentToken = ++downloadToken
    downloadingId.value = file.id
    const nextErrors = { ...fileErrors.value }
    delete nextErrors[file.id]
    fileErrors.value = nextErrors
    const nextDone = { ...fileDone.value }
    delete nextDone[file.id]
    fileDone.value = nextDone

    try {
      const savedName = await api.downloadPublicWorkFile(
        stripApiPrefix(file.download_url),
        file.filename,
        own.signal,
      )
      if (own.signal.aborted || currentToken !== downloadToken) return
      fileDone.value = { ...fileDone.value, [file.id]: `已开始下载 ${savedName}` }
    } catch (error) {
      if (own.signal.aborted || currentToken !== downloadToken) return
      if (isWorkAssetNotFound(error)) {
        fileErrors.value = {
          ...fileErrors.value,
          [file.id]: {
            message: '这份文件当前不在作品的公开版本中，作品可能刚更新过。',
            staleAsset: true,
          },
        }
        return
      }
      fileErrors.value = {
        ...fileErrors.value,
        [file.id]: {
          message: explain(error),
          staleAsset: false,
        },
      }
    } finally {
      if (!own.signal.aborted && currentToken === downloadToken) {
        downloadingId.value = null
        downloadController = null
      }
    }
  }

  watch(
    workId,
    (id) => {
      if (!id) {
        work.value = null
        notFound.value = true
        loading.value = false
        resetDownloadState()
        return
      }
      void load(id, false)
    },
    { immediate: true },
  )

  function reload() {
    const id = workId.value
    if (!id) return
    void load(id, work.value !== null)
  }

  onBeforeUnmount(() => {
    detailToken += 1
    detailController?.abort()
    detailController = null
    resetDownloadState()
  })

  return {
    work,
    loading,
    notFound,
    problem,
    refreshProblem,
    downloadingId,
    fileErrors,
    fileDone,
    reload,
    downloadFile,
  }
}
