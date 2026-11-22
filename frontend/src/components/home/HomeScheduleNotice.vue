<script setup lang="ts">
import { computed } from 'vue'
import type { Schedule } from '../../api/client'
import ScheduleClock from '../ScheduleClock.vue'
import HandNote from '../ink/HandNote.vue'
import InkStamp from '../ink/InkStamp.vue'
import PaperSheet from '../ink/PaperSheet.vue'
import { SCHEDULE_BOUNDARY_COPY } from '../../data/homeNotes'
import { describePhase } from '../../utils/phase'
import { clockTime, isoTime } from '../../utils/time'
import '../../styles/schedule.css'
import '../../styles/home-preview-sections.css'

const props = defineProps<{
  schedule: Schedule | null
  loading: boolean
  problem: string
  staleRefreshProblem?: string
}>()

const emit = defineEmits<{
  again: []
}>()

const view = computed(() => describePhase(props.schedule, props.problem))
const clockObservedAt = computed(() => props.schedule?.observed_at ?? Math.floor(Date.now() / 1000))
const fallbackTimes = computed(() => props.schedule?.today?.fallback.join('、') ?? '')

function onAgain() {
  if (!props.loading) emit('again')
}
</script>

<template>
  <PaperSheet
    as="section"
    class="home-schedule-card"
    aria-labelledby="home-now-title"
    :aria-busy="loading"
  >
    <div class="home-schedule-body">
      <div class="home-schedule-head">
        <h2
          id="home-now-title"
          class="home-section-eyebrow"
        >
          米洛现在
        </h2>
        <span class="home-schedule-timezone">北京时间 · 实用告示</span>
      </div>

      <div class="home-schedule-main">
        <!-- 不再按 stamp 文案设 :key，避免加载中到真实状态时销毁印章导致连闪两次。 -->
        <InkStamp
          :label="schedule ? view.stamp : problem ? '看不清' : '看一眼…'"
          size="md"
          :tilt="-4"
          interactive
          :zzz="view.resting"
          :accessible-label="`米洛现在：${view.stamp}。点击再看一眼`"
          @press="onAgain"
        />

        <div class="home-schedule-copy">
          <p
            class="home-schedule-line"
            role="status"
          >
            {{ view.line }}
          </p>
          <p class="home-schedule-aside">
            <HandNote
              v-if="view.aside"
              size="sm"
              :tilt="-2"
              >{{ view.aside }}</HandNote
            >
            <span
              v-else
              aria-hidden="true"
              >&nbsp;</span
            >
          </p>
        </div>
      </div>

      <p class="home-schedule-boundary">{{ SCHEDULE_BOUNDARY_COPY }}</p>

      <div class="home-schedule-meta">
        <span v-if="schedule && loading">
          上次 {{ clockTime(schedule.observed_at) }} 的记录 · 正在重新确认…
        </span>
        <span v-else-if="schedule">
          <time :datetime="isoTime(schedule.observed_at)">{{
            clockTime(schedule.observed_at)
          }}</time>
          看的记录 · 北京时间
        </span>
        <span v-else-if="loading"> 正在核对今日作息安排… </span>
        <span v-else> 作息暂不可用，不影响去留信或交办 </span>

        <button
          type="button"
          class="text-button home-schedule-again"
          :aria-disabled="loading"
          :aria-busy="loading"
          @click="onAgain"
        >
          {{ loading ? '正在看…' : '再看一眼' }}
        </button>
      </div>

      <p
        v-if="problem && !schedule"
        class="home-schedule-alert"
        role="alert"
      >
        {{ problem }}
      </p>
      <p
        v-else-if="staleRefreshProblem"
        class="home-schedule-alert is-soft"
        role="status"
      >
        {{ staleRefreshProblem }}
      </p>
    </div>

    <!-- 右侧表盘常驻固定占位，加载中与失败态渲染同尺寸虚线空盘，杜绝下方近况下移跳动。 -->
    <figure class="home-schedule-dial">
      <ScheduleClock
        :windows="schedule?.today?.windows ?? []"
        :observed-at="clockObservedAt"
        :empty="!schedule?.today"
      />
      <figcaption v-if="schedule?.today">
        <span class="clock-key is-working">深色 工作时段</span>
        <span class="clock-key is-winding">亮色 收尾</span>
        <span class="clock-rest">
          空段休息 · 仍可留信
          <template v-if="fallbackTimes"> · 读信点 {{ fallbackTimes }}</template>
        </span>
      </figcaption>
      <figcaption
        v-else
        class="is-placeholder-caption"
      >
        <span>{{
          loading ? '作息表盘正在落笔…' : '今日圆环暂未绘出，公开近况与入口照常可用。'
        }}</span>
      </figcaption>
    </figure>
  </PaperSheet>
</template>
