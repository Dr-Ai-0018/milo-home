<script setup lang="ts">
import '../../styles/ink.css'

withDefaults(defineProps<{ tilt?: number; width?: number }>(), {
  tilt: -6,
  width: 96,
})
</script>

<template>
  <span
    class="ink-tape mul"
    aria-hidden="true"
    :style="{
      '--tilt': `${tilt}deg`,
      width: `${width}px`,
    }"
  />
</template>
