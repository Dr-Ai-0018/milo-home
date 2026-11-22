<script setup lang="ts">
import InkTape from './InkTape.vue'
import '../../styles/ink.css'

withDefaults(
  defineProps<{
    as?: string
    paper?: 'plain' | 'grid' | 'ruled'
    tilt?: number
    tape?: 'left' | 'right' | 'none'
  }>(),
  {
    as: 'div',
    paper: 'plain',
    tilt: 0,
    tape: 'none',
  },
)
</script>

<template>
  <component
    :is="as"
    class="paper-sheet"
    :class="[`is-${paper}`, { 'grid-paper': paper === 'grid' }]"
    :style="{ '--tilt': `${tilt}deg` }"
  >
    <InkTape
      v-if="tape !== 'none'"
      class="paper-sheet-tape"
      :class="`is-${tape}`"
      :tilt="tape === 'left' ? -6 : 5"
    />
    <slot />
  </component>
</template>
