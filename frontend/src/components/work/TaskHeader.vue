<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type { WorkTask } from '../../api/client'
import { seedOf } from '../../utils/stampInk'
import { isoTime, stampDate, stampTime } from '../../utils/time'
import {
  effectiveTaskCopy,
  effectiveTaskStamp,
  isUnfinishedDecision,
  isMoving,
  sheetNumber,
  taskTitle,
} from '../../utils/work'
import PencilRing from '../ink/PencilRing.vue'
import RubberStamp from '../ink/RubberStamp.vue'

const props = defineProps<{
  task: WorkTask
  receiptText?: string
  supplementCount: number
  loading: boolean
  stopping: boolean
  confirmingStop: boolean
  stopProblem?: string
  failures: number
}>()

const emit = defineEmits<{
  refresh: []
  askStop: [open: boolean]
  confirmStop: []
}>()

const titleEl = ref<HTMLElement | null>(null)
const stopAskEl = ref<HTMLButtonElement | null>(null)
const stopCancelEl = ref<HTMLButtonElement | null>(null)
const stopConfirmEl = ref<HTMLElement | null>(null)

const copy = computed(() => effectiveTaskCopy(props.task))
const moving = computed(() => isMoving(props.task))
const showDecisionReceipt = computed(() => {
  const brief = copy.value.receipt?.trim()
  if (!brief) return false
  return brief !== (props.receiptText?.trim() ?? '')
})
const canStop = computed(
  () =>
    props.task.status === 'queued' ||
    props.task.status === 'running' ||
    (props.task.status === 'completed' && isUnfinishedDecision(props.task.decision)),
)
const stamp = computed(() => effectiveTaskStamp(props.task))
const stampSeed = computed(() =>
  seedOf(`${props.task.id}:${props.task.status}:${props.task.decision?.state ?? ''}`),
)

watch(canStop, async (available) => {
  if (available) return
  const focused = document.activeElement
  if (focused !== stopAskEl.value && !stopConfirmEl.value?.contains(focused)) return
  await nextTick()
  if (document.activeElement === document.body) titleEl.value?.focus({ preventScroll: true })
})

defineExpose({
  focusTitle: () => titleEl.value?.focus({ preventScroll: true }),
  focusStopAsk: () => stopAskEl.value?.focus(),
  focusStopCancel: () => stopCancelEl.value?.focus(),
})
</script>

<template>
  <header class="detail-head">
    <div class="detail-meta">
      <span class="sheet-number">{{ sheetNumber(task) }}</span>
      <span class="detail-meta-item">
        交办于
        <time :datetime="isoTime(task.created_at)">{{ stampTime(task.created_at) }}</time>
      </span>
      <span class="detail-meta-item">
        最后记账
        <time :datetime="isoTime(task.updated_at)">{{ stampTime(task.updated_at) }}</time>
      </span>
      <span
        v-if="supplementCount > 0"
        class="detail-meta-item"
      >
        补过 {{ supplementCount }} 段话
      </span>
    </div>

    <div class="detail-title-row">
      <div class="detail-title-copy">
        <h1
          id="private-title"
          ref="titleEl"
          class="detail-title"
          tabindex="-1"
        >
          {{ taskTitle(task) || '未题名的交办' }}
        </h1>
        <p
          class="detail-status-line"
          :class="`status-${copy.toneKey}`"
        >
          <span
            v-if="moving"
            class="detail-status-dot"
            aria-hidden="true"
          />
          <strong>{{ copy.line }}</strong>
        </p>
        <p
          v-if="copy.note"
          class="detail-status-note"
        >
          {{ copy.note }}
        </p>
        <p
          v-if="showDecisionReceipt"
          class="detail-decision-receipt"
        >
          {{ copy.receipt }}
        </p>
      </div>

      <div
        class="detail-stamp"
        aria-hidden="true"
      >
        <PencilRing
          v-if="stamp.kind === 'pencil'"
          :seed="stampSeed"
        />
        <RubberStamp
          v-else-if="stamp.kind === 'round'"
          :label="stamp.label"
          :date="stampDate(task.updated_at, true)"
          :seed="stampSeed"
          shape="round"
          tone="stamp"
          :ink="0.92"
        />
        <RubberStamp
          v-else
          :label="stamp.label"
          :date="stampDate(task.updated_at)"
          :seed="stampSeed"
          shape="rect"
          :tone="stamp.tone"
          :ink="0.92"
        />
      </div>
    </div>

    <div class="detail-actions">
      <button
        type="button"
        class="detail-button"
        :aria-disabled="loading"
        :aria-busy="loading"
        @click="!loading && emit('refresh')"
      >
        {{ loading ? '正在重读…' : '重新读取' }}
      </button>

      <template v-if="canStop">
        <button
          v-if="!confirmingStop"
          ref="stopAskEl"
          type="button"
          class="detail-button is-stamp"
          :aria-disabled="stopping"
          @click="emit('askStop', true)"
        >
          停下这件
        </button>

        <div
          v-else
          ref="stopConfirmEl"
          class="stop-confirm"
          role="region"
          aria-label="确认停下"
        >
          <span class="stop-confirm-note">已经做完的部分不会撤销，确定停下吗？</span>
          <button
            type="button"
            class="detail-button is-stamp-fill"
            :aria-disabled="stopping"
            :aria-busy="stopping"
            @click="emit('confirmStop')"
          >
            {{ stopping ? '正在递给米洛…' : '确定停下' }}
          </button>
          <button
            ref="stopCancelEl"
            type="button"
            class="detail-button"
            :aria-disabled="stopping"
            @click="emit('askStop', false)"
          >
            先不停
          </button>
        </div>
      </template>
    </div>

    <p
      v-if="stopProblem"
      class="detail-banner is-alert"
      role="alert"
    >
      {{ stopProblem }}
    </p>

    <p
      v-if="failures > 2"
      class="detail-banner is-warn"
      role="status"
    >
      最近几次自动跟读没有连上，下面显示的是上次读到的记录。可以点「重新读取」试试。
    </p>
  </header>
</template>
