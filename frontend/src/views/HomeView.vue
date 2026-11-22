<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import WindowScene from '../components/WindowScene.vue'
import HomeEntranceActions from '../components/home/HomeEntranceActions.vue'
import HomeNotesSection from '../components/home/HomeNotesSection.vue'
import HomeScheduleNotice from '../components/home/HomeScheduleNotice.vue'
import HomeWorksSection from '../components/home/HomeWorksSection.vue'
import InkDoodle from '../components/ink/InkDoodle.vue'
import { usePrivateSession } from '../composables/usePrivateSession'
import { useHomePublicNotes } from '../composables/usePublicNotes'
import { useHomePublicWorks } from '../composables/usePublicWorks'
import { useSchedule } from '../composables/useSchedule'
import '../styles/home-stage.css'

const { homeAccess, refreshHomeAccess } = usePrivateSession()
const { schedule, loading: scheduleLoading, problem, refreshProblem, load } = useSchedule()
const {
  works: publicWorks,
  hasLoaded: worksLoaded,
  legacyMode: worksLegacy,
  loading: worksLoading,
  problem: worksProblem,
  refreshProblem: worksRefreshProblem,
  reload: reloadWorks,
} = useHomePublicWorks()
const {
  notes: publicNotes,
  hasLoaded: notesLoaded,
  legacyMode: notesLegacy,
  loading: notesLoading,
  problem: notesProblem,
  refreshProblem: notesRefreshProblem,
  reload: reloadNotes,
} = useHomePublicNotes()

const leaves = ref(0)
const retryingWork = ref(false)
const workRestored = ref(false)
const entranceSection = ref<HTMLElement | null>(null)

watch(homeAccess, (next) => {
  // 退出、换号或撤权后不再保留上次「已恢复」的局部状态提示。
  if (next !== 'granted') workRestored.value = false
})

// 首页挂载探测已由 App.vue 统一发起；这里只在用户点重试时主动刷新交办资格。
async function onRetryWork() {
  if (retryingWork.value) return
  const active = document.activeElement
  const retryHadFocus = active?.classList.contains('entrance-retry-btn')
  retryingWork.value = true
  workRestored.value = false
  try {
    await refreshHomeAccess()
    if (homeAccess.value === 'granted') workRestored.value = true
  } finally {
    retryingWork.value = false
    await nextTick()
    if (retryHadFocus && document.activeElement === document.body) {
      const destination = homeAccess.value === 'granted' ? '/work' : '/letters'
      entranceSection.value
        ?.querySelector<HTMLAnchorElement>(`a[href="#${destination}"]`)
        ?.focus({ preventScroll: true })
    }
  }
}

function onScheduleAgain() {
  if (!scheduleLoading.value) void load()
}

// 「秋天」主题的彩蛋：一片叶子飘落到入口附近。
function onEgg(key: string) {
  if (key === 'green-red') leaves.value += 1
}
</script>

<template>
  <div class="home-page">
    <div class="home-stage">
      <section
        ref="entranceSection"
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
          :access="homeAccess"
          :retrying-work="retryingWork"
          :restored="workRestored"
          @retry-work="onRetryWork"
        />
      </section>

      <div class="home-window-hero-art">
        <WindowScene />
      </div>

      <div class="home-stage-schedule">
        <HomeScheduleNotice
          :schedule="schedule"
          :loading="scheduleLoading"
          :problem="problem"
          :stale-refresh-problem="refreshProblem"
          @again="onScheduleAgain"
        />
      </div>

      <div class="home-stage-works">
        <HomeWorksSection
          :works="publicWorks"
          :has-loaded="worksLoaded"
          :legacy-mode="worksLegacy"
          :loading="worksLoading"
          :problem="worksProblem"
          :refresh-problem="worksRefreshProblem"
          @reload="reloadWorks"
        />
      </div>

      <div class="home-stage-notes">
        <HomeNotesSection
          :notes="publicNotes"
          :has-loaded="notesLoaded"
          :legacy-mode="notesLegacy"
          :loading="notesLoading"
          :problem="notesProblem"
          :refresh-problem="notesRefreshProblem"
          @reload="reloadNotes"
        />
      </div>
    </div>
  </div>
</template>
