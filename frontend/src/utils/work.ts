import type { WorkDecision, WorkEvent, WorkInput, WorkResult, WorkTask } from '../api/client'
import { describeRevisit } from './noncompulsory'

// 交办页的文案只说记录里有的：「结束」不等于「做对了」，不知道就说不知道。
// 状态映射严格对齐后端工作任务与结果契约，不把保存或过程结束误写成执行成功。

type TaskStatus = WorkTask['status']
type Delivery = WorkInput['delivery']
type Outcome = WorkResult['outcome']

// 真实执行中、叫停、故障与待核对不能被接单决定盖住。
const EXECUTION_PRIORITY_STATUSES = new Set<TaskStatus>([
  'running',
  'stopping',
  'failed',
  'cancelled',
  'timed_out',
  'needs_reconciliation',
])

export interface EffectiveTaskCopy {
  line: string
  note?: string
  receipt?: string
  toneKey: string
}

export function isUnfinishedDecision(decision: WorkDecision | null | undefined) {
  return decision?.state === 'deferred' || decision?.state === 'needs_clarification'
}

export function canClarifyInPlace(task: WorkTask | null) {
  return (
    !!task &&
    !EXECUTION_PRIORITY_STATUSES.has(task.status) &&
    task.decision?.state === 'needs_clarification'
  )
}

export function effectiveTaskCopy(task: WorkTask): EffectiveTaskCopy {
  const base = taskCopy[task.status]
  const decision = task.decision
  if (!decision || EXECUTION_PRIORITY_STATUSES.has(task.status)) {
    return { line: base.line, note: base.note, toneKey: task.status }
  }
  const receipt = decision.receipt.trim() || undefined
  if (decision.state === 'deferred') {
    const revisitLine = describeRevisit(decision.revisit)
    return {
      line: '米洛暂缓处理这件',
      note: revisitLine ? `${revisitLine}；暂未开始执行。` : '这件已留待稍后再看，暂未开始执行。',
      receipt,
      toneKey: 'deferred',
    }
  }
  if (decision.state === 'declined') {
    return {
      line: '米洛婉拒了这件交办',
      note: '本次接单已结束；如调整后想重新交给米洛，可以明确另开一件交办。',
      receipt,
      toneKey: 'declined',
    }
  }
  if (decision.state === 'needs_clarification') {
    return {
      line: '米洛想先问清楚，再继续做',
      note: '直接在这张原单下面补充说明就行，不需要另开新单。',
      receipt,
      toneKey: 'needs_clarification',
    }
  }
  return { line: base.line, note: base.note, toneKey: task.status }
}

export function effectiveTaskStamp(task: WorkTask): StampSpec {
  const decision = task.decision
  if (decision && !EXECUTION_PRIORITY_STATUSES.has(task.status)) {
    if (decision.state === 'deferred') return { kind: 'rect', label: '暂缓', tone: 'ink' }
    if (decision.state === 'declined') return { kind: 'round', label: '婉拒' }
    if (decision.state === 'needs_clarification') {
      return { kind: 'rect', label: '待澄清', tone: 'stamp' }
    }
  }
  return stampFor(task.status)
}

export const taskCopy: Record<TaskStatus, { line: string; note?: string }> = {
  queued: { line: '排着队，还没开始', note: '可以补几句，也可以停掉。' },
  running: { line: '米洛在做' },
  stopping: { line: '正在停下', note: '停下以后这里会写明；停止不撤销已经做过的改动。' },
  completed: { line: '这次处理结束了', note: '只说明过程结束，没检查过结果对不对。' },
  failed: { line: '没做成，中途出错停了' },
  cancelled: { line: '停下了', note: '停之前做过的改动还在。' },
  timed_out: { line: '到点停了', note: '一次最多做一小时，到点就停。' },
  needs_reconciliation: {
    line: '情况还不清楚，等米洛核对',
    note: '先别重新交一遍，可能已经做过一部分。',
  },
}

export const deliveryCopy: Record<Delivery, string> = {
  queued: '排着，还没递过去',
  sending: '正在递过去',
  acknowledged: '米洛那边收下了（收下不代表已经照做）',
  unknown: '不确定递到没有，先别重写一遍',
  cancelled: '没递出去就取消了',
  conflict: '没能递上：任务状态已经变了',
}

const sameTurnCopy: Partial<Record<Delivery, string>> = {
  queued: '已存下，等递给这一轮',
  acknowledged: '这一轮收下了，是否照做要看结果',
  cancelled: '这一轮没收到，这段没有发出',
  conflict: '执行端没有收下这段补话',
}

export function inputDeliveryLine(input: WorkInput, delivery = input.delivery) {
  return (input.active_turn && sameTurnCopy[delivery]) || deliveryCopy[delivery]
}

export const outcomeCopy: Record<Outcome, string> = {
  completed: '这一轮结束了',
  failed: '这一轮出错停了',
  interrupted: '这一轮被打断了',
}

// 还会变的状态：前台定时跟读。情况不清要等人工核对，不靠定时读。
const moving = new Set<TaskStatus>(['queued', 'running', 'stopping'])

export function isMoving(task: WorkTask | null) {
  return !!task && moving.has(task.status)
}

export function answerLine(result: WorkResult) {
  const { answer } = result.output
  if (answer.state === 'available') return null
  if (answer.state === 'missing') {
    return answer.reason === 'not_recorded'
      ? '这一轮是旧记录，没存下答复'
      : '这一轮没有留下明确的答复'
  }
  return {
    ambiguous: '这一轮的答复有两种说法，对不上，先不显示',
    too_long: '答复太长，这里放不下，先不显示',
    sensitive: '答复里可能有不该公开的内容，先不显示',
  }[answer.reason]
}

export function actionsLine(result: WorkResult) {
  const { actions } = result.output
  if (!actions) return null
  return `工具记录：命令 ${actions.command_calls} 次，文件编辑工具 ${actions.file_change_calls} 次`
}

// 交办内容的第一行当标题；正文本身是不可信纯文本，只做文本绑定。
export function taskTitle(task: WorkTask) {
  const first = task.initial_input.body.trim().split('\n', 1)[0] ?? ''
  return first.length > 40 ? `${first.slice(0, 40)}…` : first
}

export function sheetNumber(task: WorkTask) {
  return `No.${String(task.sequence).padStart(3, '0')}`
}

// 哪一段话：交办时那段，或者补的第几段。
export function inputName(inputs: WorkInput[], id: string) {
  const input = inputs.find((item) => item.id === id)
  if (!input) return '一段话'
  if (input.source === 'revisit') return '安排好的回访'
  if (input.kind === 'initial') return '交办的那段'
  const order =
    inputs
      .filter((item) => item.kind === 'supplement' && item.source !== 'revisit')
      .indexOf(input) + 1
  return `补的第 ${order} 段`
}

export type StampSpec =
  | { kind: 'rect'; label: string; tone: 'ink' | 'stamp' }
  | { kind: 'round'; label: string }
  | { kind: 'pencil' }

const verdicts: Partial<Record<TaskStatus, string>> = {
  completed: '结束',
  failed: '出错',
  cancelled: '停下了',
  timed_out: '到点',
}

// 一条记录盖什么章；递话的过程不盖章，只写进文字记录。
export function stampFor(status: TaskStatus): StampSpec
export function stampFor(event: WorkEvent): StampSpec | null
export function stampFor(input: WorkEvent | TaskStatus): StampSpec | null {
  if (typeof input === 'string') return statusStamp(input)
  if (input.kind === 'task_status') return statusStamp(input.state)
  if (input.kind === 'turn_result') {
    const label = { completed: '一轮完', failed: '一轮错', interrupted: '打断' }[input.state]
    return { kind: 'rect', label, tone: 'ink' }
  }
  return null
}

function statusStamp(status: TaskStatus): StampSpec {
  if (status === 'needs_reconciliation') return { kind: 'pencil' }
  const verdict = verdicts[status]
  if (verdict) return { kind: 'round', label: verdict }
  if (status === 'stopping') return { kind: 'rect', label: '叫停', tone: 'stamp' }
  return { kind: 'rect', label: status === 'running' ? '开工' : '收件', tone: 'ink' }
}

export function eventLine(event: WorkEvent, inputs: WorkInput[]) {
  if (event.kind === 'task_status') return `这件：${taskCopy[event.state].line}`
  if (event.kind === 'input_delivery') {
    const input = inputs.find((item) => item.id === event.input_id)
    const line = input ? inputDeliveryLine(input, event.state) : deliveryCopy[event.state]
    return `${inputName(inputs, event.input_id)}：${line}`
  }
  return `${inputName(inputs, event.input_id)}：${outcomeCopy[event.state]}`
}

export function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  const kib = bytes / 1024
  if (kib < 1024) return `${kib < 10 ? kib.toFixed(1) : Math.round(kib)} KiB`
  const mib = kib / 1024
  return `${mib < 10 ? mib.toFixed(1) : mib.toFixed(1)} MiB`
}

export function shortDigest(sha256: string) {
  return sha256.length > 16 ? `${sha256.slice(0, 8)}…${sha256.slice(-6)}` : sha256
}
