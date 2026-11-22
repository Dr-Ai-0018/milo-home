<script setup lang="ts">
import '../../styles/ink.css'

// 手写旁白默认只做装饰；需要读屏读出时设 spoken，且文字不能是唯一的信息来源。
withDefaults(defineProps<{ tilt?: number; size?: 'sm' | 'md' | 'lg'; spoken?: boolean }>(), {
  tilt: -3,
  size: 'md',
  spoken: false,
})
</script>

<template>
  <span
    class="hand hand-note"
    :class="`is-${size}`"
    :style="{ '--tilt': `${tilt}deg` }"
    :aria-hidden="spoken ? undefined : 'true'"
    ><slot
  /></span>
</template>
