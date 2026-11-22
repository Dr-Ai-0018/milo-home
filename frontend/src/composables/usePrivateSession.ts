import { computed, ref, shallowRef } from 'vue'
import { api, ApiFailure, explain } from '../api/client'
import type { Conversation, Thread, User, Workspace } from '../api/client'
import { useRunHistory } from './useRunHistory'

// 书信与聊天共用的私密状态：一次登录、一个 thread 及其会话。
// 只放内存，关掉或刷新页面就没了；站内跳转时保留，退出、登录失效或换账号时由 clear() 清空并中止在途请求。
const user = ref<User | null>(null)
const thread = ref<Thread | null>(null)
const conversations = ref<Conversation[]>([])
const runHistory = useRunHistory({
  threadId: () => thread.value?.id ?? null,
  signal,
  onUnauthorized: fail,
})
const { runs, known: runsKnown, problem: runsProblem } = runHistory
const checkingRuns = ref(false)
const runsLoading = computed(() => runHistory.loading.value || checkingRuns.value)
// 工作区列表只在本次资格确认成功时用于导航；失败与无权是两种状态。
const workspaces = ref<Workspace[]>([])
const identityState = ref<'checking' | 'anonymous' | 'signed-in' | 'unavailable'>('checking')
const knownIdentity = shallowRef<User | null>(null)
const workspaceState = ref<'checking' | 'ready' | 'unavailable'>('checking')
const homeAccess = computed(() => {
  if (identityState.value !== 'signed-in') return identityState.value
  if (workspaceState.value !== 'ready') return workspaceState.value
  return workspaces.value.length ? 'granted' : 'no-access'
})
// 工作区列表可能同时有几次在读：只认最后发出的那次，撤权后旧的回应不能把「交办」入口带回来。
let workspaceRead = 0
const checked = ref(false)
// 会话目录这次登录里是否真的读到过；辅助读信记录有独立的 runsKnown。
const listed = ref(false)
const loading = ref(false)
const problem = ref('')
let controller = new AbortController()
let pending: Promise<void> | null = null
let checkingIdentity: Promise<User | null> | null = null
let checkingHome: Promise<void> | null = null
let directoryRead = 0
// 其他模块在内存里留的私密内容（如读过的信），随这里一起清空。
const cleaners = new Set<() => void>()

function onClear(cleaner: () => void) {
  cleaners.add(cleaner)
}

function clear() {
  cleaners.forEach((cleaner) => cleaner())
  controller.abort()
  controller = new AbortController()
  pending = null
  checkingIdentity = null
  checkingHome = null
  knownIdentity.value = null
  identityState.value = 'checking'
  workspaceState.value = 'checking'
  user.value = null
  thread.value = null
  conversations.value = []
  runHistory.reset()
  checkingRuns.value = false
  directoryRead += 1
  workspaces.value = []
  checked.value = false
  listed.value = false
  loading.value = false
  problem.value = ''
}

function signal() {
  return controller.signal
}

// 401 时清空私密内容，但保留提示，让来访者知道为什么回到了登录框。
function fail(error: unknown, own = signal()) {
  if (own.aborted) return
  const message = explain(error)
  if (error instanceof ApiFailure && error.status === 401) {
    clear()
    identityState.value = 'anonymous'
    checked.value = true
  }
  problem.value = message
}

function upsert(conversation: Conversation) {
  const index = conversations.value.findIndex((item) => item.id === conversation.id)
  if (index === -1) conversations.value.push(conversation)
  else conversations.value[index] = conversation
}

// 身份确认只读登录，不触碰书信、交办或运行记录。
function confirmIdentity() {
  if (checkingIdentity) return checkingIdentity
  const own = signal()
  const task = api
    .me(own)
    .then((identity) => {
      if (own.aborted) return null
      if (knownIdentity.value && knownIdentity.value.id !== identity.id) clear()
      knownIdentity.value = identity
      identityState.value = 'signed-in'
      return identity
    })
    .catch((error: unknown) => {
      if (own.aborted) return null
      if (error instanceof ApiFailure && error.status === 401) {
        if (knownIdentity.value || user.value) clear()
        identityState.value = 'anonymous'
        checked.value = true
        return null
      }
      identityState.value = 'unavailable'
      throw error
    })
    .finally(() => {
      if (checkingIdentity === task) checkingIdentity = null
    })
  checkingIdentity = task
  return task
}

async function refresh(initialThreads?: Thread[]) {
  const current = thread.value
  if (!current) return
  const own = signal()
  const mine = ++directoryRead
  void runHistory.read()
  void readWorkspaces().catch(() => null)
  try {
    const [threads, list] = await Promise.all([
      initialThreads ?? api.threads(own),
      api.conversations(current.id, own),
    ])
    if (own.aborted || mine !== directoryRead) return
    thread.value = threads.find((item) => item.id === current.id) ?? null
    conversations.value = list
    listed.value = true
  } catch (error) {
    if (!own.aborted && mine === directoryRead) throw error
  }
}

// 交办页自己确认一次权限：这里读不到要说出来，不能当成没有权限。
// 回来时已经有更晚发出的读取，就返回 null：这份结论过时了，调用方要再读一次，不能拿它下判断。
async function readWorkspaces() {
  const own = signal()
  const mine = ++workspaceRead
  try {
    const list = await api.workspaces(own)
    if (own.aborted || mine !== workspaceRead) return null
    workspaces.value = list
    workspaceState.value = 'ready'
    return list
  } catch (error) {
    if (!own.aborted && mine === workspaceRead) {
      if (error instanceof ApiFailure && error.status === 401) {
        clear()
        identityState.value = 'anonymous'
        checked.value = true
      } else {
        workspaceState.value = 'unavailable'
      }
    }
    throw error
  }
}

function refreshHomeAccess() {
  if (checkingHome) return checkingHome
  // 先作废显示结论；新身份确认后要重新读这一轮的资格。
  workspaceRead += 1
  identityState.value = 'checking'
  workspaceState.value = 'checking'
  const task = confirmIdentity()
    .then(async (identity) => {
      if (identity && identity === knownIdentity.value) await readWorkspaces()
    })
    .catch(() => {
      // 身份和资格各自留下失败状态，公开页面仍可使用。
    })
    .finally(() => {
      if (checkingHome === task) checkingHome = null
    })
  checkingHome = task
  return task
}

async function identify() {
  let own = signal()
  loading.value = true
  problem.value = ''
  try {
    const identity = await confirmIdentity()
    if (!identity || identity !== knownIdentity.value) return
    // 确认时若换了账号，旧信号已取消；接下来的数据属于新账号。
    own = signal()
    loading.value = true
    user.value = identity
    const threads = await api.threads(own)
    if (own.aborted) return
    thread.value = threads[0] ?? null
    if (!thread.value) throw new Error('Missing thread')
    await refresh(threads)
  } catch (error) {
    fail(error, own)
  } finally {
    if (!own.aborted) {
      checked.value = true
      loading.value = false
    }
  }
}

// 多个视图同时挂载时共用同一次读取。
function load() {
  if (pending) return pending
  const task = identify().finally(() => {
    if (pending === task) pending = null
  })
  pending = task
  return task
}

// 回到前台时先确认还是同一个人登录着；不是就清空，交给页面重新显示登录。
async function verify() {
  const previous = user.value
  if (!previous) return false
  const own = signal()
  try {
    const identity = await confirmIdentity()
    if (!identity || identity !== knownIdentity.value) return false
    if (identity.id !== previous.id) {
      void load()
      return false
    }
    return !own.aborted
  } catch (error) {
    fail(error, own)
    return false
  }
}

// 回到私密页面时先显示内存里的内容，再在后台悄悄更新，不显示「正在读取」；失败才提示。
async function revalidate() {
  const own = signal()
  try {
    if (!(await verify()) || own.aborted) return
    await refresh()
    if (!own.aborted) problem.value = ''
  } catch (error) {
    fail(error, own)
  }
}

async function reload() {
  const own = signal()
  loading.value = true
  problem.value = ''
  try {
    if (!(await verify()) || own.aborted) return
    await refresh()
  } catch (error) {
    fail(error, own)
  } finally {
    if (!own.aborted) loading.value = false
  }
}

async function ensureChat() {
  if (!thread.value) return null
  const own = signal()
  const chat = await api.chat(thread.value.id, own)
  if (own.aborted) return null
  upsert(chat)
  return chat
}

async function reloadRuns() {
  if (!thread.value || runsLoading.value) return
  const own = signal()
  checkingRuns.value = true
  try {
    if (!(await verify()) || own.aborted) return
    await runHistory.read()
  } finally {
    if (!own.aborted) checkingRuns.value = false
  }
}

async function createLetter(title: string | null, key: string) {
  if (!thread.value) return null
  const own = signal()
  const letter = await api.createLetter(thread.value.id, title, key, own)
  if (own.aborted) return null
  upsert(letter)
  return letter
}

async function logout() {
  const own = signal()
  loading.value = true
  problem.value = ''
  try {
    await api.logout(own)
    if (own.aborted) return
    clear()
    identityState.value = 'anonymous'
    checked.value = true
  } catch (error) {
    fail(error, own)
  } finally {
    if (!own.aborted) loading.value = false
  }
}

// 状态只由本模块的函数修改，视图只读取。
export function usePrivateSession() {
  return {
    user,
    thread,
    conversations,
    runs,
    runsKnown,
    runsLoading,
    runsProblem,
    homeAccess,
    refreshHomeAccess,
    checked,
    listed,
    loading,
    problem,
    signal,
    fail,
    load,
    reload,
    reloadRuns,
    revalidate,
    verify,
    ensureChat,
    createLetter,
    readWorkspaces,
    logout,
    clear,
    onClear,
  }
}
