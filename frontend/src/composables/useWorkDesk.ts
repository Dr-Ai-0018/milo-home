import { ref } from 'vue'
import { api, ApiFailure, explain } from '../api/client'
import type { Reception, WorkSession, WorkStatusGroup, WorkTask } from '../api/client'
import { resolveReceptionLifetime } from '../utils/noncompulsory'
import { forgetScrollPosition } from '../utils/scroll'
import { isUnfinishedDecision } from '../utils/work'
import { usePrivateSession } from './usePrivateSession'

const GROUP_META: Record<WorkStatusGroup, { label: string; hint: string; empty: string }> = {
  all: {
    label: '全部',
    hint: '全部状态的交办，最新交出的排在最前。',
    empty: '还没交过事。',
  },
  active: {
    label: '未结束',
    hint: '包含排着队、米洛在做、正在停下，以及暂缓或待澄清的交办。',
    empty: '眼下没有未结束的交办。',
  },
  needs_reconciliation: {
    label: '待核对',
    hint: '情况还不清楚、需要等米洛核对的交办。',
    empty: '目前没有需要核对的交办。',
  },
  finished: {
    label: '已结束',
    hint: '包含已结束、婉拒、出错、停下与到点的交办；各自状态标在单子上。',
    empty: '还没有已结束的交办。',
  },
}

export const WORK_STATUS_GROUPS: readonly { id: WorkStatusGroup; label: string }[] = [
  { id: 'all', label: GROUP_META.all.label },
  { id: 'active', label: GROUP_META.active.label },
  { id: 'needs_reconciliation', label: GROUP_META.needs_reconciliation.label },
  { id: 'finished', label: GROUP_META.finished.label },
]

export function statusGroupMeta(group: WorkStatusGroup) {
  return GROUP_META[group]
}

const activeStatuses = new Set<WorkTask['status']>(['queued', 'running', 'stopping'])
const finishedStatuses = new Set<WorkTask['status']>([
  'completed',
  'failed',
  'cancelled',
  'timed_out',
])

export function matchesStatusGroup(
  status: WorkTask['status'],
  group: WorkStatusGroup,
  decision?: WorkTask['decision'],
) {
  if (group === 'all') return true
  if (group === 'needs_reconciliation') return status === 'needs_reconciliation'
  if (status === 'completed' && isUnfinishedDecision(decision)) {
    return group === 'active'
  }
  if (group === 'active') return activeStatuses.has(status)
  return finishedStatuses.has(status)
}

// 交办桌：第一个工作区的主会话与当前分组下的已读交办。只放内存，
// 站内返回保留分组、已读页与焦点；退出、登录失效、换号或撤权时随私密状态一并清空。
const session = usePrivateSession()
const desk = ref<WorkSession | null>(null)
const reception = ref<Reception | null>(null)
const activeGroup = ref<WorkStatusGroup>('all')
const tasks = ref<WorkTask[]>([])
// 这次登录里确认过有没有工作权限；没确认前不能说「没有权限」。
const known = ref(false)
const listed = ref(false)
const groupLoaded = ref(false)
const loading = ref(false)
const loadingOlder = ref(false)
const nextBefore = ref<number | null>(null)
const problem = ref('')
const olderProblem = ref('')
const returnTargetId = ref<string | null>(null)
const returningFromDetail = ref(false)

let pending: Promise<void> | null = null
let listController: AbortController | null = null
let receptionExpiry: ReturnType<typeof setTimeout> | null = null
let requestTicket = 0
// 确认没有交办权限后加一：此前发出、还在路上的列表读取回来也不再写入。
let epoch = 0

function clearReceptionExpiry() {
  if (receptionExpiry !== null) {
    clearTimeout(receptionExpiry)
    receptionExpiry = null
  }
}

function syncDeskReception(raw: Reception | null | undefined) {
  clearReceptionExpiry()
  const resolved = resolveReceptionLifetime(raw)
  reception.value = resolved.reception
  if (resolved.reception && resolved.lifeMs > 0) {
    receptionExpiry = setTimeout(() => {
      receptionExpiry = null
      reception.value = null
    }, resolved.lifeMs)
  }
}

function resetDeskState(keepKnownListed = false) {
  listController?.abort()
  listController = null
  clearReceptionExpiry()
  requestTicket += 1
  desk.value = null
  reception.value = null
  activeGroup.value = 'all'
  tasks.value = []
  known.value = keepKnownListed
  listed.value = keepKnownListed
  groupLoaded.value = false
  loading.value = false
  loadingOlder.value = false
  nextBefore.value = null
  problem.value = ''
  olderProblem.value = ''
  pending = null
  returnTargetId.value = null
  returningFromDetail.value = false
  forgetScrollPosition('/work')
}

session.onClear(() => {
  resetDeskState(false)
})

function report(error: unknown, own: AbortSignal) {
  if (own.aborted) return
  if (error instanceof ApiFailure && error.status === 401) session.fail(error, own)
  else problem.value = explain(error)
}

function withdraw() {
  epoch += 1
  resetDeskState(true)
}

// 读到一份「最新发出」的工作区列表才下结论；被更晚的读取盖过就再读，最多三次。
async function confirmWorkspaces() {
  const own = session.signal()
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const list = await session.readWorkspaces()
    if (own.aborted) return null
    if (list) return list
  }
  throw new Error('Workspace check kept being superseded')
}

async function readLatest() {
  const targetGroup = activeGroup.value
  const own = session.signal()
  const controller = new AbortController()
  listController?.abort()
  listController = controller
  const signal = AbortSignal.any([own, controller.signal])
  const mine = epoch
  const myTicket = ++requestTicket
  const current = () =>
    !own.aborted &&
    !controller.signal.aborted &&
    mine === epoch &&
    myTicket === requestTicket &&
    activeGroup.value === targetGroup

  loading.value = true
  problem.value = ''
  try {
    let main = desk.value
    if (!main) {
      const list = await confirmWorkspaces()
      if (!list || !current()) return
      const [workspace] = list
      known.value = true
      if (!workspace) {
        withdraw()
        return
      }
      main = await api.mainSession(workspace.id, signal)
      if (!current()) return
      syncDeskReception(main.reception)
    }
    const page = await api.workTaskHistory(main.id, targetGroup, null, signal)
    if (!current()) return
    const last = page.tasks.at(-1)
    const connected =
      groupLoaded.value &&
      desk.value?.id === main.id &&
      !!last &&
      tasks.value.some((task) => task.id === last.id)
    const older = connected ? tasks.value.filter((task) => task.sequence < last.sequence) : []
    desk.value = main
    tasks.value = [...page.tasks, ...older]
    if (!older.length || page.next_before === null) nextBefore.value = page.next_before
    olderProblem.value = ''
    groupLoaded.value = true
    listed.value = true
  } catch (error) {
    if (!current()) return
    if (error instanceof ApiFailure && error.status === 404) {
      if ((await recheck()) && current()) report(error, signal)
    } else report(error, signal)
  } finally {
    if (listController === controller) listController = null
    if (myTicket === requestTicket) loading.value = false
  }
}

async function readOlder() {
  const main = desk.value
  const before = nextBefore.value
  const targetGroup = activeGroup.value
  if (!main || before === null || loadingOlder.value) return
  const own = session.signal()
  const controller = new AbortController()
  listController?.abort()
  listController = controller
  const signal = AbortSignal.any([own, controller.signal])
  const mine = epoch
  const myTicket = ++requestTicket
  const current = () =>
    !own.aborted &&
    !controller.signal.aborted &&
    mine === epoch &&
    myTicket === requestTicket &&
    desk.value?.id === main.id &&
    activeGroup.value === targetGroup

  loadingOlder.value = true
  olderProblem.value = ''
  try {
    if (!(await session.verify()) || !current()) return
    const page = await api.workTaskHistory(main.id, targetGroup, before, signal)
    if (!current()) return
    const seen = new Set(tasks.value.map((task) => task.id))
    tasks.value = [...tasks.value, ...page.tasks.filter((task) => !seen.has(task.id))]
    nextBefore.value = page.next_before
  } catch (error) {
    if (!current()) return
    if (error instanceof ApiFailure && error.status === 401) session.fail(error, own)
    else if (!(error instanceof ApiFailure && error.status === 404) || (await recheck())) {
      if (current()) olderProblem.value = explain(error)
    }
  } finally {
    if (listController === controller) listController = null
    if (myTicket === requestTicket) loadingOlder.value = false
  }
}

// 某件交办或列表被告知看不到时调用：重新确认这个账号还有没有工作区。确认没有就清掉列表和主会话，
// 返回 false；还有，或这次没确认成（只当暂时读不到，保留原样），返回 true。
async function recheck() {
  const own = session.signal()
  try {
    const list = await confirmWorkspaces()
    if (!list || own.aborted) return false
    if (list.length) return true
    withdraw()
    return false
  } catch (error) {
    if (error instanceof ApiFailure && error.status === 401) session.fail(error, own)
    return true
  }
}

function drop(taskId: string) {
  tasks.value = tasks.value.filter((item) => item.id !== taskId)
}

function singleRead(read: () => Promise<void>) {
  if (pending) return pending
  const task = read().finally(() => {
    if (pending === task) pending = null
  })
  pending = task
  return task
}

const load = () => singleRead(readLatest)
const loadOlder = () => readOlder()

async function selectGroup(group: WorkStatusGroup) {
  if (group === activeGroup.value) {
    if (!loading.value && (!groupLoaded.value || problem.value)) await load()
    return
  }
  listController?.abort()
  listController = null
  pending = null
  requestTicket += 1
  activeGroup.value = group
  tasks.value = []
  nextBefore.value = null
  groupLoaded.value = false
  loadingOlder.value = false
  problem.value = ''
  olderProblem.value = ''
  returnTargetId.value = null
  returningFromDetail.value = false
  await load()
}

// 回到列表：保留当前分组、已翻出页与位置，确认身份后轻刷新顶部一页。
async function revisit() {
  if (!(await session.verify())) return
  await load()
}

// 单件页读到的新状态顺手记回列表；若状态已经不属于当前分组，直接从本组移出。
function remember(task: WorkTask) {
  const index = tasks.value.findIndex((item) => item.id === task.id)
  const belongs = matchesStatusGroup(task.status, activeGroup.value, task.decision)
  if (index !== -1) {
    if (belongs) tasks.value[index] = task
    else tasks.value.splice(index, 1)
    return
  }
  if (
    belongs &&
    desk.value?.id === task.session_id &&
    task.sequence > (tasks.value[0]?.sequence ?? 0)
  ) {
    tasks.value.unshift(task)
  }
}

async function create(body: string, key: string, name: string | null = null) {
  if (!desk.value) return null
  const own = session.signal()
  const mine = epoch
  const task = await api.createWorkTask(desk.value.id, body, name, key, own).catch((error) => {
    if (error instanceof ApiFailure && error.status === 404) void recheck()
    throw error
  })
  if (own.aborted || mine !== epoch) return null
  remember(task)
  return task
}

function markOpened(taskId: string) {
  returnTargetId.value = taskId
  returningFromDetail.value = true
}

function clearReturnFlag() {
  returningFromDetail.value = false
}

export function useWorkDesk() {
  return {
    desk,
    reception,
    activeGroup,
    tasks,
    known,
    listed,
    groupLoaded,
    loading,
    loadingOlder,
    nextBefore,
    problem,
    olderProblem,
    returnTargetId,
    returningFromDetail,
    load,
    loadOlder,
    selectGroup,
    revisit,
    remember,
    drop,
    recheck,
    create,
    markOpened,
    clearReturnFlag,
    report: (error: unknown) => report(error, session.signal()),
  }
}
