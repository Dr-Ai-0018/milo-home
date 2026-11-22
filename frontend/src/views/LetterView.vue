<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ConversationStatus from '../components/ConversationStatus.vue'
import LetterComposer from '../components/LetterComposer.vue'
import KnockDoor from '../components/KnockDoor.vue'
import MessageHistory from '../components/MessageHistory.vue'
import PrivateGate from '../components/PrivateGate.vue'
import { useLetterFollowing } from '../composables/useLetterFollowing'
import { usePrivateScope } from '../composables/usePrivateScope'
import { useConversation } from '../composables/useConversation'
import { usePrivateSession } from '../composables/usePrivateSession'
import { letterHeaderStatus, letterTitle } from '../utils/letters'
import { dayLabel } from '../utils/time'
import '../styles/letters.css'
import '../styles/letter-detail.css'

const route = useRoute()
const router = useRouter()
const session = usePrivateSession()
const { user, conversations, listed } = session

const id = computed(() => String(route.params.id ?? ''))
const letter = computed(() => conversations.value.find((item) => item.id === id.value) ?? null)
// 会话列表确实读到了、里面仍没有，才算不存在；没读到只说暂时读不出。
const missing = computed(
  () => listed.value && !!user.value && !session.loading.value && !letter.value,
)
const unread = computed(() => !listed.value && !!user.value && !letter.value)

const letterId = () => (letter.value?.kind === 'letter' ? letter.value.id : null)
const {
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
} = useConversation(letterId)
const scope = usePrivateScope(letterId)
const { status, connectionLine, checking, canRecheck, check } = useLetterFollowing(
  letterId,
  catchUp,
  behind,
)

async function recheckStatus() {
  if (checking.value) return
  const active = scope.capture()
  const activeEl = document.activeElement
  const fromRecheck =
    activeEl instanceof Element && activeEl.classList.contains('conv-status-recheck')
  await check()
  if (!active() || !fromRecheck) return
  await nextTick()
  if (!active()) return
  if (document.activeElement && document.activeElement !== document.body) return
  document.querySelector<HTMLElement>('#private-title')?.focus({ preventScroll: true })
}

const retryBtn = ref<HTMLButtonElement | null>(null)

// 读取或保存中按钮只标 aria-disabled，焦点留在原处；结束后若焦点仍未移走，再温和交回可读处或重读按钮。
async function retry() {
  if (loading.value || sending.value) return
  const active = scope.capture()
  const fromRetry = document.activeElement === retryBtn.value
  await open()
  if (!active() || !fromRetry) return
  await nextTick()
  if (!active()) return
  if (document.activeElement && document.activeElement !== document.body) return
  const target = problem.value
    ? retryBtn.value
    : ((draft.value ? document.querySelector<HTMLElement>('#message') : null) ??
      document.querySelector<HTMLElement>('#history-title') ??
      document.querySelector<HTMLElement>('#private-title'))
  target?.focus({ preventScroll: true })
}

async function open() {
  const active = scope.capture()
  await load()
  // 主内容恢复即可交回焦点，辅助活动慢或失败不拖住重读收尾。
  if (active()) void check()
}

const draft = ref('')
const sending = ref(false)
const notice = ref('')
let attempt: { body: string; key: string } | null = null

async function send() {
  if (!letter.value || sending.value || loading.value || !draft.value.trim()) return
  const active = scope.capture()
  const body = draft.value.trim()
  if (!attempt || attempt.body !== body) attempt = { body, key: crypto.randomUUID() }
  sending.value = true
  problem.value = ''
  notice.value = ''
  let stored = false
  try {
    const saved = await post(body, attempt.key)
    if (!saved || !active()) return
    draft.value = ''
    attempt = null
    notice.value = '已保存。回信完成后，可以重新读取记录。'
    stored = true
  } catch (error) {
    if (active()) report(error)
  } finally {
    if (active()) sending.value = false
  }
  // 保存已经确认，立即允许续写；目录和活动各自补读，慢或失败都不拖住写信框。
  if (stored && active()) {
    void session.revalidate()
    void check()
  }
}

watch(
  () => user.value?.id,
  () => {
    sending.value = false
    draft.value = ''
    notice.value = ''
    attempt = null
  },
  { flush: 'sync' },
)

// 聊天也是会话，误入时转到聊天页。
watch(
  letter,
  (current, previous) => {
    if (current?.kind === 'chat') void router.replace('/chat')
    else if (!current || current.id !== previous?.id) {
      sending.value = false
      draft.value = ''
      notice.value = ''
      attempt = null
      if (current) void open()
    }
  },
  { immediate: true, flush: 'sync' },
)
</script>

<template>
  <div class="letters-workspace letter-page">
    <RouterLink
      class="text-button back-link"
      to="/letters"
      >← 所有信</RouterLink
    >
    <PrivateGate @reload="open">
      <p
        v-if="missing"
        class="letter-state-card"
        role="status"
      >
        没有找到这封信。可能链接不完整，回到「所有信」里重新打开。
      </p>
      <p
        v-else-if="unread"
        class="letter-state-card"
        role="status"
      >
        {{
          session.loading.value ? '正在找回这封信…' : '这封信这会儿没读出来，读取失败不会弄丢它。'
        }}
      </p>
      <template v-else-if="letter && user">
        <div class="conversation-header">
          <div class="knock-row">
            <h1
              id="private-title"
              class="overprint letter-title"
              tabindex="-1"
            >
              {{ letterTitle(letter) }}
            </h1>
            <KnockDoor :conversation-id="letter.id" />
          </div>
          <p class="pending-line">
            {{ dayLabel(letter.created_at) }} 开始
            <template v-if="letter.message_count">· 共 {{ letter.message_count }} 封</template>
            · {{ letterHeaderStatus(letter) }}
          </p>
          <ConversationStatus
            variant="inline"
            :status="status"
            :connection-line="connectionLine"
            :can-recheck="canRecheck"
            :rechecking="checking"
            @recheck="recheckStatus"
          />
        </div>
        <MessageHistory
          :key="letter.id"
          :messages="messages"
          :display-name="user.display_name"
          :loading="loading"
          :incomplete="!settled || behind || loading || !!problem"
          :has-earlier="hasEarlier"
          :fetch-earlier="earlier"
        />
        <LetterComposer
          v-model="draft"
          :label="messages.length ? '接着写' : '写这封信的第一页'"
          :sending="sending"
          :disabled="loading"
          :notice="notice"
          @send="send"
        >
          <template #trouble>
            <div
              v-if="problem"
              class="connection-problem letter-trouble"
            >
              <p
                class="feedback error"
                role="alert"
              >
                {{ problem }}
              </p>
              <button
                ref="retryBtn"
                type="button"
                class="text-button letter-trouble-retry"
                :aria-disabled="loading || sending"
                :aria-busy="loading"
                @click="retry"
              >
                重新读取这封信
              </button>
              <p
                v-if="draft"
                class="small-note"
              >
                正文还留在写信框里。内容不变时再保存一次，不会存成两封。
              </p>
            </div>
          </template>
        </LetterComposer>
      </template>
    </PrivateGate>
  </div>
</template>
