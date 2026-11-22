<script setup lang="ts">
import { computed } from 'vue'
import type { Schedule } from '../api/client'
import { handCopy } from '../theme/handCopy'
import { dayHours, windowHours } from '../utils/time'

type Window = NonNullable<Schedule['today']>['windows'][number]

// empty：作息还没排好时只画空表盘和当前时刻，不把空白说成「休息」。
const props = defineProps<{ windows: Window[]; observedAt: number; empty?: boolean }>()

// 24 小时表盘：0 点在正上方，顺时针每小时 15°。
const center = 130
const radius = 86

function point(hours: number, r = radius) {
  const angle = (hours / 24) * Math.PI * 2
  return {
    x: +(center + r * Math.sin(angle)).toFixed(1),
    y: +(center - r * Math.cos(angle)).toFixed(1),
  }
}

const ticks = Array.from({ length: 24 }, (_, hour) => {
  const major = hour % 6 === 0
  const inner = point(hour, major ? 95 : 97)
  const outer = point(hour, major ? 103 : 101)
  return {
    hour,
    major,
    x1: inner.x,
    y1: inner.y,
    x2: outer.x,
    y2: outer.y,
  }
})

const arcs = computed(() =>
  props.windows.map((window) => {
    const start = windowHours(window.start)
    const end = windowHours(window.end)
    const from = point(start)
    const to = point(end)
    const large = end - start > 12 ? 1 : 0
    // SVG 单段圆弧的首尾重合时不会画出整圆，全天窗口需分为两个半圆。
    const opposite = point(start + 12)
    return {
      key: `${window.start}-${window.end}`,
      phase: window.phase,
      d:
        end - start === 24
          ? `M${from.x} ${from.y} A${radius} ${radius} 0 0 1 ${opposite.x} ${opposite.y} A${radius} ${radius} 0 0 1 ${to.x} ${to.y}`
          : `M${from.x} ${from.y} A${radius} ${radius} 0 ${large} 1 ${to.x} ${to.y}`,
    }
  }),
)

const handAngle = computed(() => (dayHours(props.observedAt) / 24) * 360)

const summary = computed(() => {
  if (props.empty) return '作息还没排好，圆环暂时是空的。'
  if (!props.windows.length) return '今天的常规安排是休息。'
  const parts = props.windows.map(
    (window) => `${window.start} 到 ${window.end}${window.phase === 'working' ? '工作' : '收尾'}`,
  )
  return `今天的安排：${parts.join('，')}；其余时间休息。`
})

const labels = [
  { text: '0', x: 130, y: 20 },
  { text: '6', x: 244, y: 134 },
  { text: '12', x: 130, y: 249 },
  { text: '18', x: 16, y: 134 },
]
</script>

<template>
  <svg
    class="schedule-clock"
    :class="{ 'is-empty': empty }"
    viewBox="0 0 260 260"
    role="img"
    :aria-label="summary"
  >
    <!-- 柔和表盘纸面底圆 -->
    <circle
      class="clock-face"
      :cx="center"
      :cy="center"
      r="107"
    />
    <!-- 24 小时细刻度 -->
    <g
      class="clock-ticks"
      aria-hidden="true"
    >
      <line
        v-for="tick in ticks"
        :key="tick.hour"
        class="clock-tick"
        :class="{ 'is-major': tick.major }"
        :x1="tick.x1"
        :y1="tick.y1"
        :x2="tick.x2"
        :y2="tick.y2"
      />
    </g>
    <!-- 底轨：空态保留虚线占位，有作息时给细腻连续休息轨道 -->
    <circle
      class="clock-dial"
      :cx="center"
      :cy="center"
      :r="radius"
    />
    <path
      v-for="arc in arcs"
      :key="arc.key"
      class="clock-arc mul"
      :class="`is-${arc.phase}`"
      :d="arc.d"
    />
    <!-- 有配重尾针的表针 -->
    <g
      class="clock-hand"
      :style="{ transform: `rotate(${handAngle}deg)` }"
    >
      <path d="M130 144 L127.6 130 L129 53 L131 53 L132.4 130 Z" />
      <circle
        class="clock-hand-tip mul"
        cx="130"
        cy="48"
        r="4.5"
      />
    </g>
    <circle
      class="clock-pin"
      :cx="center"
      :cy="center"
      r="5.5"
    />
    <circle
      class="clock-pin-dot"
      :cx="center"
      :cy="center"
      r="2"
    />
    <text
      class="clock-center"
      x="130"
      y="107"
    >
      {{ handCopy.clockCenter }}
    </text>
    <text
      v-for="label in labels"
      :key="label.text"
      class="clock-label"
      :x="label.x"
      :y="label.y"
    >
      {{ label.text }}
    </text>
  </svg>
</template>
