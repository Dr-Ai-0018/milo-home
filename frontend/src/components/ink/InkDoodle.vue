<script setup lang="ts">
import { computed, ref } from 'vue'
import { doodleArt, useInk } from '../../theme/inks'
import '../../styles/ink-doodle.css'

withDefaults(defineProps<{ size?: number; eggAlign?: 'start' | 'end' }>(), {
  size: 46,
  eggAlign: 'start',
})

const emit = defineEmits<{ egg: [key: string] }>()
const { ink } = useInk()
const taps = ref(0)
const art = computed(() => doodleArt[ink.value.doodle])
const wiggleClass = computed(() => (taps.value === 0 ? '' : taps.value % 2 ? 'is-wa' : 'is-wb'))

// 轻盈的油墨星点与小花星飞溅，替代机械复制原图标。
const bursts = [
  { kind: 'star', dx: '-34px', dy: '-26px', size: 13, rot: '-18deg', tone: 'accent' },
  { kind: 'dot', dx: '-8px', dy: '-38px', size: 8, rot: '0deg', tone: 'ink' },
  { kind: 'star', dx: '30px', dy: '-30px', size: 15, rot: '22deg', tone: 'accent' },
  { kind: 'dot', dx: '42px', dy: '4px', size: 7, rot: '0deg', tone: 'ink' },
  { kind: 'dash', dx: '22px', dy: '26px', size: 12, rot: '38deg', tone: 'accent' },
] as const

function tap() {
  taps.value += 1
  emit('egg', ink.value.key)
}
</script>

<template>
  <span
    class="ink-doodle"
    :class="`egg-${eggAlign}`"
  >
    <button
      type="button"
      class="ink-doodle-button"
      :class="wiggleClass"
      :aria-label="`彩蛋：${ink.mood}`"
      @click="tap"
    >
      <span
        class="ink-doodle-ring"
        aria-hidden="true"
      />
      <svg
        class="ink-doodle-icon"
        aria-hidden="true"
        viewBox="0 0 24 24"
        :width="size"
        :height="size"
      >
        <path
          class="ink-doodle-tint mul"
          :d="art.tint"
        />
        <path
          class="ink-doodle-stroke"
          :d="art.ink"
        />
        <path
          v-if="art.accentStroke"
          class="ink-doodle-accent-stroke"
          :d="art.accentStroke"
        />
      </svg>
      <svg
        v-for="(burst, index) in bursts"
        :key="index"
        class="ink-doodle-spark mul"
        :class="`is-${burst.tone}`"
        aria-hidden="true"
        viewBox="0 0 16 16"
        :width="burst.size"
        :height="burst.size"
        :style="{
          '--dx': burst.dx,
          '--dy': burst.dy,
          '--rot': burst.rot,
          animationDelay: `${index * 45}ms`,
        }"
      >
        <path
          v-if="burst.kind === 'star'"
          d="M8 1.5 Q8.6 6.8 14.5 8 Q8.6 9.2 8 14.5 Q7.4 9.2 1.5 8 Q7.4 6.8 8 1.5 Z"
        />
        <circle
          v-else-if="burst.kind === 'dot'"
          cx="8"
          cy="8"
          r="3.2"
        />
        <path
          v-else
          d="M3 10 Q8 4 13 7"
        />
      </svg>
    </button>
    <span
      class="ink-doodle-egg"
      role="status"
      ><span
        v-if="taps > 0"
        :key="taps"
        class="ink-doodle-egg-line hand"
        >{{ ink.egg }}</span
      ></span
    >
  </span>
</template>
