<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type { WorkReceipt, WorkTask } from '../../api/client'
import { isMoving } from '../../utils/work'

const props = defineProps<{
  task: WorkTask
  receipt: WorkReceipt | null
  loading: boolean
  closurePolling: boolean
  problem: string
  hasTurnResults: boolean
}>()

const emit = defineEmits<{
  retry: []
}>()

const moving = computed(() => isMoving(props.task))
const headingEl = ref<HTMLElement | null>(null)
const pendingRetryBtn = ref<HTMLButtonElement | null>(null)
const alertRetryBtn = ref<HTMLButtonElement | null>(null)

function handleRetry() {
  if (props.loading) return
  emit('retry')
}

let restoreFocusOnPatch = false

watch(
  [() => props.problem, () => props.receipt?.state, () => props.loading, moving],
  () => {
    const active = document.activeElement
    if (active && (active === pendingRetryBtn.value || active === alertRetryBtn.value)) {
      restoreFocusOnPatch = true
    }
  },
  { flush: 'sync' },
)

watch([() => props.problem, () => props.receipt?.state, () => props.loading, moving], async () => {
  if (!restoreFocusOnPatch) return
  await nextTick()
  if (!restoreFocusOnPatch) return
  const active = document.activeElement
  const stillOnButton =
    (pendingRetryBtn.value && active === pendingRetryBtn.value) ||
    (alertRetryBtn.value && active === alertRetryBtn.value)
  restoreFocusOnPatch = false
  if (stillOnButton) return
  if (active === document.body || active === null) {
    headingEl.value?.focus({ preventScroll: true })
  }
})
</script>

<template>
  <section
    class="detail-section receipt-section"
    aria-labelledby="receipt-heading"
  >
    <div class="section-head">
      <h2
        id="receipt-heading"
        ref="headingEl"
        tabindex="-1"
      >
        整单回执
      </h2>
      <button
        v-if="receipt?.state === 'pending' && !moving && !problem"
        ref="pendingRetryBtn"
        type="button"
        class="detail-button is-quiet"
        :aria-disabled="loading"
        :aria-busy="loading"
        @click="handleRetry"
      >
        {{ loading ? '正在读取…' : '重读回执' }}
      </button>
    </div>

    <div
      v-if="problem"
      class="detail-banner is-alert"
      role="alert"
    >
      <p>{{ problem }}</p>
      <button
        ref="alertRetryBtn"
        type="button"
        class="detail-button is-quiet"
        :aria-disabled="loading"
        :aria-busy="loading"
        @click="handleRetry"
      >
        {{ loading ? '正在重读…' : '重读回执' }}
      </button>
    </div>

    <p
      v-if="loading && !receipt && !problem"
      class="detail-empty"
      role="status"
    >
      正在读取整单回执…
    </p>

    <div
      v-else-if="receipt?.state === 'available'"
      class="receipt-card"
    >
      <pre class="receipt-body">{{ receipt.text }}</pre>
    </div>

    <p
      v-else-if="receipt?.state === 'pending'"
      class="detail-note-card"
      role="status"
    >
      {{
        task.status === 'needs_reconciliation'
          ? '这件还在等米洛核对，整单回执暂未整理好。'
          : moving
            ? '等这件处理结束后，这里会汇总一份整单回执。'
            : task.decision?.state === 'needs_clarification'
              ? '米洛正在等你补充说明，这件目前还没有完成回执。'
              : task.decision?.state === 'deferred'
                ? '这件已暂缓留待稍后再看，整单回执待后续整理。'
                : task.decision?.state === 'declined'
                  ? '米洛已婉拒这件交办，整单回执暂未整理好。'
                  : closurePolling
                    ? '任务已结束，整单回执正在整理，稍等片刻会自动再读一次。'
                    : '整单回执还没整理好，可以点「重读回执」。'
      }}
    </p>

    <p
      v-else-if="receipt?.state === 'unavailable'"
      class="detail-note-card"
      role="status"
    >
      {{
        hasTurnResults
          ? '这件暂时没有可显示的整单回执；下面的每轮答复记录仍可查看。'
          : '这件暂时没有可显示的整单回执。'
      }}
    </p>
  </section>
</template>
