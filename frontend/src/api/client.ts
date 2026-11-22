import type { components, operations } from './schema'
import { downloadApiFile } from './download'

export type User = components['schemas']['UserView']
export type Thread = components['schemas']['ThreadView']
export type Message = components['schemas']['MessageView']
export type Run = components['schemas']['RunView']
export type Schedule = components['schemas']['ScheduleView']
export type Conversation = components['schemas']['ConversationView']
export type ConversationMessage = components['schemas']['ConversationMessageView']
export type Activity = components['schemas']['ActivityView']
export type ReplyQueue = components['schemas']['ReplyQueueView']
export type ReplyService = components['schemas']['ReplyServiceView']
export type MessageDecision = components['schemas']['MessageDecisionView']
export type ReplySummary = components['schemas']['ReplySummary']
export type Reception = components['schemas']['ReceptionView']
export type Revisit = components['schemas']['RevisitView']
export type RevisitSummary = components['schemas']['RevisitSummary']
export type WorkDecision = components['schemas']['WorkDecisionView']
export type Knock = components['schemas']['KnockView']
export type Workspace = components['schemas']['WorkspaceView']
export type WorkSession = components['schemas']['WorkSessionView']
export type WorkTask = components['schemas']['WorkTaskView']
export type WorkInput = components['schemas']['WorkInputView']
export type WorkResult = components['schemas']['WorkTurnResultView']
export type WorkArtifact = components['schemas']['WorkArtifactView']
export type WorkDeliverable = components['schemas']['WorkDeliverableView']
export type WorkEventPage = components['schemas']['WorkEventPage']
export type WorkEvent = WorkEventPage['events'][number]
export type Maintenance = components['schemas']['MaintenanceView']
export type MaintenanceCapacity = components['schemas']['CapacityFact']
export type MaintenanceBackup = components['schemas']['BackupFact']
export type MaintenanceState = Maintenance['state']
export type MaintenanceReason = NonNullable<MaintenanceCapacity['reason']>
export type PublicNoteSummary = components['schemas']['PublicNoteSummary']
export type PublicNoteDetail = components['schemas']['PublicNoteDetail']
export type PublicNotePage = components['schemas']['PublicNotePage']
export type PublicWorkSummary = components['schemas']['PublicWorkSummary']
export type PublicWorkDetail = components['schemas']['PublicWorkDetail']
export type PublicWorkPage = components['schemas']['PublicWorkPage']
export type PublicWorkCover = components['schemas']['PublicWorkCover']
export type PublicWorkLink = components['schemas']['PublicWorkLink']
export type PublicWorkFile = components['schemas']['PublicWorkFile']

export type WorkReceipt =
  components['schemas']['AvailableWorkReceipt'] | components['schemas']['UnreadableWorkReceipt']
export type WorkAttachment = components['schemas']['WorkAttachmentView']
export type WorkAttachmentList = components['schemas']['WorkAttachmentList']
type WorkAttachmentArchive = components['schemas']['WorkAttachmentArchive']
type WorkTaskHistoryPage = components['schemas']['WorkTaskHistoryPage']
export type WorkStatusGroup = NonNullable<
  NonNullable<
    operations['task_history_api_work_sessions__session_id__task_history_get']['parameters']['query']
  >['status_group']
>
type WorkInputPage = components['schemas']['WorkInputPage']
type WorkResultList = components['schemas']['WorkResultList']
type WorkArtifactList = components['schemas']['WorkArtifactList']
export type ConversationPage = components['schemas']['ConversationMessagePage']
type MessagePage = components['schemas']['MessagePage']
type Login = components['schemas']['LoginInput']
type Activation = components['schemas']['ActivateInput']

export class ApiFailure extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(code)
  }
}

const explanations: Record<string, string> = {
  login_invalid: '账号或密码不正确，请检查后再试。',
  invitation_invalid: '邀请无效、已使用或已过期，请向米洛要一份新邀请。',
  invalid_input: '请检查输入内容及长度。',
  auth_rate_limited: '尝试有点频繁，请过几分钟再来。',
  auth_busy: '登录服务繁忙，请稍后重试。',
  idempotency_conflict: '这次提交的标识发生冲突，请重新进入对话后再试。',
  database_busy: '收件服务繁忙，请稍后重试。',
  storage_error: '记录暂时无法读取或保存，请稍后重试。',
  conversation_not_found: '没有找到这封信或这段聊天，请回到列表重新打开。',
  origin_rejected: '请求来源不被允许，请从米洛在家的页面重新打开。',
  work_not_found: '这件事找不到了，或你已经没有权限。',
  work_attachment_busy: '附件下载正忙，请稍后重试。',
  work_attachment_unavailable: '附件现在取不回来，请稍后刷新交办单再试。',
  invalid_download: '收到的不是可下载的文件，请稍后重试。',
  work_input_conflict: '这件的状态已经变了，没能补上。刷新看看现在怎样了。',
  work_queue_full: '没结束的事太多了，先等几件做完再交。',
  work_inputs_full: '这件补的话已经太多了，先等它结束。',
  maintenance_unavailable: '维护摘要暂时无法核对，请稍后重试。',
  note_not_found: '这条近况当前不可读取。',
  work_asset_not_found: '这份文件当前不在作品的公开版本中。',
  work_asset_unavailable: '这份公开文件暂时无法读取，请稍后重试。',
  preview_read_only: '这是演示预览样本，暂不支持在这儿真正修改或叫停。',
}

export function explain(error: unknown): string {
  if (!(error instanceof ApiFailure)) return '暂时没有完成，请稍后重试。'
  if (error.status === 0) return '连接中断或等待超时，尚未确认结果。可以重试。'
  if (error.status === 401) return explanations[error.code] ?? '登录已失效，请重新登录。'
  return explanations[error.code] ?? '服务暂时不可用，请稍后重试。'
}

async function request<T>(
  path: string,
  signal: AbortSignal,
  body?: unknown,
  key?: string,
  method = body === undefined ? 'GET' : 'POST',
): Promise<T> {
  const headers = new Headers({ Accept: 'application/json' })
  if (body !== undefined) headers.set('Content-Type', 'application/json')
  if (key) headers.set('Idempotency-Key', key)
  let response: Response
  try {
    response = await fetch(`${import.meta.env.BASE_URL}api${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: 'same-origin',
      cache: 'no-store',
      signal: AbortSignal.any([signal, AbortSignal.timeout(15_000)]),
    })
  } catch {
    throw new ApiFailure(0, 'network')
  }
  if (response.status === 204) return undefined as T
  let data: unknown
  try {
    if (!response.headers.get('content-type')?.includes('application/json')) {
      throw new Error('Unexpected response format')
    }
    data = await response.json()
  } catch {
    throw new ApiFailure(response.ok ? 502 : response.status, 'invalid_response')
  }
  if (!response.ok) {
    const envelope = data as Partial<components['schemas']['ErrorEnvelope']> | null
    const code = typeof envelope?.error?.code === 'string' ? envelope.error.code : 'unknown'
    throw new ApiFailure(response.status, code)
  }
  return data as T
}

export const api = {
  schedule: (signal: AbortSignal) => request<Schedule>('/schedule', signal),
  me: (signal: AbortSignal) => request<User>('/auth/me', signal),
  login: (body: Login, signal: AbortSignal) => request<User>('/auth/login', signal, body),
  activate: (body: Activation, signal: AbortSignal) =>
    request<User>('/auth/accept-invite', signal, body),
  logout: (signal: AbortSignal) => request<void>('/auth/logout', signal, {}),
  threads: (signal: AbortSignal) => request<Thread[]>('/threads', signal),
  runs: (thread: string, signal: AbortSignal) =>
    request<Run[]>(`/threads/${encodeURIComponent(thread)}/runs`, signal),
  messages: (thread: string, after: number, signal: AbortSignal) =>
    request<MessagePage>(
      `/threads/${encodeURIComponent(thread)}/messages?after=${after}&limit=100`,
      signal,
    ),
  post: (thread: string, body: string, key: string, signal: AbortSignal) =>
    request<Message>(`/threads/${encodeURIComponent(thread)}/messages`, signal, { body }, key),
  // 会话层：一个 thread 下有多封书信和唯一的聊天，消息按会话读写。
  conversations: (thread: string, signal: AbortSignal) =>
    request<Conversation[]>(`/threads/${encodeURIComponent(thread)}/conversations`, signal),
  createLetter: (thread: string, title: string | null, key: string, signal: AbortSignal) =>
    request<Conversation>(
      `/threads/${encodeURIComponent(thread)}/conversations`,
      signal,
      { title },
      key,
    ),
  chat: (thread: string, signal: AbortSignal) =>
    request<Conversation>(
      `/threads/${encodeURIComponent(thread)}/chat`,
      signal,
      undefined,
      undefined,
      'PUT',
    ),
  conversationMessages: (conversation: string, after: number, signal: AbortSignal) =>
    request<ConversationPage>(
      `/conversations/${encodeURIComponent(conversation)}/messages?after=${after}&limit=100`,
      signal,
    ),
  // 从最新一条往回读：latest 取最新一页，before 取严格更早的一页；两者都按倒序返回。
  latestMessages: (conversation: string, signal: AbortSignal) =>
    request<ConversationPage>(
      `/conversations/${encodeURIComponent(conversation)}/messages?latest=true&limit=100`,
      signal,
    ),
  earlierMessages: (conversation: string, before: number, signal: AbortSignal) =>
    request<ConversationPage>(
      `/conversations/${encodeURIComponent(conversation)}/messages?before=${before}&limit=100`,
      signal,
    ),
  activity: (conversation: string, signal: AbortSignal) =>
    request<Activity>(`/conversations/${encodeURIComponent(conversation)}/activity`, signal),
  // 敲门只保存一声提示；请求体固定为空对象，重试沿用原键。
  knock: (conversation: string, key: string, signal: AbortSignal) =>
    request<Knock>(`/conversations/${encodeURIComponent(conversation)}/knocks`, signal, {}, key),
  postToConversation: (conversation: string, body: string, key: string, signal: AbortSignal) =>
    request<ConversationMessage>(
      `/conversations/${encodeURIComponent(conversation)}/messages`,
      signal,
      { body },
      key,
    ),
  // 交办：只给有工作权限的账号；没有权限时工作区列表是 []。
  workspaces: (signal: AbortSignal) => request<Workspace[]>('/workspaces', signal),
  mainSession: (workspace: string, signal: AbortSignal) =>
    request<WorkSession>(
      `/workspaces/${encodeURIComponent(workspace)}/main-session`,
      signal,
      {},
      undefined,
      'PUT',
    ),
  workTaskHistory: (
    session: string,
    statusGroup: WorkStatusGroup,
    before: number | null,
    signal: AbortSignal,
  ) => {
    const query = new URLSearchParams({ limit: '50', status_group: statusGroup })
    if (before !== null) query.set('before', String(before))
    return request<WorkTaskHistoryPage>(
      `/work-sessions/${encodeURIComponent(session)}/task-history?${query.toString()}`,
      signal,
    )
  },
  createWorkTask: (
    session: string,
    body: string,
    name: string | null,
    key: string,
    signal: AbortSignal,
  ) =>
    request<WorkTask>(
      `/work-sessions/${encodeURIComponent(session)}/tasks`,
      signal,
      { body, deliverable_name: name },
      key,
    ),
  workTask: (task: string, signal: AbortSignal) =>
    request<WorkTask>(`/work-tasks/${encodeURIComponent(task)}`, signal),
  workInputs: (task: string, after: number, signal: AbortSignal) =>
    request<WorkInputPage>(
      `/work-tasks/${encodeURIComponent(task)}/inputs?after=${after}&limit=50`,
      signal,
    ),
  appendWorkInput: (
    task: string,
    body: string,
    key: string,
    signal: AbortSignal,
    activeTurn: string | null,
  ) =>
    request<WorkInput>(
      `/work-tasks/${encodeURIComponent(task)}/inputs`,
      signal,
      activeTurn ? { body, active_turn: activeTurn } : { body },
      key,
    ),
  stopWorkTask: (task: string, key: string, signal: AbortSignal) =>
    request<WorkTask>(`/work-tasks/${encodeURIComponent(task)}/stop`, signal, {}, key),
  workResults: (task: string, signal: AbortSignal) =>
    request<WorkResultList>(`/work-tasks/${encodeURIComponent(task)}/results`, signal),
  workArtifacts: (task: string, signal: AbortSignal) =>
    request<WorkArtifactList>(`/work-tasks/${encodeURIComponent(task)}/artifacts`, signal),
  workReceipt: (task: string, signal: AbortSignal) =>
    request<WorkReceipt>(`/work-tasks/${encodeURIComponent(task)}/receipt`, signal),
  workAttachments: (task: string, signal: AbortSignal) =>
    request<WorkAttachmentList>(`/work-tasks/${encodeURIComponent(task)}/attachments`, signal),
  downloadWorkAttachment: (
    task: string,
    attachment: string,
    fallbackName: string,
    signal: AbortSignal,
  ) =>
    downloadApiFile(
      `/work-tasks/${encodeURIComponent(task)}/attachments/${encodeURIComponent(attachment)}/download`,
      signal,
      fallbackName,
      'application/octet-stream',
    ),
  downloadWorkArchive: (
    task: string,
    attachmentIds: WorkAttachmentArchive['attachment_ids'],
    fallbackName: string,
    signal: AbortSignal,
  ) =>
    downloadApiFile(
      `/work-tasks/${encodeURIComponent(task)}/attachments/archive`,
      signal,
      fallbackName,
      'application/zip',
      { attachment_ids: attachmentIds },
    ),
  workEvents: (task: string, after: number, signal: AbortSignal) =>
    request<WorkEventPage>(
      `/work-tasks/${encodeURIComponent(task)}/events?after=${after}&limit=50`,
      signal,
    ),
  // 本人只读维护摘要：未配置返回 404，不可核对返回 503，不触发扫描或维护动作。
  maintenance: (signal: AbortSignal) => request<Maintenance>('/maintenance', signal),
  // 公开近况与历史：匿名只读，按 sequence 严格倒序翻页；不写库、不触发模型。
  publicNotes: (limit: number, before: number | null, signal: AbortSignal) => {
    const query = new URLSearchParams({ limit: String(limit) })
    if (before !== null) query.set('before', String(before))
    return request<PublicNotePage>(`/notes?${query.toString()}`, signal)
  },
  publicNote: (noteId: string, signal: AbortSignal) =>
    request<PublicNoteDetail>(`/notes/${encodeURIComponent(noteId)}`, signal),
  // 公开作品与成果：匿名只读，按首次发布 sequence 倒序翻页；文件一律按附件下载。
  publicWorks: (limit: number, before: number | null, signal: AbortSignal) => {
    const query = new URLSearchParams({ limit: String(limit) })
    if (before !== null) query.set('before', String(before))
    return request<PublicWorkPage>(`/works?${query.toString()}`, signal)
  },
  publicWork: (workId: string, signal: AbortSignal) =>
    request<PublicWorkDetail>(`/works/${encodeURIComponent(workId)}`, signal),
  downloadPublicWorkFile: (downloadPath: string, fallbackName: string, signal: AbortSignal) =>
    downloadApiFile(downloadPath, signal, fallbackName, 'application/octet-stream'),
}
