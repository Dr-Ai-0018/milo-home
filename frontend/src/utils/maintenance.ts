import type {
  Maintenance,
  MaintenanceBackup,
  MaintenanceCapacity,
  MaintenanceReason,
  MaintenanceState,
} from '../api/client'
import { stampTime } from './time'
import { formatBytes } from './work'

export interface MaintenanceCapacityRow {
  kind: MaintenanceCapacity['kind']
  label: string
  state: MaintenanceState
  stateLabel: string
  usageLine: string
  compactValue: string
  ratio: number | null
  complete: boolean
  note: string
}

export interface MaintenanceBackupRow {
  kind: MaintenanceBackup['kind']
  label: string
  boundary: string
  state: MaintenanceState
  stateLabel: string
  successLine: string
  attemptLine: string
  note: string
}

export interface MaintenanceSummaryView {
  state: MaintenanceState
  stateLabel: string
  headline: string
  observedAt: number
  expired: boolean
  hasObservation: boolean
  capacity: MaintenanceCapacityRow[]
  backups: MaintenanceBackupRow[]
}

const STATE_LABEL: Record<MaintenanceState, string> = {
  normal: '正常',
  attention: '需要留意',
  blocked: '已受限',
  unknown: '状态未知',
}

const CAPACITY_LABEL: Record<MaintenanceCapacity['kind'], string> = {
  business: '信件与交办记录',
  attachments: '交办附件',
  public_assets: '公开作品文件',
  workspace: '工作区原件',
  journal_hot: '近期运行记录（热区）',
  journal_cold: '已封存历史记录（冷区）',
  native_mapping: '对话归属映射',
  native_history: '主对话历史',
  backups: '本地备份目录',
  development: '开发与调试资料',
}

const BACKUP_META: Record<MaintenanceBackup['kind'], { label: string; boundary: string }> = {
  daily: {
    label: '每日备份',
    boundary: '例行在线备份；本身不证明整套资料都已可独立恢复。',
  },
  paired: {
    label: '完整配套采集',
    boundary: '受控窗口内打包的整套一致资料。',
  },
  remote: {
    label: '远端取回核对',
    boundary: '从准确远端快照取回并核对过完整性。',
  },
  restoration: {
    label: '独立恢复演练',
    boundary: '独立目录核对过文件可恢复；不代表副本直接接管运行。',
  },
}

const BACKUP_ORDER: readonly MaintenanceBackup['kind'][] = [
  'daily',
  'paired',
  'remote',
  'restoration',
]

// 全部固定原因枚举翻译为克制的人话；绝不直接把原因码、路径或内部 ID 暴露给页面。
const REASON_COPY: Record<MaintenanceReason, string> = {
  unavailable: '目前缺少这项核对记录。',
  expired: '上次观察已过有效期，当前情况暂未重新确认。',
  incomplete: '仅做了有限范围统计，当前占用只作下界参考。',
  capacity_near: '已接近预设维护提醒线，需要留意后续增长。',
  capacity_full: '已达到当前运行准入上限，新轮次会暂受限制。',
  protected_capacity: '已达到受保护容量边界，需要单独安排维护扩容。',
  evidence_invalid: '这份核对记录无法校验，暂时不能采信。',
  busy: '上次维护检查时正有任务在运行，已自动避让暂缓。',
  last_attempt_failed: '最近一次尝试未成功，已保留此前最近一次成功时间。',
  not_associated: '每日任务已执行，但尚未与具体远端快照关联核对。',
  unclassified_files: '发现未分类的新文件，已按保护规则暂停自动打包。',
  insufficient_space: '本次预算或可用空间不足，已暂缓继续写入。',
}

const OUTCOME_LABEL: Record<MaintenanceBackup['last_outcome'], string> = {
  succeeded: '成功',
  failed: '失败',
  deferred: '已暂缓',
  unknown: '结果未知',
}

// 计算只读摘要的剩余有效毫秒数；无效、时钟回退或已过期返回 0。
export function resolveMaintenanceLifetimeMs(view: Maintenance, nowSeconds: number): number {
  if (
    typeof view.observed_at !== 'number' ||
    typeof view.expires_at !== 'number' ||
    view.expires_at <= view.observed_at ||
    nowSeconds < view.observed_at ||
    nowSeconds >= view.expires_at
  ) {
    return 0
  }
  return Math.max(0, view.expires_at - nowSeconds) * 1000
}

function describeCapacityUsage(item: MaintenanceCapacity): string {
  const used = typeof item.used_bytes === 'number' ? formatBytes(item.used_bytes) : null
  const limit = typeof item.limit_bytes === 'number' ? formatBytes(item.limit_bytes) : null
  const allocated =
    typeof item.allocated_bytes === 'number' ? formatBytes(item.allocated_bytes) : null

  if (used === null) {
    return allocated ? `磁盘占用约 ${allocated}` : '暂未统计出大小'
  }
  if (!item.complete) {
    return limit ? `至少 ${used}（仅作下界，参考线 ${limit}）` : `至少 ${used}（仅作下界）`
  }
  if (limit) {
    return allocated && allocated !== used
      ? `已用 ${used} / ${limit}（磁盘约 ${allocated}）`
      : `已用 ${used} / ${limit}`
  }
  return allocated && allocated !== used ? `已用 ${used}（磁盘约 ${allocated}）` : `已用 ${used}`
}

function describeCompactCapacity(item: MaintenanceCapacity): {
  compactValue: string
  ratio: number | null
} {
  const used = typeof item.used_bytes === 'number' ? item.used_bytes : null
  const limit =
    typeof item.limit_bytes === 'number' && item.limit_bytes > 0 ? item.limit_bytes : null
  const allocated = typeof item.allocated_bytes === 'number' ? item.allocated_bytes : null

  if (used === null) {
    return {
      compactValue: allocated !== null ? `约 ${formatBytes(allocated)}` : '未统计',
      ratio: null,
    }
  }
  const usedText = formatBytes(used)
  if (!item.complete) {
    return {
      compactValue: limit ? `至少 ${usedText} / ${formatBytes(limit)}` : `至少 ${usedText}`,
      ratio: null,
    }
  }
  if (limit) {
    return {
      compactValue: `${usedText} / ${formatBytes(limit)}`,
      ratio: Math.min(1, Math.max(0, used / limit)),
    }
  }
  const diskSuffix =
    allocated !== null && formatBytes(allocated) !== usedText
      ? `（磁盘 ${formatBytes(allocated)}）`
      : ''
  return {
    compactValue: `${usedText}${diskSuffix}`,
    ratio: null,
  }
}

function describeCapacityNote(item: MaintenanceCapacity, expired: boolean): string {
  if (expired) return REASON_COPY.expired
  if (item.reason) return REASON_COPY[item.reason]
  if (!item.complete) return REASON_COPY.incomplete
  return ''
}

function describeBackupRow(
  kind: MaintenanceBackup['kind'],
  item: MaintenanceBackup | undefined,
  expired: boolean,
  nowSeconds: number,
): MaintenanceBackupRow {
  const meta = BACKUP_META[kind]
  if (!item) {
    return {
      kind,
      label: meta.label,
      boundary: meta.boundary,
      state: 'unknown',
      stateLabel: STATE_LABEL.unknown,
      successLine: '尚无核对记录',
      attemptLine: '',
      note: expired ? REASON_COPY.expired : REASON_COPY.unavailable,
    }
  }

  const evidenceStale =
    !expired &&
    typeof item.fresh_until === 'number' &&
    (nowSeconds >= item.fresh_until || nowSeconds < item.fresh_until - 365 * 86400 * 10)

  const state: MaintenanceState = expired
    ? 'unknown'
    : evidenceStale && item.state === 'normal'
      ? 'attention'
      : item.state

  const successLine =
    typeof item.last_success_at === 'number'
      ? `最近成功：${stampTime(item.last_success_at)}`
      : '尚无成功记录'

  let attemptLine = ''
  if (typeof item.last_attempt_at === 'number') {
    const newerThanSuccess =
      typeof item.last_success_at !== 'number' || item.last_attempt_at > item.last_success_at
    const showAttempt = item.last_outcome !== 'succeeded' || newerThanSuccess
    if (showAttempt) {
      attemptLine = `最近尝试：${stampTime(item.last_attempt_at)}（${OUTCOME_LABEL[item.last_outcome]}）`
    }
  } else if (item.last_outcome === 'failed' || item.last_outcome === 'deferred') {
    attemptLine = `最近尝试：${OUTCOME_LABEL[item.last_outcome]}`
  }

  const notes: string[] = []
  if (expired) {
    notes.push(REASON_COPY.expired)
  } else {
    if (item.reason) notes.push(REASON_COPY[item.reason])
    if (evidenceStale) notes.push('这项备份核对已超过建议复查时间。')
  }

  return {
    kind,
    label: meta.label,
    boundary: meta.boundary,
    state,
    stateLabel: STATE_LABEL[state],
    successLine,
    attemptLine,
    note: notes.join(' '),
  }
}

function worstState(states: readonly MaintenanceState[]): MaintenanceState {
  if (states.includes('blocked')) return 'blocked'
  if (states.includes('attention')) return 'attention'
  if (states.includes('unknown')) return 'unknown'
  return 'normal'
}

function buildHeadline(
  state: MaintenanceState,
  expired: boolean,
  hasObservation: boolean,
  capacity: MaintenanceCapacityRow[],
  backups: MaintenanceBackupRow[],
  raw: Maintenance,
): string {
  if (!hasObservation) return '暂无维护观察记录，尚不能确认容量与备份情况。'
  if (expired) return '上次维护记录已过期，保留旧参考值，当前状态未知。'

  const blockedCap = (raw.capacity ?? []).find((c) => c.state === 'blocked')
  if (blockedCap) {
    if (blockedCap.reason === 'protected_capacity') {
      return `${CAPACITY_LABEL[blockedCap.kind]}已达到受保护上限，需要单独维护处理。`
    }
    return `${CAPACITY_LABEL[blockedCap.kind]}已触及运行上限，新任务暂受限制。`
  }

  const failedBackup = (raw.backups ?? []).find((b) => b.last_outcome === 'failed')
  if (failedBackup) {
    return `最近一次${BACKUP_META[failedBackup.kind].label}尝试失败，需留意检查。`
  }

  const attentionCap = capacity.find((c) => c.state === 'attention')
  if (attentionCap) {
    return `${attentionCap.label}需要留意，展开可看明细。`
  }

  const attentionBackup = backups.find((b) => b.state === 'attention')
  if (attentionBackup) {
    return `${attentionBackup.label}需要留意，展开可看明细。`
  }

  if (state === 'unknown') {
    const dailyOk = (raw.backups ?? []).some(
      (b) => b.kind === 'daily' && b.state === 'normal' && b.last_outcome === 'succeeded',
    )
    const hasUnknownBackup = backups.some((b) => b.kind !== 'daily' && b.state === 'unknown')
    if (dailyOk && hasUnknownBackup) {
      return '每日备份近期正常，部分备份或恢复记录仍待核对。'
    }
    const incompleteCap = (raw.capacity ?? []).find((c) => !c.complete)
    if (incompleteCap) {
      return `${CAPACITY_LABEL[incompleteCap.kind]}仅作有限扫描，当前状态未完全确认。`
    }
    return '部分容量或备份状态暂未知，展开可看已记录项。'
  }

  return '资料容量与备份恢复记录目前正常。'
}

// 把原始 MaintenanceView 结合当前时间（支持本地过期降级）转换为页面只读视图模型。
export function buildMaintenanceSummary(
  raw: Maintenance,
  nowSeconds: number,
  forceExpired = false,
): MaintenanceSummaryView {
  const rawCapacity = raw.capacity ?? []
  const rawBackups = raw.backups ?? []
  const hasObservation = rawCapacity.length > 0 || rawBackups.length > 0
  const expired =
    forceExpired || (hasObservation && resolveMaintenanceLifetimeMs(raw, nowSeconds) <= 0)

  const capacity: MaintenanceCapacityRow[] = rawCapacity.map((item) => {
    const itemState: MaintenanceState = expired ? 'unknown' : item.state
    const { compactValue, ratio } = describeCompactCapacity(item)
    return {
      kind: item.kind,
      label: CAPACITY_LABEL[item.kind],
      state: itemState,
      stateLabel: STATE_LABEL[itemState],
      usageLine: describeCapacityUsage(item),
      compactValue,
      ratio,
      complete: item.complete,
      note: describeCapacityNote(item, expired),
    }
  })

  const backupMap = new Map(rawBackups.map((item) => [item.kind, item]))
  const backups: MaintenanceBackupRow[] = hasObservation
    ? BACKUP_ORDER.map((kind) => describeBackupRow(kind, backupMap.get(kind), expired, nowSeconds))
    : []

  const effectiveState: MaintenanceState =
    !hasObservation || expired
      ? 'unknown'
      : worstState([raw.state, ...capacity.map((c) => c.state), ...backups.map((b) => b.state)])

  return {
    state: effectiveState,
    stateLabel: STATE_LABEL[effectiveState],
    headline: buildHeadline(effectiveState, expired, hasObservation, capacity, backups, raw),
    observedAt: raw.observed_at,
    expired,
    hasObservation,
    capacity,
    backups,
  }
}
