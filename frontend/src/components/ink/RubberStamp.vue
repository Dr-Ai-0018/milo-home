<script setup lang="ts">
import { computed, useId } from 'vue'
import { stampLook } from '../../utils/stampInk'
import StampDent from './StampDent.vue'
import StampInkFilters from './StampInkFilters.vue'
import '../../styles/stamp.css'

// 真橡皮章盖出来的印：边缘发毛、有缺口、一边实一边虚、轮廓积墨、偶尔重影或溅点，
// 纸面被压下去一点。样子只由 seed 决定；fresh 时播放一次落章、按压、抬起。
const props = withDefaults(
  defineProps<{
    label: string
    date: string
    seed: number
    shape?: 'rect' | 'round'
    tone?: 'ink' | 'stamp'
    ink?: number
    fresh?: boolean
    // 同时来了好几条新记录时，一枚接一枚地盖。
    delay?: number
  }>(),
  { shape: 'rect', tone: 'ink', ink: 1, fresh: false, delay: 0 },
)

const PAD = 12
const id = useId()
const round = computed(() => props.shape === 'round')
const width = computed(() => (round.value ? 92 : 84))
const height = computed(() => (round.value ? 92 : 44))
const look = computed(() => stampLook(props.seed, props.ink, round.value ? 9 : 5))
const box = computed(() => `${-PAD} ${-PAD} ${width.value + 2 * PAD} ${height.value + 2 * PAD}`)
const center = computed(() => width.value / 2)

const layers = computed(() => {
  const { ghost, inkOpacity, poolOpacity } = look.value
  return [
    ...(ghost
      ? [{ key: 'ghost', filter: `f${id}`, opacity: 0.14, x: ghost.x, y: ghost.y, pool: false }]
      : []),
    { key: 'main', filter: `f${id}`, opacity: inkOpacity, x: 0, y: 0, pool: false },
    { key: 'pool', filter: `p${id}`, opacity: poolOpacity, x: 0, y: 0, pool: true },
  ]
})

const dots = computed(() =>
  look.value.dots.map(({ angle, reach, size }) => ({
    x: (width.value / 2 + Math.cos(angle) * (width.value / 2 + reach)).toFixed(1),
    y: (height.value / 2 + Math.sin(angle) * (height.value / 2 + reach)).toFixed(1),
    size,
  })),
)

const place = computed(() => ({
  '--rot': `${look.value.rotation}deg`,
  '--nx': `${look.value.nudge.x}px`,
  '--ny': `${look.value.nudge.y}px`,
  '--delay': `${props.delay}ms`,
  width: `${width.value + 2 * PAD}px`,
  height: `${height.value + 2 * PAD}px`,
  margin: `${-PAD}px`,
}))
</script>

<template>
  <span
    class="rubber-stamp"
    :class="[`is-${shape}`, `tone-${tone}`, { 'is-fresh': fresh }]"
    :style="place"
    aria-hidden="true"
  >
    <StampDent
      :id="id"
      :box="box"
      :round="round"
      :center="center"
      :width="width"
      :height="height"
      :dent="look.dent"
    />

    <svg
      class="stamp-print"
      :viewBox="box"
      focusable="false"
    >
      <defs>
        <StampInkFilters
          :id="id"
          :noise="look.noise"
          :rough="look.rough"
          :dry="look.dry"
        />
        <linearGradient
          :id="`g${id}`"
          :gradientTransform="`rotate(${look.pressAngle} .5 .5)`"
        >
          <stop
            offset="0"
            stop-color="#fff"
          />
          <stop
            :offset="look.pressMid"
            stop-color="#fff"
          />
          <stop
            offset="1"
            stop-color="#fff"
            :stop-opacity="look.pressLow"
          />
        </linearGradient>
        <mask :id="`m${id}`">
          <rect
            :x="-PAD"
            :y="-PAD"
            :width="width + 2 * PAD"
            :height="height + 2 * PAD"
            :fill="`url(#g${id})`"
          />
        </mask>
        <path
          v-if="round"
          :id="`a${id}`"
          :d="`M ${center - 31} ${center + 1} A 31 31 0 0 1 ${center + 31} ${center + 1}`"
          fill="none"
        />
      </defs>

      <g :mask="`url(#m${id})`">
        <g
          v-for="layer in layers"
          :key="layer.key"
          :class="{ 'stamp-pool': layer.pool }"
          :filter="`url(#${layer.filter})`"
          :opacity="layer.opacity"
          :transform="layer.x || layer.y ? `translate(${layer.x} ${layer.y})` : undefined"
        >
          <template v-if="round">
            <circle
              :cx="center"
              :cy="center"
              r="44"
              stroke-width="3.6"
              pathLength="100"
              :stroke-dasharray="look.nicks"
            />
            <circle
              :cx="center"
              :cy="center"
              r="38.5"
              stroke-width="1"
            />
            <text
              class="stamp-date"
              font-size="8"
              letter-spacing="1"
            >
              <textPath
                :href="`#a${id}`"
                startOffset="50%"
                text-anchor="middle"
              >
                {{ date }}
              </textPath>
            </text>
            <line
              :x1="center - 26"
              :y1="center + 5"
              :x2="center + 26"
              :y2="center + 5"
              stroke-width="1"
            />
            <text
              class="stamp-label"
              :x="center"
              :y="center + 24"
              :font-size="label.length > 2 ? 17 : 20"
              letter-spacing="1"
            >
              {{ label }}
            </text>
          </template>
          <template v-else>
            <rect
              x="2"
              y="2"
              :width="width - 4"
              :height="height - 4"
              rx="3"
              stroke-width="2.8"
              pathLength="100"
              :stroke-dasharray="look.nicks"
            />
            <rect
              x="5.8"
              y="5.8"
              :width="width - 11.6"
              :height="height - 11.6"
              rx="2"
              stroke-width="0.9"
            />
            <text
              class="stamp-label"
              :x="center"
              y="24"
              :font-size="label.length > 2 ? 14 : 15"
              letter-spacing="2"
            >
              {{ label }}
            </text>
            <text
              class="stamp-date"
              :x="center"
              y="35"
              font-size="7.5"
            >
              {{ date }}
            </text>
          </template>
        </g>
      </g>

      <circle
        v-if="round && look.baseEdge !== null"
        class="stamp-base"
        :cx="center"
        :cy="center"
        :r="center + 3"
        pathLength="100"
        :stroke-dasharray="`0 ${look.baseEdge * 100} 22 100`"
      />
      <rect
        v-else-if="look.baseEdge !== null"
        class="stamp-base"
        x="-2"
        y="-2"
        :width="width + 4"
        :height="height + 4"
        rx="4"
        pathLength="100"
        :stroke-dasharray="`0 ${look.baseEdge * 100} 22 100`"
      />
      <circle
        v-for="(dot, index) in dots"
        :key="index"
        class="stamp-dot"
        :cx="dot.x"
        :cy="dot.y"
        :r="dot.size"
      />
    </svg>

    <span
      v-if="fresh"
      class="stamp-block"
      :style="{ inset: `${PAD - 3}px` }"
    />
  </span>
</template>
