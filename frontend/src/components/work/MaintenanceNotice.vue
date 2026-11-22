<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type { MaintenanceSummaryView } from '../../utils/maintenance'
import { isoTime, stampTime } from '../../utils/time'
import '../../styles/maintenance-notice.css'

const props = defineProps<{
  summary: MaintenanceSummaryView | null
  visible: boolean
  loading: boolean
  problem: string
}>()

const emit = defineEmits<{
  reload: []
}>()

const expanded = ref(false)
const noticeEl = ref<HTMLElement | null>(null)
const toggleBtnEl = ref<HTMLButtonElement | null>(null)

watch(
  () => props.visible,
  async (shown) => {
    if (shown) return
    const heldFocus = noticeEl.value?.contains(document.activeElement)
    expanded.value = false
    await nextTick()
    if (heldFocus && document.activeElement === document.body) {
      document.querySelector<HTMLElement>('#private-title')?.focus({ preventScroll: true })
    }
  },
)

const toneClass = computed(() => {
  if (!props.summary) return props.problem ? 'is-attention' : 'is-unknown'
  return `is-${props.summary.state}`
})

const badgeLabel = computed(() => {
  if (props.summary) return props.summary.stateLabel
  if (props.loading) return '核对中'
  return props.problem ? '未核对' : '状态未知'
})

const headline = computed(() => {
  if (props.summary) return props.summary.headline
  if (props.loading) return '正在核对资料维护摘要…'
  return props.problem || '暂时没有可读的维护摘要。'
})

function toggle() {
  expanded.value = !expanded.value
}

function onPanelKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape' || !expanded.value) return
  expanded.value = false
  toggleBtnEl.value?.focus({ preventScroll: true })
}

function barWidth(ratio: number | null) {
  if (ratio === null) return '0%'
  if (ratio === 0) return '0%'
  return `${Math.max(2, Math.round(ratio * 1000) / 10)}%`
}
</script>

<template>
  <section
    v-if="visible"
    ref="noticeEl"
    class="maintenance-notice"
    :class="toneClass"
    aria-label="资料维护摘要"
  >
    <div class="maintenance-bar">
      <div class="maintenance-brief">
        <div class="maintenance-summary-line">
          <span
            class="maintenance-dot"
            aria-hidden="true"
          />
          <span class="maintenance-badge">{{ badgeLabel }}</span>
          <p class="maintenance-headline">{{ headline }}</p>
        </div>
        <p
          v-if="summary && summary.hasObservation"
          class="maintenance-observed"
        >
          <span>
            观测于
            <time :datetime="isoTime(summary.observedAt)">{{ stampTime(summary.observedAt) }}</time>
          </span>
          <span
            v-if="summary.expired"
            class="maintenance-expired-tag"
            >· 已过有效期</span
          >
          <span
            v-else-if="problem"
            class="maintenance-stale-tag"
            >· 本次重读未成功</span
          >
        </p>
      </div>

      <button
        ref="toggleBtnEl"
        type="button"
        class="maintenance-toggle"
        :aria-expanded="expanded"
        aria-controls="maintenance-panel"
        @click="toggle"
      >
        <span>{{ expanded ? '收起' : '展开明细' }}</span>
        <span
          class="maintenance-chevron"
          :class="{ 'is-open': expanded }"
          aria-hidden="true"
          >▾</span
        >
      </button>
    </div>

    <div
      v-if="expanded"
      id="maintenance-panel"
      class="maintenance-panel"
      @keydown="onPanelKeydown"
    >
      <div class="maintenance-panel-head">
        <p class="maintenance-panel-meta">
          <template v-if="summary && summary.hasObservation">
            <template v-if="summary.expired">
              这份记录已过期，下方仅保留旧数值与成功时刻，当前状态已降为未知。
            </template>
            <template v-else> 只读呈现最近一次受控采集结果，不随页面自动刷新。 </template>
          </template>
          <template v-else> 维护摘要由后台受控采集，可手动重新核对。 </template>
        </p>
        <button
          type="button"
          class="maintenance-reload"
          :aria-disabled="loading"
          :aria-busy="loading"
          @click="!loading && emit('reload')"
        >
          {{ loading ? '核对中…' : '重新读取' }}
        </button>
      </div>

      <p
        v-if="problem"
        class="maintenance-alert"
        role="alert"
      >
        {{ problem }}
      </p>

      <template v-if="summary && summary.hasObservation">
        <div class="maintenance-group">
          <div class="maintenance-group-head">
            <h3>备份与恢复</h3>
            <span class="maintenance-group-sub">四项独立核对，每日成功不等于整套可恢复</span>
          </div>
          <ul class="maintenance-backup-grid">
            <li
              v-for="item in summary.backups"
              :key="item.kind"
              class="maintenance-item maintenance-backup-cell"
              :class="`is-${item.state}`"
            >
              <div class="maintenance-cell-top">
                <span class="maintenance-item-name">{{ item.label }}</span>
                <span class="maintenance-state-inline">
                  <span
                    class="maintenance-dot"
                    aria-hidden="true"
                  />
                  <span>{{ item.stateLabel }}</span>
                </span>
              </div>
              <p class="maintenance-item-value">{{ item.successLine }}</p>
              <p
                v-if="item.attemptLine"
                class="maintenance-item-attempt"
              >
                {{ item.attemptLine }}
              </p>
              <p class="maintenance-item-note">
                {{ item.note || item.boundary }}
              </p>
            </li>
          </ul>
        </div>

        <div class="maintenance-group">
          <div class="maintenance-group-head">
            <h3>资料容量</h3>
            <span class="maintenance-group-sub">口径各自独立，不相加为磁盘总量</span>
          </div>
          <ul
            v-if="summary.capacity.length"
            class="maintenance-capacity-table"
          >
            <li
              v-for="item in summary.capacity"
              :key="item.kind"
              class="maintenance-item maintenance-cap-row"
              :class="`is-${item.state}`"
            >
              <div class="maintenance-cap-main">
                <span class="maintenance-item-name">{{ item.label }}</span>
                <span
                  v-if="item.note"
                  class="maintenance-cap-sub"
                  >{{ item.note }}</span
                >
              </div>

              <div
                class="maintenance-cap-meter"
                aria-hidden="true"
              >
                <div
                  v-if="item.ratio !== null"
                  class="maintenance-track"
                >
                  <span
                    class="maintenance-fill"
                    :style="{ width: barWidth(item.ratio) }"
                  />
                </div>
                <span
                  v-else-if="!item.complete"
                  class="maintenance-meter-hint"
                  >仅作下界</span
                >
              </div>

              <div class="maintenance-cap-metrics">
                <span
                  class="maintenance-cap-num"
                  :title="item.usageLine"
                  >{{ item.compactValue }}</span
                >
                <span class="maintenance-state-inline">
                  <span
                    class="maintenance-dot"
                    aria-hidden="true"
                  />
                  <span>{{ item.stateLabel }}</span>
                </span>
              </div>
            </li>
          </ul>
          <p
            v-else
            class="maintenance-empty"
          >
            本次摘要未附容量统计项，不表示零占用。
          </p>
        </div>
      </template>

      <p
        v-else-if="summary && !summary.hasObservation"
        class="maintenance-empty"
      >
        目前还没有可核对的容量或备份观察记录；这不表示零占用或从未备份。
      </p>

      <p class="maintenance-readonly-note">
        没有数据的项不画成
        0；封存后磁盘占用不一定立刻减小。此处仅供只读查看，不包含清理、暂停、恢复或文件下载操作。
      </p>
    </div>
  </section>
</template>
