<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import type { ConversationMessage } from '../api/client'
import { handCopy } from '../theme/handCopy'
import {
  coveredVisitorIds,
  describeRevisit,
  effectiveMessageState,
  replyTargetIds,
} from '../utils/noncompulsory'
import { clockTime, isoTime, postmarkDay, stampTime } from '../utils/time'
import HandNote from './ink/HandNote.vue'
import PaperSheet from './ink/PaperSheet.vue'
import Postmark from './ink/Postmark.vue'
import RoundSeal from './ink/RoundSeal.vue'
import '../styles/letter-reading.css'

const props = defineProps<{
  messages: ConversationMessage[]
  displayName: string
  loading: boolean
  incomplete: boolean
  hasEarlier: boolean
  fetchEarlier: () => Promise<boolean>
}>()

// 长信只展开最近几封，更早的折在上面：内存里有的点开往前展开，展开完再向服务器往前读。
// 展开的内容接在按钮下面，往下读；起点按那封信固定，新来的回信只往后加。
const RECENT = 4
const FOLD_FROM = 7
const STEP = 20
const anchor = ref<string | null>(null)
const first = computed(() => {
  const index = anchor.value
    ? props.messages.findIndex((message) => message.id === anchor.value)
    : -1
  if (index >= 0) return index
  const count = props.messages.length
  return count >= FOLD_FROM ? count - RECENT : 0
})
const visible = computed(() => props.messages.slice(first.value))
const fetching = ref(false)
const failed = ref(false)
const sectionEl = ref<HTMLElement | null>(null)
const moreBtn = ref<HTMLButtonElement | null>(null)
let mounted = true
onBeforeUnmount(() => {
  mounted = false
})

watch(
  () => props.messages.length,
  (count) => {
    if (count && anchor.value === null) anchor.value = props.messages[first.value]?.id ?? null
  },
  { immediate: true },
)

async function showEarlier() {
  if (fetching.value) return
  const top = props.messages[first.value]?.id ?? null
  const hadFocusBeforeFetch = document.activeElement === moreBtn.value
  if (!first.value) {
    fetching.value = true
    failed.value = false
    const ok = await props.fetchEarlier()
    if (!mounted) return
    fetching.value = false
    failed.value = !ok
    if (!ok) return
  }
  const index = top ? props.messages.findIndex((message) => message.id === top) : -1
  // 异步前翻回来后再次核对焦点：用户若已移焦就不抢焦点，也不触发额外滚屏。
  const stillOnButton = moreBtn.value
    ? document.activeElement === moreBtn.value
    : hadFocusBeforeFetch
  let revealedId: string | null = null
  if (index > 0) {
    const nextFirst = Math.max(0, index - STEP)
    revealedId = props.messages[nextFirst]?.id ?? null
    anchor.value = revealedId
  } else {
    revealedId = props.messages[first.value]?.id ?? null
  }
  if (!stillOnButton) return
  await nextTick()
  if (!mounted || moreBtn.value) return
  if (document.activeElement && document.activeElement !== document.body) return
  const target =
    (revealedId
      ? sectionEl.value?.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(revealedId)}"]`)
      : null) ??
    sectionEl.value?.querySelector<HTMLElement>('[data-message-id]') ??
    sectionEl.value?.querySelector<HTMLElement>('#history-title')
  target?.focus({ preventScroll: true })
}

const earlierLabel = computed(() => {
  // 服务器上还有更早的没读进来时，只说至少还有几封。
  if (first.value) return `更早的${props.hasEarlier ? '至少' : ''}还有 ${first.value} 封，往前展开`
  if (fetching.value) return '正在往前读…'
  return failed.value ? '更早的信这会儿没读出来，再点一次试试' : '往前读更早的信'
})

const coveredIds = computed(() => coveredVisitorIds(props.messages))
const created = computed(
  () => new Map(props.messages.map((message) => [message.id, message.created_at])),
)

function replyTarget(message: ConversationMessage) {
  const targets = replyTargetIds(message)
  if (!targets.length) return ''
  if (targets.length === 1) {
    const at = created.value.get(targets[0]!)
    return at ? `回你 ${stampTime(at)} 的那封信` : '回你之前的一封信'
  }
  const times = targets
    .map((id) => created.value.get(id))
    .filter((at): at is number => typeof at === 'number')
    .sort((a, b) => a - b)
  return times.length
    ? `合着回你 ${stampTime(times[0]!)} 等 ${targets.length} 封信`
    : `合着回你之前的 ${targets.length} 封信`
}

function visitorState(message: ConversationMessage) {
  const state = effectiveMessageState(message, coveredIds.value)
  const revisitLine = describeRevisit(message.reply_decision?.revisit)
  switch (state) {
    case 'replied':
    case 'legacy_replied':
      return { label: '米洛回过了', tone: 'settled' as const, revisitLine }
    case 'no_reply':
      return { label: '已收下', tone: 'settled' as const, revisitLine }
    case 'deferred':
      return { label: '留待稍后回访', tone: 'deferred' as const, revisitLine }
    case 'unprocessed':
      return { label: '还在等回信', tone: 'waiting' as const, revisitLine }
    case 'legacy_unreplied':
    default:
      return {
        label: props.incomplete ? '当前记录中暂未见回信' : '还在等回信',
        tone: props.incomplete ? ('muted' as const) : ('waiting' as const),
        revisitLine: '',
      }
  }
}
const historySummary = computed(() => {
  const total = props.messages.length
  if (!total) return ''
  if (first.value || props.hasEarlier) {
    return `正展开最近 ${visible.value.length} 封`
  }
  return `共 ${total} 封`
})
</script>

<template>
  <section
    ref="sectionEl"
    class="message-history"
    aria-labelledby="history-title"
    :aria-busy="loading"
  >
    <div class="history-bar">
      <h2
        id="history-title"
        class="history-title"
        tabindex="-1"
      >
        我们的信
      </h2>
      <p
        v-if="historySummary"
        class="history-summary"
      >
        {{ historySummary }}
      </p>
    </div>
    <button
      v-if="first || (hasEarlier && messages.length)"
      ref="moreBtn"
      type="button"
      class="history-more"
      :class="{ 'is-failed': failed && !first }"
      :aria-disabled="fetching"
      :aria-busy="fetching"
      @click="showEarlier"
    >
      {{ earlierLabel }}
    </button>
    <p
      v-if="!messages.length"
      class="empty-history"
    >
      {{
        loading ? '正在找回记录…' : incomplete ? '记录暂时无法确认，请重新读取。' : '这里还没有信。'
      }}
      <HandNote
        v-if="!loading && !incomplete"
        size="sm"
        >{{ handCopy.emptyThread }}</HandNote
      >
    </p>
    <TransitionGroup
      v-else
      tag="ol"
      name="slip"
      class="message-list"
    >
      <li
        v-for="message in visible"
        :key="message.id"
        class="message-entry"
        :class="message.role === 'milo' ? 'is-milo' : 'is-visitor'"
      >
        <PaperSheet
          v-if="message.role === 'milo'"
          as="article"
          paper="grid"
          :tilt="0"
          class="letter-sheet reply-sheet"
          :data-message-id="message.id"
          tabindex="-1"
          :aria-label="`米洛的回信，${stampTime(message.created_at)}`"
        >
          <div class="letter-sheet-head">
            <p class="message-meta">
              <strong>米洛</strong>
              <time :datetime="isoTime(message.created_at)">{{
                stampTime(message.created_at)
              }}</time>
              <span v-if="replyTarget(message)">· {{ replyTarget(message) }}</span>
            </p>
          </div>
          <p class="message-body">{{ message.body }}</p>
          <div class="reply-sign">
            <span>米洛</span>
            <RoundSeal :size="40" />
          </div>
        </PaperSheet>
        <PaperSheet
          v-else
          as="article"
          :tilt="0"
          class="letter-sheet visitor-slip"
          :data-message-id="message.id"
          tabindex="-1"
          :aria-label="`${displayName} 的信，${stampTime(message.created_at)}`"
        >
          <div class="letter-sheet-head">
            <p class="message-meta">
              <strong>{{ displayName }}</strong>
              <time :datetime="isoTime(message.created_at)">{{
                stampTime(message.created_at)
              }}</time>
            </p>
            <Postmark
              class="slip-postmark"
              :date="postmarkDay(message.created_at)"
              :time="clockTime(message.created_at)"
              :size="52"
            />
          </div>
          <p class="message-body">{{ message.body }}</p>
          <p class="message-state">
            <span
              class="state-chip"
              :class="{
                'is-waiting': visitorState(message).tone === 'waiting',
                'is-deferred': visitorState(message).tone === 'deferred',
              }"
              >{{ visitorState(message).label }}</span
            >
            <span
              v-if="visitorState(message).revisitLine"
              class="state-revisit"
              >{{ visitorState(message).revisitLine }}</span
            >
          </p>
        </PaperSheet>
      </li>
    </TransitionGroup>
  </section>
</template>
