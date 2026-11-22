import { onBeforeUnmount, onMounted, ref } from 'vue'
import { api, explain } from '../api/client'
import type { PublicWorkDetail, PublicWorkSummary } from '../api/client'
import { isLegacyWorksMissing } from '../utils/publicWorks'

const HOME_WORKS_LIMIT = 2
const CATALOG_WORKS_LIMIT = 10

interface CatalogMemory {
  works: PublicWorkSummary[]
  nextBefore: number | null
  hasLoaded: boolean
  reachedEnd: boolean
  lastOpenedId: string | null
}

// 仅保存在会话内存，不写浏览器持久存储；用于从作品详情返回目录时保留已读页与原条目焦点。
const catalogMemory: CatalogMemory = {
  works: [],
  nextBefore: null,
  hasLoaded: false,
  reachedEnd: false,
  lastOpenedId: null,
}

export function rememberOpenedWork(workId: string) {
  catalogMemory.lastOpenedId = workId
}

export function consumeLastOpenedWork(): string | null {
  const id = catalogMemory.lastOpenedId
  catalogMemory.lastOpenedId = null
  return id
}

/** 详情确认 404（已撤下或不存在）后，立即从目录内存中移除对应条目。 */
export function removePublicWorkFromMemory(workId: string) {
  if (catalogMemory.lastOpenedId === workId) {
    catalogMemory.lastOpenedId = null
  }
  if (!catalogMemory.hasLoaded) return
  const filtered = catalogMemory.works.filter((item) => item.id !== workId)
  if (filtered.length !== catalogMemory.works.length) {
    catalogMemory.works = filtered
  }
}

/** 详情读到新公开修订时，同步更新目录内存中对应条目的摘要与封面。 */
export function syncPublicWorkInMemory(detail: PublicWorkDetail) {
  if (!catalogMemory.hasLoaded) return
  const index = catalogMemory.works.findIndex((item) => item.id === detail.id)
  if (index === -1) return
  const updated: PublicWorkSummary = {
    id: detail.id,
    sequence: detail.sequence,
    revision: detail.revision,
    title: detail.title,
    summary: detail.summary,
    category: detail.category,
    version_label: detail.version_label,
    content_date: detail.content_date,
    published_at: detail.published_at,
    updated_at: detail.updated_at,
    cover: detail.cover,
  }
  catalogMemory.works = catalogMemory.works.map((item, idx) => (idx === index ? updated : item))
}

/** 首页最多两件作品：独立于作息、近况与登录读取，不轮询、不写持久缓存。 */
export function useHomePublicWorks() {
  const works = ref<PublicWorkSummary[]>([])
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
      const page = await api.publicWorks(HOME_WORKS_LIMIT, null, own.signal)
      if (own.signal.aborted || currentToken !== token) return
      works.value = page.works
      hasLoaded.value = true
      legacyMode.value = false
    } catch (error) {
      if (own.signal.aborted || currentToken !== token) return
      if (isLegacyWorksMissing(error)) {
        works.value = []
        hasLoaded.value = false
        legacyMode.value = true
        return
      }
      const message = explain(error)
      if (hasLoaded.value) {
        refreshProblem.value = `这次没读到最新作品（${message}），下面暂留刚才的内容。`
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
    works,
    hasLoaded,
    legacyMode,
    loading,
    problem,
    refreshProblem,
    reload: load,
  }
}

/** 公开作品目录：默认 10 条按首次发布顺序向前翻，返回保留内存已读页与原条目焦点。 */
export function usePublicWorksCatalog() {
  const works = ref<PublicWorkSummary[]>(catalogMemory.hasLoaded ? [...catalogMemory.works] : [])
  const nextBefore = ref<number | null>(catalogMemory.hasLoaded ? catalogMemory.nextBefore : null)
  const hasLoaded = ref(catalogMemory.hasLoaded)
  const reachedEnd = ref(catalogMemory.hasLoaded ? catalogMemory.reachedEnd : false)
  const legacyMode = ref(false)
  const loading = ref(!catalogMemory.hasLoaded)
  const loadingMore = ref(false)
  const problem = ref('')
  const refreshProblem = ref('')
  const moreProblem = ref('')
  let controller: AbortController | null = null
  let token = 0

  function syncMemory() {
    catalogMemory.works = [...works.value]
    catalogMemory.nextBefore = nextBefore.value
    catalogMemory.hasLoaded = hasLoaded.value
    catalogMemory.reachedEnd = reachedEnd.value
  }

  function clearMemory() {
    catalogMemory.works = []
    catalogMemory.nextBefore = null
    catalogMemory.hasLoaded = false
    catalogMemory.reachedEnd = false
    catalogMemory.lastOpenedId = null
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
      const page = await api.publicWorks(CATALOG_WORKS_LIMIT, null, own.signal)
      if (own.signal.aborted || currentToken !== token) return
      works.value = page.works
      nextBefore.value = page.next_before
      hasLoaded.value = true
      legacyMode.value = false
      reachedEnd.value = page.next_before === null && page.works.length > 0
      syncMemory()
    } catch (error) {
      if (own.signal.aborted || currentToken !== token) return
      if (isLegacyWorksMissing(error)) {
        works.value = []
        nextBefore.value = null
        hasLoaded.value = false
        reachedEnd.value = false
        legacyMode.value = true
        clearMemory()
        return
      }
      const message = explain(error)
      if (hasLoaded.value) {
        refreshProblem.value = `这次没能刷新作品目录（${message}），下面暂留已读内容。`
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
      const page = await api.publicWorks(CATALOG_WORKS_LIMIT, cursor, own.signal)
      if (own.signal.aborted || currentToken !== token) return
      if (page.works.length > 0) {
        const seen = new Set(works.value.map((item) => item.id))
        const incoming = page.works.filter((item) => !seen.has(item.id))
        works.value = [...works.value, ...incoming]
      }
      nextBefore.value = page.next_before
      if (page.next_before === null) {
        reachedEnd.value = true
      }
      syncMemory()
    } catch (error) {
      if (own.signal.aborted || currentToken !== token) return
      moreProblem.value = `更早的作品暂时没读出来（${explain(error)}），可以稍后再试。`
    } finally {
      if (!own.signal.aborted && currentToken === token) {
        loadingMore.value = false
        controller = null
      }
    }
  }

  onMounted(() => {
    if (!catalogMemory.hasLoaded) {
      void refresh()
    } else {
      works.value = [...catalogMemory.works]
      nextBefore.value = catalogMemory.nextBefore
      reachedEnd.value = catalogMemory.reachedEnd
      loading.value = false
    }
  })

  onBeforeUnmount(() => {
    token += 1
    controller?.abort()
    controller = null
  })

  return {
    works,
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
