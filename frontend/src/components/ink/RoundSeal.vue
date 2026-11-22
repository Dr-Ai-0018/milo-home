<script setup lang="ts">
import '../../styles/ink.css'

withDefaults(defineProps<{ top?: string; bottom?: string; size?: number }>(), {
  top: '米洛',
  bottom: '回信',
  size: 58,
})
</script>

<template>
  <svg
    class="round-seal mul"
    aria-hidden="true"
    viewBox="0 0 58 58"
    :width="size"
    :height="size"
  >
    <circle
      cx="29"
      cy="29"
      r="26"
      stroke-width="3"
    />
    <circle
      cx="29"
      cy="29"
      r="21"
      stroke-width="1"
    />
    <text
      x="29"
      y="27"
      font-size="12"
    >
      {{ top }}
    </text>
    <text
      x="29"
      y="41"
      font-size="10"
    >
      {{ bottom }}
    </text>
  </svg>
</template>
