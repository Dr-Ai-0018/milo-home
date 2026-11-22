import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { Ref } from 'vue'
import { api, explain } from '../api/client'
import type { PublicNoteDetail, PublicNoteSummary } from '../api/client'
import { isLegacyNotesMissing, isNoteNotFound } from '../utils/publicNotes'

const HOME_LIMIT = 3
const HISTORY_LIMIT = 10

interface HistoryMemory {
  notes: PublicNoteSummary[]
  nextBefore: number | null
  hasLoaded: boolean
  reachedEnd: boolean
}

// 仅保存在会话内存，不写入任何浏览器持久存储；用于从详情返回历史时保留已读页。
const historyMemory: HistoryMemory = {
  notes: [],
  nextBefore: null,
  hasLoaded: false,
  reachedEnd: false,
}

/** 详情页确认某条目 404（已撤下/不存在）后，立即从内存列表中移除该条目。 */
export function removePublicNoteFromMemory(noteId: string) {
  if (!historyMemory.hasLoaded) return
  const filtered = historyMemory.notes.filter((item) => item.id !== noteId)
  if (filtered.length !== historyMemory.notes.length) {
    historyMemory.notes = filtered
  }
}

/** 详情页读到新公开修订时，同步内存历史列表中对应条目的摘要字段。 */
function syncPublicNoteInMemory(detail: PublicNoteDetail) {
  if (!historyMemory.hasLoaded) return
  const index = historyMemory.notes.findIndex((item) => item.id === detail.id)
  if (index === -1) return
  const updated: PublicNoteSummary = {
    id: detail.id,
    sequence: detail.sequence,
    revision: detail.revision,
    title: detail.title,
    summary: detail.summary,
    category: detail.category,
    content_date: detail.content_date,
    published_at: detail.published_at,
    updated_at: detail.updated_at,
  }
  historyMemory.notes = historyMemory.notes.map((item, idx) => (idx === index ? updated : item))
}

/** 首页最近 3 条公开近况：独立加载，不阻塞作息与入口；不轮询、不写持久缓存。 */
export function useHomePublicNotes() {
  const notes = ref<PublicNoteSummary[]>([])
  const hasLoaded = ref(false)
  const legacyMode = ref(false)
  const loading = ref(true)
  const problem = ref('')
  const refreshProblem = ref('')
  let controller: AbortController | null = null
  let token = 0

  async function load() {
    if (loading.value && controller) return
    controller?.abort()
    const own = new AbortController()
    controller = own
    const currentToken = ++token
    loading.value = true
    problem.value = ''
    refreshProblem.value = ''
    try {
      const page = await api.publicNotes(HOME_LIMIT, null, own.signal)
      if (own.signal.aborted || currentToken !== token) return
      notes.value = page.notes
      hasLoaded.value = true
      legacyMode.value = false
    } catch (error) {
      if (own.signal.aborted || currentToken !== token) return
      if (isLegacyNotesMissing(error)) {
        notes.value = []
        hasLoaded.value = false
        legacyMode.value = true
        return
      }
      const message = explain(error)
      if (hasLoaded.value) {
        refreshProblem.value = `这次没读到最新近况（${message}），下面暂留刚才的内容。`
      } else {
        legacyMode.value = false
        problem.value = message
      }
    } finally {
      if (!own.signal.aborted && currentToken === token) {
        loading.value = false
        controller = null
      }
    }
  }

  onMounted(() => {
    void load()
  })

  onBeforeUnmount(() => {
    token += 1
    controller?.abort()
    controller = null
  })

  return {
    notes,
    hasLoaded,
    legacyMode,
    loading,
    problem,
    refreshProblem,
    reload: load,
  }
}

/** 公开近况历史页：默认 10 条、按 sequence 倒序翻页；纯内存保留已读页以便返回定位。 */
export function usePublicNotesHistory() {
  const notes = ref<PublicNoteSummary[]>(historyMemory.hasLoaded ? [...historyMemory.notes] : [])
  const nextBefore = ref<number | null>(historyMemory.hasLoaded ? historyMemory.nextBefore : null)
  const hasLoaded = ref(historyMemory.hasLoaded)
  const reachedEnd = ref(historyMemory.hasLoaded ? historyMemory.reachedEnd : false)
  const legacyMode = ref(false)
  const loading = ref(!historyMemory.hasLoaded)
  const loadingMore = ref(false)
  const problem = ref('')
  const refreshProblem = ref('')
  const moreProblem = ref('')
  let controller: AbortController | null = null
  let token = 0

  function syncMemory() {
    historyMemory.notes = [...notes.value]
    historyMemory.nextBefore = nextBefore.value
    historyMemory.hasLoaded = hasLoaded.value
    historyMemory.reachedEnd = reachedEnd.value
  }

  function clearMemory() {
    historyMemory.notes = []
    historyMemory.nextBefore = null
    historyMemory.hasLoaded = false
    historyMemory.reachedEnd = false
  }

  async function refresh() {
    if (loading.value && controller) return
    if (loadingMore.value) return
    controller?.abort()
    const own = new AbortController()
    controller = own
    const currentToken = ++token
    loading.value = true
    problem.value = ''
    refreshProblem.value = ''
    moreProblem.value = ''
    try {
      const page = await api.publicNotes(HISTORY_LIMIT, null, own.signal)
      if (own.signal.aborted || currentToken !== token) return
      notes.value = page.notes
      nextBefore.value = page.next_before
      hasLoaded.value = true
      legacyMode.value = false
      reachedEnd.value = page.next_before === null && page.notes.length > 0
      syncMemory()
    } catch (error) {
      if (own.signal.aborted || currentToken !== token) return
      if (isLegacyNotesMissing(error)) {
        notes.value = []
        nextBefore.value = null
        hasLoaded.value = false
        reachedEnd.value = false
        legacyMode.value = true
        clearMemory()
        return
      }
      const message = explain(error)
      if (hasLoaded.value) {
        refreshProblem.value = `这次没能刷新最新近况（${message}），下面暂留已读内容。`
      } else {
        legacyMode.value = false
        problem.value = message
      }
    } finally {
      if (!own.signal.aborted && currentToken === token) {
        loading.value = false
        controller = null
      }
    }
  }

  async function loadMore() {
    if (loading.value || loadingMore.value || nextBefore.value === null) return
    const cursor = nextBefore.value
    controller?.abort()
    const own = new AbortController()
    controller = own
    const currentToken = ++token
    loadingMore.value = true
    moreProblem.value = ''
    try {
      const page = await api.publicNotes(HISTORY_LIMIT, cursor, own.signal)
      if (own.signal.aborted || currentToken !== token) return
      if (page.notes.length > 0) {
        const seen = new Set(notes.value.map((item) => item.id))
        const incoming = page.notes.filter((item) => !seen.has(item.id))
        notes.value = [...notes.value, ...incoming]
      }
      nextBefore.value = page.next_before
      if (page.next_before === null) {
        reachedEnd.value = true
      }
      syncMemory()
    } catch (error) {
      if (own.signal.aborted || currentToken !== token) return
      moreProblem.value = `更早的近况暂时没读出来（${explain(error)}），可以稍后再试。`
    } finally {
      if (!own.signal.aborted && currentToken === token) {
        loadingMore.value = false
        controller = null
      }
    }
  }

  onMounted(() => {
    if (!historyMemory.hasLoaded) {
      void refresh()
    } else {
      // 同步可能被详情 404 移除的条目，不重拉覆盖当前已读页。
      notes.value = [...historyMemory.notes]
      nextBefore.value = historyMemory.nextBefore
      reachedEnd.value = historyMemory.reachedEnd
      loading.value = false
    }
  })

  onBeforeUnmount(() => {
    token += 1
    controller?.abort()
    controller = null
  })

  return {
    notes,
    nextBefore,
    hasLoaded,
    reachedEnd,
    legacyMode,
    loading,
    loadingMore,
    problem,
    refreshProblem,
    moreProblem,
    refresh,
    loadMore,
  }
}

/** 单条公开近况详情：纯文本正文；404 视为不可读并从内存历史移除，绝不降级静态文案。 */
export function usePublicNoteDetail(noteId: Ref<string>) {
  const note = ref<PublicNoteDetail | null>(null)
  const loading = ref(true)
  const notFound = ref(false)
  const problem = ref('')
  const refreshProblem = ref('')
  let controller: AbortController | null = null
  let token = 0

  async function load(id: string, preserveCurrent: boolean) {
    if (preserveCurrent && loading.value && controller) return
    controller?.abort()
    const own = new AbortController()
    controller = own
    const currentToken = ++token
    loading.value = true
    notFound.value = false
    problem.value = ''
    refreshProblem.value = ''
    if (!preserveCurrent) {
      note.value = null
    }
    try {
      const result = await api.publicNote(id, own.signal)
      if (own.signal.aborted || currentToken !== token) return
      note.value = result
      notFound.value = false
      syncPublicNoteInMemory(result)
    } catch (error) {
      if (own.signal.aborted || currentToken !== token) return
      if (isNoteNotFound(error)) {
        note.value = null
        notFound.value = true
        removePublicNoteFromMemory(id)
        return
      }
      const message = explain(error)
      if (note.value) {
        refreshProblem.value = `这次没能核对到最新内容（${message}），下面暂留刚才读到的版本。`
      } else {
        problem.value = message
      }
    } finally {
      if (!own.signal.aborted && currentToken === token) {
        loading.value = false
        controller = null
      }
    }
  }

  watch(
    noteId,
    (id) => {
      if (!id) {
        note.value = null
        notFound.value = true
        loading.value = false
        return
      }
      void load(id, false)
    },
    { immediate: true },
  )

  function reload() {
    const id = noteId.value
    if (!id) return
    void load(id, note.value !== null)
  }

  onBeforeUnmount(() => {
    token += 1
    controller?.abort()
    controller = null
  })

  return {
    note,
    loading,
    notFound,
    problem,
    refreshProblem,
    reload,
  }
}
