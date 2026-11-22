<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type { WorkArtifact, WorkAttachment, WorkAttachmentList, WorkTask } from '../../api/client'
import { formatBytes, isMoving, shortDigest } from '../../utils/work'
import TaskArtifacts from './TaskArtifacts.vue'

const props = defineProps<{
  task: WorkTask
  attachments: WorkAttachmentList | null
  loading: boolean
  problem: string
  artifacts: WorkArtifact[]
  artifactsProblem: string
  selectedSet: Set<string>
  selectedCount: number
  allSelected: boolean
  downloadingTarget: string | null
  downloadProblem: string
  downloadNotice: string
}>()

const emit = defineEmits<{
  retryAttachments: []
  toggleSelect: [attachmentId: string]
  toggleAll: []
  downloadOne: [attachment: WorkAttachment]
  downloadSelected: []
  downloadAll: []
  focusFallback: []
}>()

const moving = computed(() => isMoving(props.task))
const showLegacy = computed(
  () => !!props.task.deliverable || props.artifacts.length > 0 || !!props.artifactsProblem,
)
const hasSealedBlock = computed(
  () =>
    !!props.problem ||
    (props.loading && !props.attachments) ||
    (!!props.attachments && props.attachments.state !== 'none'),
)
const showSection = computed(() => hasSealedBlock.value || showLegacy.value)
const files = computed(() => props.attachments?.attachments ?? [])
const pendingNote = computed(() => {
  if (props.task.status === 'needs_reconciliation') {
    return '这件还在等米洛核对，如有产出文件会在核对并封存后显示；需要时可点右上角重读附件。'
  }
  return moving.value
    ? '产出文件会在处理结束后封存到这里。'
    : '产出文件正在封存，请稍候或点右上角重读附件。'
})

const headingEl = ref<HTMLElement | null>(null)
const pendingRetryBtn = ref<HTMLButtonElement | null>(null)
const alertRetryBtn = ref<HTMLButtonElement | null>(null)

function handleRetryAttachments() {
  if (props.loading) return
  emit('retryAttachments')
}

let restoreFocusOnPatch = false

watch(
  [() => props.problem, () => props.attachments?.state, () => props.loading, moving],
  () => {
    const active = document.activeElement
    if (active && (active === pendingRetryBtn.value || active === alertRetryBtn.value)) {
      restoreFocusOnPatch = true
    }
  },
  { flush: 'sync' },
)

watch(
  [() => props.problem, () => props.attachments?.state, () => props.loading, moving],
  async () => {
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
      if (headingEl.value) headingEl.value.focus({ preventScroll: true })
      else emit('focusFallback')
    }
  },
)
</script>

<template>
  <section
    v-if="showSection"
    class="detail-section attachments-section"
    :aria-labelledby="
      hasSealedBlock || artifactsProblem ? 'attachments-heading' : 'artifacts-title'
    "
  >
    <div
      v-if="hasSealedBlock || artifactsProblem"
      class="section-head"
    >
      <h2
        id="attachments-heading"
        ref="headingEl"
        tabindex="-1"
      >
        成果与附件
      </h2>
      <button
        v-if="attachments?.state === 'pending' && !moving && !problem"
        ref="pendingRetryBtn"
        type="button"
        class="detail-button is-quiet"
        :aria-disabled="loading"
        :aria-busy="loading"
        @click="handleRetryAttachments"
      >
        {{ loading ? '正在读取…' : '重读附件' }}
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
        @click="handleRetryAttachments"
      >
        {{ loading ? '正在重读…' : '重读附件' }}
      </button>
    </div>

    <p
      v-if="loading && !attachments && !problem"
      class="detail-empty"
      role="status"
    >
      正在读取封存附件…
    </p>

    <template v-else-if="attachments?.state === 'available'">
      <p
        v-if="files.length === 0"
        class="detail-empty"
      >
        这次没有可下载的封存附件。
      </p>

      <template v-else>
        <div class="attachments-bar">
          <label class="attachment-select-all">
            <input
              type="checkbox"
              :checked="allSelected"
              @change="emit('toggleAll')"
            />
            <span>全选（已选 {{ selectedCount }} / {{ files.length }}）</span>
          </label>

          <div class="attachments-bar-actions">
            <button
              v-if="files.length > 1"
              type="button"
              class="detail-button"
              :aria-disabled="selectedCount === 0 || !!downloadingTarget"
              :aria-busy="downloadingTarget === 'selected'"
              @click="selectedCount > 0 && !downloadingTarget && emit('downloadSelected')"
            >
              {{
                downloadingTarget === 'selected'
                  ? '正在打包所选…'
                  : `打包下载所选（${selectedCount}）`
              }}
            </button>
            <button
              type="button"
              class="detail-button is-primary"
              :aria-disabled="!!downloadingTarget"
              :aria-busy="downloadingTarget === 'all'"
              @click="!downloadingTarget && emit('downloadAll')"
            >
              {{ downloadingTarget === 'all' ? '正在打包全部…' : '打包下载全部 (.zip)' }}
            </button>
          </div>
        </div>

        <p
          v-if="downloadProblem"
          class="detail-banner is-alert"
          role="alert"
        >
          {{ downloadProblem }}
        </p>
        <p
          v-else-if="downloadNotice"
          class="detail-banner is-ok"
          role="status"
        >
          {{ downloadNotice }}
        </p>

        <ul class="attachment-list">
          <li
            v-for="item in files"
            :key="item.id"
            class="attachment-row"
            :class="{ 'is-picked': selectedSet.has(item.id) }"
          >
            <label class="attachment-pick">
              <input
                type="checkbox"
                :checked="selectedSet.has(item.id)"
                :aria-label="`选择 ${item.filename}`"
                @change="emit('toggleSelect', item.id)"
              />
            </label>

            <div class="attachment-info">
              <span class="attachment-name">{{ item.filename }}</span>
              <span class="attachment-meta">
                <span>{{ formatBytes(item.size_bytes) }}</span>
                <details
                  v-if="item.sha256"
                  class="attachment-digest"
                >
                  <summary>SHA-256 · {{ shortDigest(item.sha256) }}</summary>
                  <code>{{ item.sha256 }}</code>
                </details>
              </span>
            </div>

            <button
              type="button"
              class="detail-button is-quiet attachment-download"
              :aria-disabled="!!downloadingTarget"
              :aria-busy="downloadingTarget === item.id"
              :aria-label="
                downloadingTarget === item.id
                  ? `正在保存 ${item.filename}`
                  : `下载 ${item.filename}`
              "
              @click="!downloadingTarget && emit('downloadOne', item)"
            >
              {{ downloadingTarget === item.id ? '正在保存…' : '下载' }}
            </button>
          </li>
        </ul>
      </template>
    </template>

    <p
      v-else-if="attachments?.state === 'pending'"
      class="detail-note-card"
      role="status"
    >
      {{ pendingNote }}
    </p>

    <p
      v-else-if="attachments?.state === 'unavailable'"
      class="detail-note-card"
      role="status"
    >
      这件没能把附件封存好，清单与打包下载暂不可用。
    </p>

    <div
      v-if="showLegacy"
      class="legacy-artifacts"
    >
      <p
        v-if="artifactsProblem"
        class="detail-banner is-alert"
        role="alert"
      >
        {{ artifactsProblem }}
      </p>
      <TaskArtifacts
        v-else
        :artifacts="artifacts"
        :task="task"
      />
    </div>
  </section>
</template>
