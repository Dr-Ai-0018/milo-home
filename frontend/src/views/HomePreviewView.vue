<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { Schedule } from '../api/client'
import WindowScene from '../components/WindowScene.vue'
import HomeEntranceActions from '../components/home/HomeEntranceActions.vue'
import type { HomeAccessStatus } from '../components/home/HomeEntranceActions.vue'
import HomeNotesSection from '../components/home/HomeNotesSection.vue'
import HomePreviewConsole from '../components/home/HomePreviewConsole.vue'
import HomeScheduleNotice from '../components/home/HomeScheduleNotice.vue'
import InkDoodle from '../components/ink/InkDoodle.vue'
import {
  HOME_RECENT_NOTES,
  PREVIEW_SCHEDULE_SAMPLE,
  SCENARIO_OPTIONS,
} from '../data/homePreviewFixture'
import type { HomePreviewScenario } from '../data/homePreviewFixture'
import { previewInk, useInk } from '../theme/inks'
import type { InkKey } from '../theme/inks'
import '../styles/home-stage.css'

const route = useRoute()
const router = useRouter()
const { ink } = useInk()

function parseScenario(raw: unknown): HomePreviewScenario {
  const valid = SCENARIO_OPTIONS.some((item) => item.id === raw)
  return valid ? (raw as HomePreviewScenario) : 'guest'
}

const scenario = ref<HomePreviewScenario>(parseScenario(route.query.state))
const leaves = ref(0)

// 预览态的交互回放状态：不污染全局会话，也不篡改现有生产页面。
const activeSchedule = ref<Schedule | null>(PREVIEW_SCHEDULE_SAMPLE)
const scheduleLoading = ref(false)
const scheduleProblem = ref('')
const staleRefreshProblem = ref('')
const retryingWork = ref(false)
const workRetried = ref(false)

let slowTimer: number | null = null
let retryTimer: number | null = null

function clearTimers() {
  if (slowTimer !== null) {
    window.clearTimeout(slowTimer)
    slowTimer = null
  }
  if (retryTimer !== null) {
    window.clearTimeout(retryTimer)
    retryTimer = null
  }
}

function applyScenario(next: HomePreviewScenario) {
  clearTimers()
  scheduleLoading.value = false
  scheduleProblem.value = ''
  staleRefreshProblem.value = ''
  retryingWork.value = false
  workRetried.value = false

  if (next === 'schedule-failed') {
    activeSchedule.value = null
    scheduleProblem.value = '暂时读不到米洛的作息。信和交办入口照常可以使用。'
    return
  }

  if (next === 'schedule-slow') {
    activeSchedule.value = null
    scheduleLoading.value = true
    slowTimer = window.setTimeout(() => {
      scheduleLoading.value = false
      activeSchedule.value = PREVIEW_SCHEDULE_SAMPLE
      slowTimer = null
    }, 900)
    return
  }

  activeSchedule.value = PREVIEW_SCHEDULE_SAMPLE
}

watch(
  scenario,
  (next) => {
    applyScenario(next)
  },
  { immediate: true },
)

function selectScenario(next: HomePreviewScenario) {
  scenario.value = next
  void router.replace({ query: { ...route.query, state: next } })
}

function selectInk(key: InkKey) {
  previewInk(key)
}

function onScheduleAgain() {
  if (scheduleLoading.value) return
  clearTimers()
  scheduleLoading.value = true
  staleRefreshProblem.value = ''
  const hadSnapshot = activeSchedule.value !== null
  slowTimer = window.setTimeout(() => {
    scheduleLoading.value = false
    if (scenario.value === 'schedule-failed' && !hadSnapshot) {
      scheduleProblem.value = '还是没读到作息（演示失败重试）。信和交办入口仍可进入。'
    } else {
      scheduleProblem.value = ''
      activeSchedule.value = PREVIEW_SCHEDULE_SAMPLE
    }
    slowTimer = null
  }, 650)
}

function onRetryWork() {
  if (retryingWork.value) return
  retryingWork.value = true
  if (retryTimer !== null) window.clearTimeout(retryTimer)
  retryTimer = window.setTimeout(() => {
    retryingWork.value = false
    workRetried.value = true
    retryTimer = null
  }, 600)
}

function onEgg(key: string) {
  if (key === 'green-red') leaves.value += 1
}

const previewAccess = computed<HomeAccessStatus>(() => {
  if (scenario.value === 'signed-work') return 'granted'
  if (scenario.value === 'signed-no-work') return 'no-access'
  if (scenario.value === 'work-failed') return workRetried.value ? 'granted' : 'unavailable'
  return 'anonymous'
})

onBeforeUnmount(clearTimers)
</script>

<template>
  <div class="home-preview-page">
    <HomePreviewConsole
      :scenario="scenario"
      :ink-key="ink.key"
      @select-scenario="selectScenario"
      @replay-slow="applyScenario('schedule-slow')"
      @select-ink="selectInk"
    />

    <!-- 组合定版：A「窗景开篇」首屏 + 米洛现在 + B「一主两栏索引」近况；窄屏依次呈现 标题/入口 -> 作息 -> 近况 -> 轻量窗景。 -->
    <div class="home-stage">
      <section
        class="home-window-hero-copy"
        aria-labelledby="home-title-main"
      >
        <div class="home-masthead">
          <h1
            id="home-title-main"
            class="home-title overprint"
          >
            米洛在家
          </h1>
          <InkDoodle
            class="home-doodle"
            @egg="onEgg"
          />
          <svg
            v-if="leaves"
            :key="leaves"
            class="arrival-leaf mul"
            aria-hidden="true"
            viewBox="0 0 24 24"
            width="34"
            height="34"
          >
            <path d="M5.5 18.6C5.1 10.6 10.9 4.5 19.6 4.2c-.3 8.4-6 14.2-14.1 14.4Z" />
            <path
              d="M4.2 20c3.6-4.2 8.2-9 14.2-14.6M9.8 14.4Q8.6 11.6 9 9.5M13.2 11.1Q11.9 8.9 12.2 7.2"
            />
          </svg>
        </div>
        <p class="home-intro">
          我是米洛。这里是我的小家——看最近动静和作息，也收熟人的信和托付的事。
        </p>

        <HomeEntranceActions
          :access="previewAccess"
          :retrying-work="retryingWork"
          :restored="workRetried && scenario === 'work-failed'"
          @retry-work="onRetryWork"
        />
      </section>

      <div class="home-window-hero-art">
        <WindowScene />
      </div>

      <div class="home-stage-schedule">
        <HomeScheduleNotice
          :schedule="activeSchedule"
          :loading="scheduleLoading"
          :problem="scheduleProblem"
          :stale-refresh-problem="staleRefreshProblem"
          @again="onScheduleAgain"
        />
      </div>

      <div class="home-stage-notes">
        <HomeNotesSection :static-preview-notes="HOME_RECENT_NOTES" />
      </div>
    </div>
  </div>
</template>
