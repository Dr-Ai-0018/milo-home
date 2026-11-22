<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import type { ConversationMessage, ReplySummary } from '../api/client'
import type { OutgoingSlip } from '../composables/useChatOutbox'
import {
  coveredVisitorIds,
  describeRevisit,
  effectiveMessageState,
  replyTargetIds,
} from '../utils/noncompulsory'
import { clockTime, dayLabel, isoTime, sameDay } from '../utils/time'
import '../styles/chat-slips.css'

const props = defineProps<{
  messages: ConversationMessage[]
  outbox: OutgoingSlip[]
  complete: boolean
  hasEarlier: boolean
  replySummary?: ReplySummary | null
  fetchEarlier: () => Promise<boolean>
}>()
const emit = defineEmits<{ retry: [id: string]; discard: [id: string] }>()

// 长聊天只摆出最近的一段，更早的收在上面：内存里有的点开再往前摆，摆完了再向服务器往前读。
// 没往前翻时窗口跟着最新走；翻过后按那张纸条固定起点，往前读进来的和新来的都不挤动正在看的。
const STEP = 80
const anchor = ref<string | null>(null)
const first = computed(() => {
  const index = anchor.value
    ? props.messages.findIndex((message) => message.id === anchor.value)
    : -1
  return index >= 0 ? index : Math.max(0, props.messages.length - STEP)
})
const visible = computed(() => props.messages.slice(first.value))
const fetching = ref(false)
const failed = ref(false)
const list = ref<HTMLOListElement | null>(null)
let mounted = true
onBeforeUnmount(() => {
  mounted = false
})

function placeOf(id: string | null) {
  const slip = id ? list.value?.querySelector(`[data-id="${CSS.escape(id)}"]`) : null
  return slip?.getBoundingClientRect().top ?? null
}

// 往前摆开时保持眼前这张纸条不动：按它自己前后的位置差补偿，等待期间底部新来的纸条不算进去。
// 先关掉浏览器自带的滚动锚定，免得补偿两次。读回来时已经离开或换了聊天，就什么都不动。
async function showEarlier() {
  if (fetching.value) return
  const top = props.messages[first.value]?.id ?? null
  if (!first.value) {
    fetching.value = true
    failed.value = false
    const done = await props.fetchEarlier()
    if (!mounted) return
    fetching.value = false
    failed.value = !done
    if (!done) return
  }
  const index = top ? props.messages.findIndex((message) => message.id === top) : -1
  if (index <= 0) return
  const before = placeOf(top)
  anchor.value = props.messages[Math.max(0, index - STEP)]?.id ?? null
  await nextTick()
  const after = placeOf(top)
  if (mounted && before !== null && after !== null) scrollBy(0, after - before)
}

const earlierLabel = computed(() => {
  if (first.value)
    return `更早的纸条${props.hasEarlier ? '至少' : ''}还有 ${first.value} 张，往前看一些`
  if (fetching.value) return '正在往前读…'
  return failed.value ? '更早的纸条这会儿没读出来，再点一次试试' : '往前读更早的纸条'
})

// 来访者在往回看时，新纸条只往后加，不从上面挤掉正在看的。
function hold() {
  anchor.value ??= props.messages[first.value]?.id ?? null
}

defineExpose({ hold })

const coveredIds = computed(() => coveredVisitorIds(props.messages))

// 同一个人三分钟内连着写的纸条收紧排列，只在第一张标署名和时间；跨天加日期分隔。
// 合回正文只呈现一次，仅在覆盖多张纸条时补轻提示；只在确有未取消的回访安排时附一句说明，不逐条贴标签。
const rows = computed(() =>
  visible.value.map((message, index) => {
    const previous = visible.value[index - 1]
    const newDay = !previous || !sameDay(previous.created_at, message.created_at)
    const mergedCount = message.role === 'milo' ? replyTargetIds(message).length : 0
    const revisitNote =
      message.role === 'visitor' ? describeRevisit(message.reply_decision?.revisit) : ''
    const grouped =
      !newDay &&
      previous.role === message.role &&
      message.created_at - previous.created_at < 180 &&
      mergedCount <= 1
    return {
      message,
      newDay,
      grouped,
      mergedNote: mergedCount > 1 ? `合着回 ${mergedCount} 张` : '',
      revisitNote,
    }
  }),
)

// 末尾真正尚未处理的来访纸条才在最后一张下面轻声说明；不把已合回、不需回复或已暂缓的算成“还没回”。
// 有全会话 reply_summary 时优先按全会话真值；旧后端缺失时退回按已读页末尾未覆盖纸条估算。
const waiting = computed(() => {
  if (!props.complete) return ''
  const summary = props.replySummary
  if (summary) {
    if (summary.unprocessed_count <= 0) return ''
    return summary.unprocessed_count > 1
      ? `这 ${summary.unprocessed_count} 张正存着，等米洛来看。`
      : '这张正存着，等米洛来看。'
  }
  let count = 0
  let bounded = false
  for (const message of [...props.messages].reverse()) {
    if (message.role !== 'visitor') {
      bounded = true
      break
    }
    const state = effectiveMessageState(message, coveredIds.value)
    if (state !== 'unprocessed' && state !== 'legacy_unreplied') {
      bounded = true
      break
    }
    count += 1
  }
  if (!count) return ''
  if (!bounded && props.hasEarlier) return `至少有 ${count} 张正存着，等米洛来看。`
  return count > 1 ? `这 ${count} 张正存着，等米洛来看。` : '这张正存着，等米洛来看。'
})
const lastId = computed(() => props.messages.at(-1)?.id)

const outboxLabels: Record<OutgoingSlip['state'], string> = {
  sending: '正在递过去…',
  failed: '没递过去',
  unknown: '不确定有没有递到',
}

// 点击重试时按钮会随 sending 状态卸掉：只在焦点本来在这张待发纸条内时原地保焦，不触发额外滚动。
async function onRetry(id: string) {
  const active = document.activeElement
  const fromSlip =
    active instanceof Element && !!active.closest(`[data-outbox-id="${CSS.escape(id)}"]`)
  emit('retry', id)
  if (!fromSlip) return
  await nextTick()
  if (!mounted || (document.activeElement && document.activeElement !== document.body)) return
  const slipEl = list.value?.querySelector<HTMLElement>(`[data-outbox-id="${CSS.escape(id)}"]`)
  if (slipEl) slipEl.focus({ preventScroll: true })
  else document.querySelector<HTMLElement>('#chat-input')?.focus({ preventScroll: true })
}

function onDiscard(id: string) {
  emit('discard', id)
}

// 待发纸条因递送成功或「不要了」移出列表时，若焦点仍停在它身上，则温和移交到相邻待发纸条或输入框。
watch(
  () => props.outbox.map((item) => item.id),
  async (ids, previousIds) => {
    if (!previousIds?.length) return
    const removed = previousIds.filter((id) => !ids.includes(id))
    if (!removed.length) return
    const active = document.activeElement
    const activeId =
      active instanceof Element
        ? active.closest<HTMLElement>('[data-outbox-id]')?.dataset.outboxId
        : null
    if (!activeId || !removed.includes(activeId)) return
    const index = previousIds.indexOf(activeId)
    const fallbackId =
      previousIds.slice(index + 1).find((id) => ids.includes(id)) ??
      [...previousIds.slice(0, index)].reverse().find((id) => ids.includes(id)) ??
      null
    await nextTick()
    if (!mounted || (document.activeElement && document.activeElement !== document.body)) return
    const target = fallbackId
      ? list.value?.querySelector<HTMLElement>(
          `[data-outbox-id="${CSS.escape(fallbackId)}"] .chat-retry button, [data-outbox-id="${CSS.escape(fallbackId)}"]`,
        )
      : null
    if (target) target.focus({ preventScroll: true })
    else document.querySelector<HTMLElement>('#chat-input')?.focus({ preventScroll: true })
  },
  { flush: 'sync' },
)
</script>

<template>
  <button
    v-if="first || hasEarlier"
    type="button"
    class="chat-earlier"
    :class="{ 'is-failed': failed && !first }"
    :aria-disabled="fetching"
    :aria-busy="fetching"
    @click="showEarlier"
  >
    {{ earlierLabel }}
  </button>
  <ol
    ref="list"
    class="chat-slips"
    aria-label="纸条"
  >
    <template
      v-for="row in rows"
      :key="row.message.id"
    >
      <li
        v-if="row.newDay"
        class="chat-day"
        aria-hidden="true"
      >
        <span>{{ dayLabel(row.message.created_at) }}</span>
      </li>
      <li
        class="chat-slip"
        :class="[`is-${row.message.role}`, { 'is-grouped': row.grouped }]"
        :data-id="row.message.id"
      >
        <p
          class="chat-meta"
          :class="{ 'visually-hidden': row.grouped }"
        >
          <strong>{{ row.message.role === 'milo' ? '米洛' : '你' }}</strong>
          <time :datetime="isoTime(row.message.created_at)">{{
            clockTime(row.message.created_at)
          }}</time>
          <span
            v-if="row.mergedNote"
            class="chat-merged-note"
            >· {{ row.mergedNote }}</span
          >
        </p>
        <p class="chat-body">{{ row.message.body }}</p>
        <p
          v-if="row.revisitNote"
          class="chat-slip-note"
        >
          {{ row.revisitNote }}
        </p>
      </li>
      <li
        v-if="waiting && row.message.id === lastId"
        class="chat-waiting"
      >
        {{ waiting }}
      </li>
    </template>
    <li
      v-for="slip in outbox"
      :key="slip.id"
      class="chat-slip is-visitor is-outgoing"
      :class="`is-${slip.state}`"
      :data-outbox-id="slip.id"
      tabindex="-1"
    >
      <p class="chat-meta">
        <strong>你</strong>
        <span class="chat-outbox-state">{{ outboxLabels[slip.state] }}</span>
      </p>
      <p class="chat-body">{{ slip.body }}</p>
      <div
        v-if="slip.state !== 'sending'"
        class="chat-retry"
      >
        <span class="chat-retry-reason">{{
          slip.state === 'unknown' ? '可以再试一次，不会重复。' : slip.reason
        }}</span>
        <span class="chat-retry-actions">
          <button
            type="button"
            class="text-button"
            @click="onRetry(slip.id)"
          >
            再试一次
          </button>
          <button
            v-if="slip.state === 'failed'"
            type="button"
            class="text-button"
            @click="onDiscard(slip.id)"
          >
            不要了
          </button>
        </span>
      </div>
    </li>
  </ol>
</template>
