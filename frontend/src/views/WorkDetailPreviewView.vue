<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { WorkAttachment } from '../api/client'
import PaperSheet from '../components/ink/PaperSheet.vue'
import TaskAttachments from '../components/work/TaskAttachments.vue'
import TaskBrief from '../components/work/TaskBrief.vue'
import TaskFollowUp from '../components/work/TaskFollowUp.vue'
import TaskHeader from '../components/work/TaskHeader.vue'
import TaskReceipt from '../components/work/TaskReceipt.vue'
import TaskStamps from '../components/work/TaskStamps.vue'
import TurnAnswers from '../components/work/TurnAnswers.vue'
import WorkComposer from '../components/work/WorkComposer.vue'
import { PREVIEW_INK_CHOICES } from '../data/homePreviewFixture'
import {
  PREVIEW_ATTACHMENTS_FULL,
  PREVIEW_EVENTS_COMPLETED,
  PREVIEW_EVENTS_RUNNING,
  PREVIEW_INPUTS,
  PREVIEW_RECEIPT_LONG,
  PREVIEW_RESULTS,
  PREVIEW_TASK_COMPLETED,
  PREVIEW_TASK_RUNNING,
  WORK_PREVIEW_SCENARIOS,
  WORK_REUSE_RULES,
} from '../data/workDetailPreviewFixture'
import type { WorkDetailPreviewScenario } from '../data/workDetailPreviewFixture'
import { previewInk, useInk } from '../theme/inks'
import '../styles/home-preview-console.css'
import '../styles/work.css'
import '../styles/work-detail.css'
import '../styles/work-delivery.css'
import '../styles/work-detail-preview.css'

const route = useRoute()
const router = useRouter()
const { ink } = useInk()

function parseScenario(raw: unknown): WorkDetailPreviewScenario {
  return WORK_PREVIEW_SCENARIOS.some((s) => s.id === raw)
    ? (raw as WorkDetailPreviewScenario)
    : 'completed-long'
}

const scenario = ref<WorkDetailPreviewScenario>(parseScenario(route.query.state))
const barCollapsed = ref(false)

// 演示态状态：模拟中途完成保留草稿，以及附件局部失败后的 :aria-disabled 原地重试恢复。
const finishedMidTyping = ref(false)
const draft = ref('等会儿顺便把昨天那份旧列表也核对一下，我还没写完这句话…')
const composerNotice = ref('')
const sending = ref(false)
const followUpOpen = ref(false)
const attachmentsLoading = ref(false)
const attachmentsRecovered = ref(false)
const selectedIds = ref<string[]>(['att-1'])
const downloadNotice = ref('')
const confirmingStop = ref(false)
const stopNotice = ref('')

watch(scenario, (next) => {
  finishedMidTyping.value = false
  composerNotice.value = ''
  sending.value = false
  followUpOpen.value = false
  attachmentsLoading.value = false
  attachmentsRecovered.value = false
  confirmingStop.value = false
  stopNotice.value = ''
  downloadNotice.value = ''
  draft.value =
    next === 'running-typing' ? '等会儿顺便把昨天那份旧列表也核对一下，我还没写完这句话…' : ''
})

function selectScenario(next: WorkDetailPreviewScenario) {
  scenario.value = next
  void router.replace({ query: { ...route.query, state: next } })
}

const activeTask = computed(() =>
  scenario.value === 'running-typing' && !finishedMidTyping.value
    ? PREVIEW_TASK_RUNNING
    : PREVIEW_TASK_COMPLETED,
)

const isMoving = computed(() => activeTask.value.status === 'running')
const activeReceipt = computed(() => (isMoving.value ? null : PREVIEW_RECEIPT_LONG))
const activeAttachments = computed(() => {
  if (isMoving.value) return { state: 'none' as const, attachments: [] }
  if (scenario.value === 'attachments-failed' && !attachmentsRecovered.value) return null
  return PREVIEW_ATTACHMENTS_FULL
})

const attachmentsProblem = computed(() =>
  scenario.value === 'attachments-failed' && !attachmentsRecovered.value
    ? '暂时没读到交付附件列表（演示局部容错）；上面的文字回执仍可阅读。'
    : '',
)

const activeEvents = computed(() =>
  isMoving.value ? PREVIEW_EVENTS_RUNNING : PREVIEW_EVENTS_COMPLETED,
)
const activeResults = computed(() =>
  isMoving.value ? PREVIEW_RESULTS.slice(0, 1) : PREVIEW_RESULTS,
)
const selectedSet = computed(() => new Set(selectedIds.value))
const selectedCount = computed(() => selectedIds.value.length)
const allSelected = computed(
  () =>
    (activeAttachments.value?.attachments.length ?? 0) > 0 &&
    selectedCount.value === (activeAttachments.value?.attachments.length ?? 0),
)

function onRetryAttachments() {
  if (attachmentsLoading.value) return
  attachmentsLoading.value = true
  window.setTimeout(() => {
    attachmentsLoading.value = false
    attachmentsRecovered.value = true
  }, 600)
}

function toggleSelect(id: string) {
  selectedIds.value = selectedSet.value.has(id)
    ? selectedIds.value.filter((item) => item !== id)
    : [...selectedIds.value, id]
}

function toggleAll() {
  const all = activeAttachments.value?.attachments.map((item) => item.id) ?? []
  selectedIds.value = allSelected.value ? [] : all
}

function onPreviewDownload(label: string) {
  downloadNotice.value = `演示态已触发「${label}」（不发送真实网络请求）。`
}

function onSendPreview() {
  if (!draft.value.trim() || sending.value) return
  sending.value = true
  window.setTimeout(() => {
    sending.value = false
    composerNotice.value = `演示已收下这句话：「${draft.value.trim()}」`
    draft.value = ''
    finishedMidTyping.value = false
  }, 450)
}

function onAskStop(open: boolean) {
  confirmingStop.value = open
  stopNotice.value = ''
}

function onConfirmStop() {
  confirmingStop.value = false
  stopNotice.value = '这是预览样板：真实交办点确认后会请求停下当前这一轮。'
}

const currentScenarioMeta = computed(
  () =>
    WORK_PREVIEW_SCENARIOS.find((item) => item.id === scenario.value) ?? WORK_PREVIEW_SCENARIOS[0],
)
</script>

<template>
  <div class="work-desk work-task-page work-detail-preview-page">
    <aside
      class="preview-console"
      aria-label="交办详情规则试用预览台"
    >
      <div class="preview-console-bar">
        <div class="preview-console-heading">
          <span class="preview-badge">第二样板预览</span>
          <strong>交办详情视觉与交互规则校验</strong>
          <span class="preview-summary">{{ currentScenarioMeta?.label }}</span>
        </div>
        <button
          type="button"
          class="text-button preview-collapse-btn"
          :aria-expanded="!barCollapsed"
          @click="barCollapsed = !barCollapsed"
        >
          {{ barCollapsed ? '展开场景面板' : '收起面板' }}
        </button>
      </div>

      <div
        v-if="!barCollapsed"
        class="preview-console-body"
      >
        <div class="preview-control-row">
          <span class="preview-control-label">检验场景</span>
          <div
            class="preview-chip-group"
            role="group"
            aria-label="切换交办详情测试场景"
          >
            <button
              v-for="item in WORK_PREVIEW_SCENARIOS"
              :key="item.id"
              type="button"
              class="preview-chip is-sm"
              :aria-pressed="scenario === item.id"
              @click="selectScenario(item.id)"
            >
              {{ item.label }}
            </button>
            <button
              v-if="scenario === 'running-typing' && !finishedMidTyping"
              type="button"
              class="preview-chip is-sm is-replay"
              @click="finishedMidTyping = true"
            >
              模拟打字中途任务完成
            </button>
          </div>
        </div>

        <div class="preview-control-row">
          <span class="preview-control-label">代表色档</span>
          <div
            class="preview-chip-group"
            role="group"
            aria-label="对比交办详情代表色档"
          >
            <button
              v-for="choice in PREVIEW_INK_CHOICES"
              :key="choice.key"
              type="button"
              class="preview-chip is-sm"
              :aria-pressed="ink.key === choice.key"
              @click="previewInk(choice.key)"
            >
              {{ choice.label }}
            </button>
          </div>
          <span class="preview-variant-note">{{ currentScenarioMeta?.note }}</span>
        </div>

        <details class="preview-rules-drawer">
          <summary>查看本页复用的视觉与架构规则（5 条）</summary>
          <dl class="preview-rules-list">
            <div
              v-for="item in WORK_REUSE_RULES"
              :key="item.title"
              class="preview-rule-item"
            >
              <dt>{{ item.title }}</dt>
              <dd>{{ item.rule }}</dd>
            </div>
          </dl>
        </details>
      </div>
    </aside>

    <div class="detail-top-bar">
      <RouterLink
        class="text-button back-link"
        to="/preview/home"
      >
        ← 回首页预览
      </RouterLink>
      <p class="work-offline">用合成单检验同一套纸面质感、局部容错与草稿保位。</p>
    </div>

    <PaperSheet
      as="article"
      class="detail-sheet"
    >
      <TaskHeader
        :task="activeTask"
        :supplement-count="1"
        :loading="false"
        :stopping="false"
        :confirming-stop="confirmingStop"
        :stop-problem="stopNotice"
        :failures="0"
        @ask-stop="onAskStop"
        @confirm-stop="onConfirmStop"
      />

      <!-- 检验重点 1：正在输入补话草稿期间任务突然结束，输入框与草稿不销毁也不抢焦点。 -->
      <section
        v-if="scenario === 'running-typing'"
        class="work-preview-composer-keep"
        aria-label="继续补话与草稿保护"
      >
        <p
          v-if="finishedMidTyping"
          class="work-preview-finished-tip"
          role="status"
        >
          这件交办刚刚忙完了——你刚才写的这句话还留在这里，可以作为继续追问发给米洛，或先往下看回执。
        </p>
        <WorkComposer
          v-model="draft"
          :label="finishedMidTyping ? '继续补充给米洛（保留你的草稿）' : '趁它还在做，再补一句话'"
          :action="finishedMidTyping ? '作为新追问发送' : '先补进去'"
          :sending="sending"
          held=""
          :notice="composerNotice"
          @send="onSendPreview"
        />
      </section>

      <!-- 检验重点 2：已结束单先出整单回执与交付附件，附件报错只在本块原地恢复，不遮挡回执。 -->
      <template v-if="!isMoving">
        <TaskReceipt
          :task="activeTask"
          :receipt="activeReceipt"
          :loading="false"
          :closure-polling="false"
          problem=""
          :has-turn-results="activeResults.length > 0"
        />

        <TaskAttachments
          :task="activeTask"
          :attachments="activeAttachments"
          :loading="attachmentsLoading"
          :problem="attachmentsProblem"
          :artifacts="[]"
          artifacts-problem=""
          :selected-set="selectedSet"
          :selected-count="selectedCount"
          :all-selected="allSelected"
          :downloading-target="null"
          download-problem=""
          :download-notice="downloadNotice"
          @retry-attachments="onRetryAttachments"
          @toggle-select="toggleSelect"
          @toggle-all="toggleAll"
          @download-one="(att: WorkAttachment) => onPreviewDownload(att.filename)"
          @download-selected="() => onPreviewDownload(`勾选的 ${selectedCount} 份`)"
          @download-all="() => onPreviewDownload('全部打包 ZIP')"
        />
      </template>

      <!-- 检验重点 3：借鉴“先看当前结论，细节按需就地展开”，把原交办要求与逐轮历史温和折起。 -->
      <details
        class="work-preview-collapse"
        :open="isMoving"
      >
        <summary>
          {{
            isMoving
              ? '查看原交办要求与目前经过'
              : '展开原交办要求与逐轮处理记录（2 轮 · 4 条节点）'
          }}
        </summary>
        <div class="work-preview-collapse-body">
          <TaskBrief
            :task="activeTask"
            :initial="PREVIEW_INPUTS[0] ?? null"
            :supplements="PREVIEW_INPUTS.slice(1)"
          />
          <TaskStamps
            :task="activeTask"
            :events="[...activeEvents]"
            :inputs="[...PREVIEW_INPUTS]"
          />
          <TurnAnswers
            :results="[...activeResults]"
            :inputs="[...PREVIEW_INPUTS]"
          />
        </div>
      </details>

      <TaskFollowUp
        v-if="!isMoving && scenario !== 'running-typing'"
        v-model:open="followUpOpen"
        v-model:draft="draft"
        filename=""
        :task-status="activeTask.status"
        :retry-append="false"
        action="作为补充发给米洛"
        :sending="sending"
        held=""
        trouble=""
        :notice="composerNotice"
        @send="onSendPreview"
      />
    </PaperSheet>
  </div>
</template>
