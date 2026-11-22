<script setup lang="ts">
import { useId } from 'vue'
import '../../styles/ink.css'

// 邮戳只做装饰，时间必须另有可读文本。
withDefaults(defineProps<{ date: string; time: string; ring?: string; size?: number }>(), {
  ring: '米洛在家 · 投递 · 北京时间 ·',
  size: 84,
})

const ringId = `postmark-${useId()}`
</script>

<template>
  <svg
    class="postmark mul"
    aria-hidden="true"
    viewBox="0 0 96 96"
    :width="size"
    :height="size"
  >
    <defs>
      <path
        :id="ringId"
        d="M48 48 m-34 0 a34 34 0 1 1 68 0 a34 34 0 1 1 -68 0"
      />
    </defs>
    <circle
      cx="48"
      cy="48"
      r="42"
    />
    <text
      class="postmark-ring"
      font-size="9"
    >
      <textPath :href="`#${ringId}`">{{ ring }}</textPath>
    </text>
    <text
      class="postmark-date"
      x="48"
      y="46"
      font-size="12"
    >
      {{ date }}
    </text>
    <text
      class="postmark-time"
      x="48"
      y="60"
      font-size="11"
    >
      {{ time }}
    </text>
  </svg>
</template>
