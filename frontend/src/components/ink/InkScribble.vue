<script setup lang="ts">
import '../../styles/ink.css'

withDefaults(defineProps<{ kind?: 'circle' | 'underline'; active?: boolean }>(), {
  kind: 'circle',
  active: false,
})
</script>

<template>
  <span
    class="ink-scribble"
    :class="[`is-${kind}`, { 'is-active': active }]"
  >
    <slot />
    <svg
      class="ink-scribble-mark"
      aria-hidden="true"
      viewBox="0 0 100 40"
      preserveAspectRatio="none"
    >
      <path
        v-if="kind === 'circle'"
        pathLength="1"
        d="M6 24C4 8 34 3 62 5c26 2 34 18 22 28C70 42 22 41 9 31 3 25 12 10 28 7"
      />
      <path
        v-else
        pathLength="1"
        d="M2 30c18-4 40-6 60-4s28 3 36 0"
      />
    </svg>
  </span>
</template>
