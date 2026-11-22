<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import type { WorkDecision, WorkTask } from '../../api/client'
import DeliverableField from './DeliverableField.vue'
import WorkComposer from './WorkComposer.vue'

const props = withDefaults(
  defineProps<{
    taskStatus: WorkTask['status']
    taskDecision?: WorkDecision | null
    clarifyInPlace?: boolean
    moving?: boolean
    pinned?: boolean
    transitionTip?: string
    open: boolean
    draft: string
    filename: string
    retryAppend: boolean
    composerLabel?: string
    action: string
    sending: boolean
    held: string
    trouble: string
    notice: string
  }>(),
  {
    taskDecision: null,
    clarifyInPlace: false,
    moving: false,
    pinned: false,
    transitionTip: '',
    composerLabel: '',
  },
)

const emit = defineEmits<{
  'update:open': [value: boolean]
  'update:draft': [value: string]
  'update:filename': [value: string]
  send: []
  dismissPinned: []
  composerFocusChange: [focused: boolean]
}>()

const sectionEl = ref<HTMLElement | null>(null)
const panelEl = ref<HTMLElement | null>(null)
const triggerBtnEl = ref<HTMLButtonElement | null>(null)
const dismissPinnedBtnEl = ref<HTMLButtonElement | null>(null)

const activeLabel = computed(() => {
  if (props.clarifyInPlace) return props.composerLabel || '补充说明，接着这件做'
  if (props.moving) return props.composerLabel || '继续补话'
  if (props.retryAppend) return '刚才补的话'
  if (props.pinned) return '刚才写在这里的话（可另开一件交办）'
  return '新交办的内容'
})

const followUpHint = computed(() => {
  if (props.taskStatus === 'needs_reconciliation') {
    return '情况还不清楚，等米洛核对完再决定要不要另交一件。'
  }
  if (props.taskDecision?.state === 'deferred') {
    return '这件已暂缓留待稍后再看；如果是有别的新事要交给米洛，可以另开一张单子。'
  }
  if (props.taskDecision?.state === 'declined') {
    return '这件米洛已婉拒；如果调整过内容想重新交办，可以另开一张单子。'
  }
  return '这件已经停下了；要接着做新事，可以另开一张单子。'
})

async function toggleOpen() {
  const next = !props.open
  emit('update:open', next)
  if (!next) return
  await nextTick()
  const field = panelEl.value?.querySelector('textarea')
  field?.focus()
}

async function handleDismissPinned() {
  const focusedOnButton = document.activeElement === dismissPinnedBtnEl.value
  emit('dismissPinned')
  await nextTick()
  if (focusedOnButton) triggerBtnEl.value?.focus()
}

function handleFocusOut(event: FocusEvent) {
  const next = event.relatedTarget as Node | null
  if (!sectionEl.value?.contains(next)) {
    emit('composerFocusChange', false)
  }
}
</script>

<template>
  <section
    ref="sectionEl"
    class="detail-section composer-section followup-section"
    :aria-label="moving || pinned ? '继续补话与草稿' : undefined"
    :aria-labelledby="moving || pinned ? undefined : 'followup-heading'"
    @focusin="emit('composerFocusChange', true)"
    @focusout="handleFocusOut"
  >
    <div
      v-if="clarifyInPlace"
      class="followup-trigger-row"
    >
      <div class="followup-copy">
        <h2 id="followup-heading">补充说明（接着原单）</h2>
        <p class="followup-hint">
          米洛想先问清楚再继续做；直接写在这里就会接回这张原单，不用另开新单。
        </p>
      </div>
    </div>

    <div
      v-else-if="!moving && !pinned && !retryAppend"
      class="followup-trigger-row"
    >
      <div class="followup-copy">
        <h2 id="followup-heading">另开一件交办</h2>
        <p class="followup-hint">{{ followUpHint }}</p>
      </div>
      <button
        ref="triggerBtnEl"
        type="button"
        class="detail-button"
        :aria-expanded="open"
        @click="toggleOpen"
      >
        {{ open ? '先收起' : '另开一件交办' }}
      </button>
    </div>

    <div
      v-else-if="!moving && !pinned && retryAppend"
      class="followup-trigger-row"
    >
      <div class="followup-copy">
        <h2 id="followup-heading">确认刚才的补话</h2>
        <p class="followup-hint">可以沿用刚才的请求，确认那段话有没有存好。</p>
      </div>
    </div>

    <div
      v-if="clarifyInPlace || moving || pinned || open || retryAppend"
      ref="panelEl"
      class="followup-composer"
    >
      <WorkComposer
        :model-value="draft"
        :label="activeLabel"
        :action="action"
        :sending="sending"
        :held="held"
        :notice="notice"
        @update:model-value="emit('update:draft', $event)"
        @send="emit('send')"
      >
        <template
          v-if="!clarifyInPlace && !moving && !retryAppend && !held"
          #deliverable
        >
          <DeliverableField
            :model-value="filename"
            :sending="sending"
            @update:model-value="emit('update:filename', $event)"
          />
        </template>
        <template #trouble>
          <p
            v-if="trouble"
            class="field-error"
            role="alert"
          >
            {{ trouble }}
          </p>
        </template>
      </WorkComposer>

      <div
        v-if="!moving && pinned && transitionTip"
        class="composer-transition-bar"
      >
        <p
          class="composer-transition-tip"
          role="status"
        >
          {{ transitionTip }}
        </p>
        <button
          v-if="!retryAppend"
          ref="dismissPinnedBtnEl"
          type="button"
          class="detail-button is-quiet"
          @click="handleDismissPinned"
        >
          先收到底部
        </button>
      </div>
    </div>
  </section>
</template>
