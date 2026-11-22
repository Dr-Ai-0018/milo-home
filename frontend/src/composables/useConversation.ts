import { ref, watch } from 'vue'
import { api, ApiFailure, explain } from '../api/client'
import type { ConversationMessage, ConversationPage } from '../api/client'
import { usePrivateSession } from './usePrivateSession'
import { usePrivateScope } from './usePrivateScope'

const PAGE = 100

// 读过的会话留在内存里，再打开时先原样摆出来，只补读后面新增的；退出或失效时随私密状态清空。
interface Kept {
  messages: ConversationMessage[]
  after: number
  before: number | null
  hasEarlier: boolean
  behind: boolean
}
const kept = new Map<string, Kept>()
usePrivateSession().onClear(() => kept.clear())

// 一个会话（一封信或聊天）的消息、游标与发送。书信和聊天共用。
// 打开时先读最新一页，更早的按需往前读，新来的按 after 往后补；排序只认 sequence，空隙不重编号。
// 往后补的游标只认最新页和补读页确认到的最高序号，往前读的页不能把它拉回去。
export function useConversation(conversationId: () => string | null) {
  const session = usePrivateSession()
  const scope = usePrivateScope(conversationId)
  const messages = ref<ConversationMessage[]>([])
  const loading = ref(false)
  // 已读到最新一页；没读到之前不说哪张没回。
  const settled = ref(false)
  const hasEarlier = ref(false)
  // 上一轮补读读满了一页，后面可能还有，等下一轮。
  const behind = ref(false)
  const problem = ref('')
  let after = 0
  let before: number | null = null

  function reset() {
    scope.invalidate()
    loading.value = false
    problem.value = ''
    const id = conversationId()
    const entry = id ? kept.get(id) : undefined
    messages.value = entry?.messages ?? []
    settled.value = !!entry
    hasEarlier.value = entry?.hasEarlier ?? false
    behind.value = entry?.behind ?? false
    after = entry?.after ?? 0
    before = entry?.before ?? null
  }

  // 还没读到最新页时不留底，免得下次打开把半截记录当成读全了。
  function keep(id: string) {
    if (!settled.value) return
    kept.set(id, {
      messages: messages.value,
      after,
      before,
      hasEarlier: hasEarlier.value,
      behind: behind.value,
    })
  }

  // 各个方向读到的页都按 sequence 排成阅读顺序；刚发出的信先按服务器返回并入。
  function merge(incoming: ConversationMessage[]) {
    const seen = new Set(messages.value.map((message) => message.id))
    const fresh = incoming.filter((message) => !seen.has(message.id))
    if (!fresh.length) return
    messages.value = [...messages.value, ...fresh].sort((a, b) => a.sequence - b.sequence)
  }

  async function fetchPage(
    read: (own: AbortSignal) => Promise<ConversationPage>,
    active: () => boolean,
  ) {
    const own = session.signal()
    try {
      const page = await read(own)
      return active() ? page : null
    } catch (error) {
      if (!active()) return null
      throw error
    }
  }

  async function readLatest(id: string, active: () => boolean) {
    const page = await fetchPage((own) => api.latestMessages(id, own), active)
    if (!page) return
    merge(page.messages)
    after = Math.max(after, page.next_after)
    before = page.next_before
    hasEarlier.value = page.has_more_before
    behind.value = false
    settled.value = true
    keep(id)
  }

  async function readAfter(id: string, active: () => boolean) {
    const page = await fetchPage((own) => api.conversationMessages(id, after, own), active)
    if (!page) return 0
    merge(page.messages)
    if (page.messages.length) after = Math.max(after, page.next_after)
    behind.value = page.messages.length === PAGE
    keep(id)
    return page.messages.length
  }

  // 登录失效交给共享状态清空；其他错误只留在本会话旁边。
  function report(error: unknown) {
    if (session.signal().aborted) return
    if (error instanceof ApiFailure && error.status === 401) session.fail(error)
    else problem.value = explain(error)
  }

  async function run(task: (id: string, active: () => boolean) => Promise<void>) {
    const id = conversationId()
    if (!id) return
    const active = scope.capture()
    if (!active()) return
    loading.value = true
    problem.value = ''
    try {
      await task(id, active)
    } catch (error) {
      if (active()) report(error)
    } finally {
      if (active()) loading.value = false
    }
  }

  // 打开会话：读过的先摆出来再悄悄补读新增的；没读过就读最新一页。
  const load = async () => {
    reset()
    const id = conversationId()
    if (!id || !kept.has(id)) return run(readLatest)
    try {
      await catchUp()
    } catch (error) {
      report(error)
    }
  }

  // 往前读一页更早的。结果只交给发起的那块界面说明，不占用整段的读取状态和提示。
  async function earlier() {
    const id = conversationId()
    if (!id || before === null || !hasEarlier.value) return true
    const active = scope.capture()
    const from = before
    let page
    try {
      page = await fetchPage((own) => api.earlierMessages(id, from, own), active)
    } catch (error) {
      if (error instanceof ApiFailure && error.status === 401) session.fail(error)
      return false
    }
    if (!page) return true
    merge(page.messages)
    before = page.next_before ?? from
    hasEarlier.value = page.has_more_before
    keep(id)
    return true
  }

  // 增量补读：还没读到最新页时先读最新页；之后每轮最多三页往后补，用于前台刷新。
  // 出错时抛给调用方决定是否退避。
  async function catchUp(pages = 3) {
    const id = conversationId()
    if (!id) return
    const active = scope.capture()
    if (!settled.value) {
      if (!document.hidden) await readLatest(id, active)
      return
    }
    for (let page = 0; page < pages && !document.hidden && active(); page += 1) {
      if ((await readAfter(id, active)) < PAGE) break
    }
  }

  // 只负责保存一条来信；草稿、重试键和提示由各自的界面管理。
  async function post(body: string, key: string) {
    const id = conversationId()
    if (!id) return null
    const own = session.signal()
    const active = scope.capture()
    if (!active()) return null
    let message
    try {
      message = await api.postToConversation(id, body, key, own)
    } catch (error) {
      if (!active()) return null
      throw error
    }
    if (!active()) return null
    merge([message])
    keep(id)
    return message
  }

  watch(conversationId, reset, { flush: 'sync' })
  // 登录失效或退出后，本会话内容随之清空。
  watch(
    () => session.user.value?.id,
    () => reset(),
    { flush: 'sync' },
  )

  return {
    messages,
    loading,
    settled,
    hasEarlier,
    behind,
    problem,
    load,
    earlier,
    catchUp,
    post,
    report,
  }
}
