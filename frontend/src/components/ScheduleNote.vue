<script setup lang="ts">
import { computed } from 'vue'
import { useSchedule } from '../composables/useSchedule'
import { handCopy } from '../theme/handCopy'
import { describePhase } from '../utils/phase'
import { clockTime, isoTime, stampTime } from '../utils/time'
import ScheduleClock from './ScheduleClock.vue'
import HandNote from './ink/HandNote.vue'
import InkStamp from './ink/InkStamp.vue'
import '../styles/schedule.css'

// stamp 只盖一枚章，给门牌和页头角落用；点章再看一眼，整句说明交给读屏和首页。
defineProps<{ compact?: boolean; stamp?: boolean }>()

const { schedule, loading, problem, refreshProblem, load } = useSchedule()

// 正在看的时候按钮只标 aria-disabled，焦点留在原处；再按一次在这里挡掉。
function again() {
  if (!loading.value) void load()
}
const activeProblem = computed(() => problem.value || refreshProblem.value)
const view = computed(() => describePhase(schedule.value, activeProblem.value))
const fallbackTimes = computed(() => schedule.value?.today?.fallback.join('、') ?? '')
</script>

<template>
  <InkStamp
    v-if="stamp"
    class="now-mark"
    :label="loading ? '在看' : view.stamp"
    size="sm"
    :tilt="-6"
    interactive
    :accessible-label="`米洛现在：${view.stamp}。${view.line}点一下再看一眼`"
    :title="view.line"
    :aria-busy="loading"
    :aria-disabled="loading"
    @press="again"
  />

  <section
    v-else-if="compact"
    class="now is-compact"
    aria-label="米洛现在"
    :aria-busy="loading"
  >
    <InkStamp
      :label="view.stamp"
      size="sm"
      :tilt="-4"
    />
    <p role="status">{{ view.line }}</p>
    <button
      type="button"
      class="text-button"
      :aria-disabled="loading"
      @click="again"
    >
      {{ loading ? '正在看' : '再看一眼' }}
    </button>
    <p
      v-if="activeProblem"
      class="feedback error"
      role="alert"
    >
      {{ activeProblem }}
    </p>
  </section>

  <section
    v-else
    class="now"
    aria-labelledby="now-title"
    :aria-busy="loading"
  >
    <div class="now-main">
      <h2
        id="now-title"
        class="now-title"
      >
        米洛现在
      </h2>
      <div class="now-stamp-row">
        <span class="now-stamp-wrap">
          <InkStamp
            :key="view.stamp"
            :label="view.stamp"
            size="lg"
            interactive
            pop
            :zzz="view.resting"
            :accessible-label="`米洛现在：${view.stamp}。点一下重新盖章`"
          />
          <HandNote
            class="now-stamp-aside"
            size="sm"
            :tilt="-4"
            >{{ handCopy.stampAside }}</HandNote
          >
        </span>
        <div class="now-words">
          <p
            class="now-line"
            role="status"
          >
            {{ view.line }}
          </p>
          <p
            v-if="view.aside"
            class="now-aside"
            aria-hidden="true"
          >
            <HandNote :tilt="-2">{{ view.aside }}</HandNote>
            <svg
              viewBox="0 0 56 24"
              width="52"
              height="22"
            >
              <path d="M2 14c14-10 30-10 48-2m-8-8 8 8-10 4" />
            </svg>
          </p>
        </div>
      </div>
      <p
        v-if="schedule?.override_ends_at"
        class="now-detail"
      >
        今天的安排临时调整过，到 {{ stampTime(schedule.override_ends_at) }} 恢复平常。
      </p>
      <p
        v-if="schedule?.paused"
        class="now-detail"
      >
        暂停会一直保持，直到米洛那边手动恢复。
      </p>
      <p class="now-honest">自动叫醒还没启用，目前是手动请米洛来读信。</p>
      <p class="now-meta">
        <span v-if="schedule">
          <time :datetime="isoTime(schedule.observed_at)">{{
            clockTime(schedule.observed_at)
          }}</time>
          看的 · 北京时间
        </span>
        <button
          type="button"
          class="text-button"
          :aria-disabled="loading"
          @click="again"
        >
          {{ loading ? '正在看' : '再看一眼' }}
        </button>
      </p>
      <p
        v-if="activeProblem"
        class="feedback error"
        role="alert"
      >
        {{ activeProblem }}
      </p>
    </div>

    <figure
      v-if="schedule?.today"
      class="now-clock"
    >
      <ScheduleClock
        :windows="schedule.today.windows"
        :observed-at="schedule.observed_at"
      />
      <figcaption>
        <span v-if="schedule.source === 'relaxed'">今天放松些。</span>
        <span class="clock-key is-working">深色 上班</span>
        <span class="clock-key is-winding">亮色 收尾</span>
        <span class="clock-rest">空着的地方：休息，信照收</span>
        <span
          v-if="fallbackTimes"
          class="clock-fallback"
          >以后的固定读信点：{{ fallbackTimes }}</span
        >
      </figcaption>
    </figure>
    <figure
      v-else-if="schedule"
      class="now-clock is-empty"
    >
      <ScheduleClock
        :windows="[]"
        :observed-at="schedule.observed_at"
        empty
      />
      <figcaption>
        <span class="clock-rest">作息排好后会画在这里。</span>
      </figcaption>
    </figure>
  </section>
</template>
