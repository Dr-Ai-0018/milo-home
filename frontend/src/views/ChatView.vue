<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { ApiFailure } from '../api/client'
import ChatComposer from '../components/ChatComposer.vue'
import ChatSlips from '../components/ChatSlips.vue'
import ConversationStatus from '../components/ConversationStatus.vue'
import KnockDoor from '../components/KnockDoor.vue'
import PrivateGate from '../components/PrivateGate.vue'
import ScheduleNote from '../components/ScheduleNote.vue'
import { useActivity } from '../composables/useActivity'
import { useChatOutbox } from '../composables/useChatOutbox'
import { useChatPolling } from '../composables/useChatPolling'
import { useChatViewport } from '../composables/useChatViewport'
import { useConversation } from '../composables/useConversation'
import { usePrivateScope } from '../composables/usePrivateScope'
import { usePrivateSession } from '../composables/usePrivateSession'
import { describeConversationStatus, emptyConversationStatus } from '../utils/activity'
import '../styles/chat.css'

const session = usePrivateSession()
const { thread } = session
const chatId = ref<string | null>(null)
const opening = ref(false)
const openProblem = ref('')
const refreshing = ref(false)
const scope = usePrivateScope(() => chatId.value)

const { messages, loading, settled, hasEarlier, behind, problem, earlier, catchUp, post, report } =
  useConversation(() => chatId.value)
const {
  processing,
  latestReply,
  replyQueue,
  replyService,
  replySummary,
  reception,
  revisits,
  fresh,
  attempted,
  read: readActivity,
  lose,
  forget,
} = useActivity(() => chatId.value)
const outbox = useChatOutbox(post, (error) => session.fail(error))

// 首次打开读最新一页，落到最新的纸条；之后只在本来就看着末尾时跟到底。更早的往上翻再读。
let landed: string | null = null
// 第一次进来、内存里还没有纸条时，读完再一次摆出来，不先画顶部再跳到底。
const gathering = ref(false)
const slips = ref<InstanceType<typeof ChatSlips> | null>(null)
const viewport = useChatViewport({
  conversationId: () => chatId.value,
  hasMessages: () => !!messages.value.length,
  hold: () => slips.value?.hold(),
})

async function tick() {
  const own = session.signal()
  const id = chatId.value
  const active = scope.capture()
  const inactive = () => !active() || document.hidden
  if (inactive()) return
  try {
    const first = landed !== id
    const finishRead = viewport.prepareRead(first)
    const before = messages.value.length
    gathering.value = first && !before
    try {
      await catchUp()
    } finally {
      gathering.value = false
    }
    if (inactive()) return
    landed = id
    await finishRead(messages.value.length > before)
    if (inactive()) return
    // 结果不确定的纸条用原键再递一次：存过的会原样返回，不会重复。
    await Promise.all(
      outbox.slips.value
        .filter((slip) => slip.state === 'unknown')
        .map((slip) => outbox.retry(slip.id)),
    )
    problem.value = ''
  } catch (error) {
    if (inactive()) return
    lose()
    if (error instanceof ApiFailure && error.status === 401) {
      polling.stop()
      session.fail(error, own)
      return
    }
    report(error)
    throw error
  }
  if (inactive()) return
  try {
    await readActivity()
  } catch (error) {
    if (inactive()) return
    lose()
    if (error instanceof ApiFailure && error.status === 401) session.fail(error, own)
  }
}

const polling = useChatPolling(tick, () => session.verify())

// 取得唯一的聊天会话（不发消息、不叫醒米洛），然后开始前台刷新。
async function open() {
  if (!thread.value || opening.value) return
  // 列表里已经有聊天就直接用，不必每次进来都再取一次。
  const known = session.conversations.value.find((item) => item.kind === 'chat')
  if (known) {
    chatId.value = known.id
    polling.start()
    return
  }
  opening.value = true
  openProblem.value = ''
  try {
    const chat = await session.ensureChat()
    if (!chat) return
    chatId.value = chat.id
    polling.start()
  } catch (error) {
    if (error instanceof ApiFailure && error.status === 401) session.fail(error)
    else openProblem.value = '聊天暂时打不开，请稍后点「重新读取」。'
  } finally {
    opening.value = false
  }
}

watch(
  thread,
  (current) => {
    if (current && !chatId.value) void open()
    if (!current) {
      refreshing.value = false
      polling.stop()
      outbox.clear()
      forget()
      chatId.value = null
    }
  },
  { immediate: true, flush: 'sync' },
)

async function refresh() {
  if (refreshing.value || opening.value || loading.value) return
  const own = session.signal()
  refreshing.value = true
  try {
    if (chatId.value) await polling.retry()
    else await open()
  } finally {
    if (!own.aborted) refreshing.value = false
  }
}

async function send(body: string) {
  const delivering = outbox.send(body)
  await viewport.revealSent()
  await delivering
}

// 连不上时只说连接；处理与排队情况等连上再说，免得几句「看不清」叠在一起。
const status = computed(() =>
  chatId.value && !polling.failures.value
    ? describeConversationStatus({
        subject: '这段聊天',
        unit: '条留言',
        processing: processing.value,
        replyQueue: replyQueue.value,
        replyService: replyService.value,
        replySummary: replySummary.value,
        reception: reception.value,
        revisits: revisits.value,
        fresh: fresh.value,
        showUnknown: attempted.value,
      })
    : emptyConversationStatus(),
)

const trouble = computed(() => !!(openProblem.value || problem.value || session.problem.value))
const finding = computed(
  () =>
    !trouble.value &&
    (opening.value ||
      gathering.value ||
      !chatId.value ||
      (loading.value && !messages.value.length)),
)
const empty = computed(
  () =>
    !!chatId.value &&
    !trouble.value &&
    !finding.value &&
    !messages.value.length &&
    !outbox.slips.value.length,
)

const connectionLine = computed(() =>
  polling.failures.value ? '暂时连不上，会隔一会儿自动再试，也可以点「重新读取」。' : '',
)

// 就地重读成功会移除错误条：仅在该按钮仍拥有焦点时交回稳定标题。
watch([openProblem, problem, connectionLine], async () => {
  const button = document.activeElement
  if (!(button instanceof HTMLElement) || !button.matches('.chat-inline-retry')) return
  const active = scope.capture()
  await nextTick()
  if (
    !active() ||
    document.hidden ||
    button.isConnected ||
    document.activeElement !== document.body
  )
    return
  document.querySelector<HTMLElement>('#private-title')?.focus({ preventScroll: true })
})
</script>

<template>
  <section
    class="chat-heading"
    :class="{ 'is-known': session.user.value || !session.checked.value }"
    aria-labelledby="chat-heading-title"
  >
    <div class="knock-row">
      <h1
        id="chat-heading-title"
        class="overprint"
      >
        传纸条
      </h1>
      <KnockDoor
        v-if="chatId"
        :conversation-id="chatId"
      />
    </div>
    <!-- 常来的人只留一句不保证马上回，作息收成一枚章；第一次来看完整说明。 -->
    <p
      v-if="session.user.value"
      class="chat-known"
    >
      <ScheduleNote stamp />
      <span>纸条会存好，等米洛来读；不保证马上回。</span>
    </p>
    <template v-else-if="session.checked.value">
      <p class="chat-intro">短短几句也行。纸条会存好，等米洛来读；不保证马上回。</p>
      <ScheduleNote compact />
    </template>
  </section>

  <div class="chat-workspace">
    <PrivateGate @reload="refresh">
      <h2
        id="private-title"
        class="visually-hidden"
        tabindex="-1"
      >
        和米洛的纸条
      </h2>
      <p
        v-if="finding"
        class="session-loading chat-loading"
        role="status"
      >
        正在找回纸条…
      </p>
      <p
        v-else-if="empty"
        class="chat-empty"
      >
        还没有纸条。写一句试试。
      </p>
      <ChatSlips
        v-if="!gathering"
        ref="slips"
        :messages="messages"
        :outbox="outbox.slips.value"
        :complete="settled && !behind && !problem"
        :has-earlier="hasEarlier"
        :reply-summary="replySummary"
        :fetch-earlier="earlier"
        @retry="outbox.retry"
        @discard="outbox.discard"
      />
      <ConversationStatus
        variant="card"
        :status="status"
        :latest-reply="latestReply"
        :connection-line="connectionLine"
        :show-inline-retry="!(openProblem || problem)"
        :retrying="opening || loading || refreshing"
        @retry="refresh"
      />
      <div
        v-if="openProblem || problem"
        class="feedback error chat-alert"
        role="alert"
      >
        <span>{{ openProblem || problem }}</span>
        <button
          type="button"
          class="text-button chat-inline-retry"
          :aria-disabled="opening || loading || refreshing"
          :aria-busy="opening || loading || refreshing"
          @click="refresh"
        >
          马上重读
        </button>
      </div>
      <ChatComposer
        :disabled="!chatId"
        :owner-key="session.user.value?.id ?? null"
        @send="send"
      />
    </PrivateGate>
  </div>
</template>
