<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { explain } from '../api/client'
import PaperSheet from '../components/ink/PaperSheet.vue'
import PrivateGate from '../components/PrivateGate.vue'
import TaskAttachments from '../components/work/TaskAttachments.vue'
import TaskFollowUp from '../components/work/TaskFollowUp.vue'
import TaskHeader from '../components/work/TaskHeader.vue'
import TaskReceipt from '../components/work/TaskReceipt.vue'
import TaskRecords from '../components/work/TaskRecords.vue'
import { usePrivateSession } from '../composables/usePrivateSession'
import { useWorkComposer } from '../composables/useWorkComposer'
import { useWorkDownloads } from '../composables/useWorkDownloads'
import { useWorkTask } from '../composables/useWorkTask'
import { isMoving } from '../utils/work'
import '../styles/work.css'
import '../styles/work-detail.css'
import '../styles/work-delivery.css'

const route = useRoute()
const session = usePrivateSession()
const id = computed(() => String(route.params.id ?? ''))
const {
  task,
  inputs,
  results,
  artifacts,
  receipt,
  attachments,
  events,
  loading,
  receiptLoading,
  closurePolling,
  attachmentsLoading,
  problem,
  receiptProblem,
  attachmentsProblem,
  artifactsProblem,
  recordsLoading,
  recordsProblem,
  refreshRecords,
  gone,
  failures,
  open,
  refresh,
  refreshReceipt,
  refreshAttachments,
  markGone,
  ...work
} = useWorkTask(() => id.value || null)

const headerRef = ref<InstanceType<typeof TaskHeader> | null>(null)
const followUpOpen = ref(false)
const composerPinned = ref(false)
const composerFocused = ref(false)
const transitionTip = ref('')

const reply = useWorkComposer({
  task,
  capture: work.capture,
  append: work.append,
  onFollowUpCreated: () => {
    followUpOpen.value = false
  },
})

const attachmentItems = computed(() => attachments.value?.attachments ?? [])
const downloads = useWorkDownloads({
  taskId: () => id.value || null,
  attachments: attachmentItems,
  capture: work.capture,
  signal: work.signal,
  onTaskGone: markGone,
  onRefreshAttachments: () => void refreshAttachments(),
})

const supplements = computed(() =>
  inputs.value.filter((input) => input.kind === 'supplement' && input.source !== 'revisit'),
)
const moving = computed(() => isMoving(task.value))

function dismissPinned() {
  composerPinned.value = false
  followUpOpen.value = false
  transitionTip.value = ''
}

function handleAttachmentsFocusFallback() {
  headerRef.value?.focusTitle()
}

function preserveEditorPosition(taskId: string) {
  const editor = document.activeElement
  if (!composerFocused.value || !(editor instanceof HTMLTextAreaElement)) return
  const top = editor.getBoundingClientRect().top
  const scroll = window.scrollY
  void nextTick(() => {
    if (task.value?.id !== taskId || !composerPinned.value || document.activeElement !== editor) {
      return
    }
    if (Math.abs(window.scrollY - scroll) > 1) return
    const shift = editor.getBoundingClientRect().top - top
    if (Math.abs(shift) > 1) window.scrollBy({ top: shift, behavior: 'instant' })
  })
}

watch(
  [() => task.value?.id ?? null, moving, () => task.value?.status ?? null],
  ([currentTaskId, active, nextStatus], previous) => {
    if (!currentTaskId) {
      composerPinned.value = false
      transitionTip.value = ''
      return
    }
    if (!previous || previous[0] !== currentTaskId) {
      composerPinned.value = false
      transitionTip.value = ''
      return
    }
    if (active) {
      composerPinned.value = false
      transitionTip.value = ''
      return
    }
    if (previous && previous[1] && !active) {
      const editing =
        composerFocused.value ||
        reply.draft.value.length > 0 ||
        reply.filename.value.length > 0 ||
        reply.sending.value ||
        reply.retryAppend.value ||
        Boolean(reply.trouble.value)
      if (editing) {
        // 页头文字与操作会变高或收缩；编辑中的视口位置也一并留住。
        preserveEditorPosition(currentTaskId)
        composerPinned.value = true
        followUpOpen.value = true
        transitionTip.value = reply.retryAppend.value
          ? '这件交办刚刚停下了——刚才那段补话可以点上方按钮确认是否存好。'
          : nextStatus === 'needs_reconciliation'
            ? '这件交办刚变成「等米洛核对」——你写的话先留在这里，等核对清楚后再决定要不要另开一件。'
            : reply.clarifyInPlace.value
              ? '米洛刚在这张单上留言想先问清楚——你刚才写的话还留在这里，可以直接补回这张原单。'
              : '这件交办刚刚结束了——你刚才写的话还留在这里，需要的话可明确点上方按钮另开一件交办，也可先往下看回执与附件。'
      }
    }
  },
  { flush: 'sync' },
)

// 补话待确认时展开表单；确认完成后仍保留反馈和焦点。
watch([moving, reply.retryAppend], ([active, retrying]) => {
  if (!active && retrying) followUpOpen.value = true
})

// 停下要先确认；同一次确认里重试沿用同一个键。
const confirmingStop = ref(false)
const stopping = ref(false)
const stopProblem = ref('')
let stopKey: string | null = null

async function askStop(openConfirm: boolean) {
  if (stopping.value) return
  stopProblem.value = ''
  confirmingStop.value = openConfirm
  await nextTick()
  if (openConfirm) headerRef.value?.focusStopCancel()
  else headerRef.value?.focusStopAsk()
}

async function confirmStop() {
  if (stopping.value) return
  stopKey ??= crypto.randomUUID()
  const current = work.capture()
  stopping.value = true
  stopProblem.value = ''
  try {
    const stopped = await work.stop(stopKey)
    if (!stopped || !current()) return
    confirmingStop.value = false
    stopKey = null
    await nextTick()
    if (current()) headerRef.value?.focusTitle()
  } catch (error) {
    if (current()) stopProblem.value = explain(error)
  } finally {
    if (current()) stopping.value = false
  }
}

function reset() {
  reply.resetComposer()
  stopping.value = false
  stopProblem.value = ''
  confirmingStop.value = false
  stopKey = null
  followUpOpen.value = false
  composerPinned.value = false
  composerFocused.value = false
  transitionTip.value = ''
  downloads.resetDownloads()
}

watch(
  gone,
  (value) => {
    if (value) reset()
  },
  { flush: 'sync' },
)

watch(
  [id, () => session.user.value?.id],
  ([taskId, userId], previous) => {
    if (previous && previous[0] !== taskId) reset()
    if (previous && previous[1] !== userId) reset()
    if (taskId && userId) void open()
  },
  { immediate: true },
)
</script>

<template>
  <div class="work-desk work-task-page">
    <div class="detail-top-bar">
      <RouterLink
        class="text-button back-link"
        to="/work"
        >← 所有交办</RouterLink
      >
      <p class="work-offline">交办先存好，做到哪一步，看单子上的记录。</p>
    </div>

    <PrivateGate @reload="refresh">
      <p
        v-if="gone"
        class="empty-history"
        role="status"
      >
        这件事找不到了，或你已经没有权限。回到「所有交办」里重新打开。
      </p>
      <p
        v-else-if="!task && loading"
        class="empty-history"
        role="status"
      >
        正在读取这件交办的记录…
      </p>
      <div
        v-else-if="!task"
        class="detail-banner is-alert"
        role="alert"
      >
        <p>{{ problem || '暂时没有读到这件交办。' }}</p>
        <button
          type="button"
          class="detail-button is-quiet"
          @click="refresh"
        >
          再读一次
        </button>
      </div>

      <PaperSheet
        v-else
        as="article"
        class="detail-sheet"
        :class="{ 'is-running': moving, 'is-composer-pinned': !moving && composerPinned }"
      >
        <TaskHeader
          ref="headerRef"
          :task="task"
          :receipt-text="receipt?.state === 'available' ? receipt.text : ''"
          :supplement-count="supplements.length"
          :loading="loading"
          :stopping="stopping"
          :confirming-stop="confirmingStop"
          :stop-problem="stopProblem"
          :failures="failures"
          @refresh="refresh"
          @ask-stop="askStop"
          @confirm-stop="confirmStop"
        />

        <p
          v-if="problem && !loading"
          class="detail-banner is-alert detail-top-problem"
          role="alert"
        >
          {{ problem }}
        </p>

        <TaskReceipt
          :task="task"
          :receipt="receipt"
          :loading="receiptLoading"
          :closure-polling="closurePolling"
          :problem="receiptProblem"
          :has-turn-results="results.length > 0"
          @retry="refreshReceipt"
        />

        <TaskAttachments
          :task="task"
          :attachments="attachments"
          :loading="attachmentsLoading"
          :problem="attachmentsProblem"
          :artifacts="artifacts"
          :artifacts-problem="artifactsProblem"
          :selected-set="downloads.selectedSet.value"
          :selected-count="downloads.selectedCount.value"
          :all-selected="downloads.allSelected.value"
          :downloading-target="downloads.downloadingTarget.value"
          :download-problem="downloads.downloadProblem.value"
          :download-notice="downloads.downloadNotice.value"
          @retry-attachments="refreshAttachments"
          @toggle-select="downloads.toggleSelect"
          @toggle-all="downloads.toggleAll"
          @download-one="downloads.downloadOne"
          @download-selected="downloads.downloadSelected"
          @download-all="downloads.downloadAll"
          @focus-fallback="handleAttachmentsFocusFallback"
        />

        <TaskRecords
          :task="task"
          :inputs="inputs"
          :results="results"
          :events="events"
          :loading="recordsLoading"
          :problem="recordsProblem"
          @retry="refreshRecords"
        />

        <TaskFollowUp
          v-model:open="followUpOpen"
          v-model:draft="reply.draft.value"
          v-model:filename="reply.filename.value"
          :task-status="task.status"
          :task-decision="task.decision ?? null"
          :clarify-in-place="reply.clarifyInPlace.value"
          :moving="moving"
          :pinned="composerPinned"
          :transition-tip="transitionTip"
          :retry-append="reply.retryAppend.value"
          :composer-label="reply.composer.value.label"
          :action="reply.composer.value.action"
          :sending="reply.sending.value"
          :held="reply.composer.value.held"
          :trouble="reply.trouble.value"
          :notice="reply.notice.value"
          @send="reply.send"
          @dismiss-pinned="dismissPinned"
          @composer-focus-change="composerFocused = $event"
        />
      </PaperSheet>
    </PrivateGate>
  </div>
</template>
