<script setup lang="ts">
import { computed, useId } from 'vue'
import { rng } from '../../utils/stampInk'
import '../../styles/stamp.css'

// 情况不清时不盖真章：铅笔虚线圈一下，写明在等核对。
const props = withDefaults(defineProps<{ seed: number; top?: string; bottom?: string }>(), {
  top: '情况不清',
  bottom: '等米洛核对',
})

const id = useId()
const noise = computed(() => Math.floor(rng(props.seed)() * 99) + 1)
</script>

<template>
  <span
    class="pencil-ring"
    aria-hidden="true"
  >
    <svg
      width="92"
      height="92"
      viewBox="0 0 92 92"
      focusable="false"
    >
      <defs>
        <filter :id="`w${id}`">
          <feTurbulence
            type="fractalNoise"
            baseFrequency="0.9"
            numOctaves="1"
            :seed="noise"
            result="grain"
          />
          <feDisplacementMap
            in="SourceGraphic"
            in2="grain"
            scale="1.6"
            xChannelSelector="R"
            yChannelSelector="G"
          />
        </filter>
      </defs>
      <g :filter="`url(#w${id})`">
        <circle
          cx="46"
          cy="46"
          r="42"
          stroke-width="1.2"
          stroke-dasharray="5 4"
        />
        <circle
          cx="46.6"
          cy="45.6"
          r="36"
          stroke-width="0.7"
          stroke-dasharray="2 5"
        />
      </g>
      <text
        x="46"
        y="45"
        font-size="13"
      >
        {{ top }}
      </text>
      <text
        x="46"
        y="60"
        font-size="8.5"
      >
        {{ bottom }}
      </text>
    </svg>
  </span>
</template>
