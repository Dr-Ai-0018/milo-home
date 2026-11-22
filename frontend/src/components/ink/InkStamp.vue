<script setup lang="ts">
import { computed, ref, useAttrs } from 'vue'
import '../../styles/ink.css'

const props = withDefaults(
  defineProps<{
    label: string
    interactive?: boolean
    size?: 'lg' | 'md' | 'sm'
    tilt?: number
    zzz?: boolean
    pop?: boolean
    accessibleLabel?: string
  }>(),
  {
    interactive: false,
    size: 'md',
    tilt: -7,
    zzz: false,
    pop: false,
    accessibleLabel: undefined,
  },
)

const emit = defineEmits<{ press: [] }>()
const attrs = useAttrs()
const presses = ref(0)

// 交替两个同样的关键帧类名，让每次点击都能重新盖章。
const pressClass = computed(() => {
  if (presses.value === 0) return props.pop ? 'is-pop' : ''
  return presses.value % 2 ? 'is-press-a' : 'is-press-b'
})

// 外层标了 aria-disabled（比如正在看），按下不再盖一次，也不发 press。
function press() {
  const held = attrs['aria-disabled']
  if (held === true || held === 'true') return
  presses.value += 1
  emit('press')
}
</script>

<template>
  <component
    :is="interactive ? 'button' : 'span'"
    :type="interactive ? 'button' : undefined"
    class="ink-stamp"
    :class="[`is-${size}`, pressClass]"
    :style="{ '--tilt': `${tilt}deg` }"
    :aria-label="accessibleLabel"
    @click="interactive ? press() : undefined"
  >
    {{ label }}
    <span
      v-if="zzz"
      class="ink-zzz"
      aria-hidden="true"
      ><span>z</span><span>z</span><span>z</span></span
    >
  </component>
</template>
