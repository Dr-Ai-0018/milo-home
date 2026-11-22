<script setup lang="ts">
// 纸面压痕：章座把纸压下去一点。光从左上来，凹下去的左上内壁背光发暗，右下内壁迎光发亮。
// 压痕不叠底（亮边才看得见），也不随墨量变，干墨的章照样有。
defineProps<{
  id: string
  box: string
  round: boolean
  center: number
  width: number
  height: number
  dent: number
}>()
</script>

<template>
  <svg
    class="stamp-dent"
    :viewBox="box"
    focusable="false"
  >
    <defs>
      <filter
        :id="`d${id}`"
        x="-20%"
        y="-20%"
        width="140%"
        height="140%"
      >
        <feGaussianBlur stdDeviation="0.7" />
      </filter>
      <!-- 光从左上来：凹下去的左上内壁背光发暗，右下内壁迎光发亮。 -->
      <linearGradient
        :id="`ds${id}`"
        x1="0"
        y1="0"
        x2="1"
        y2="1"
      >
        <stop
          offset="0"
          stop-color="#5a4228"
          :stop-opacity="dent * 1.8"
        />
        <stop
          offset="0.55"
          stop-color="#5a4228"
          stop-opacity="0"
        />
      </linearGradient>
      <linearGradient
        :id="`dl${id}`"
        x1="0"
        y1="0"
        x2="1"
        y2="1"
      >
        <stop
          offset="0.45"
          stop-color="#fff"
          stop-opacity="0"
        />
        <stop
          offset="1"
          stop-color="#fff"
          stop-opacity="0.7"
        />
      </linearGradient>
    </defs>
    <g
      :filter="`url(#d${id})`"
      fill="none"
    >
      <template
        v-for="edge in ['ds', 'dl']"
        :key="edge"
      >
        <circle
          v-if="round"
          :cx="center"
          :cy="center"
          :r="center + 3"
          :stroke="`url(#${edge}${id})`"
          stroke-width="1.6"
        />
        <rect
          v-else
          x="-3"
          y="-3"
          :width="width + 6"
          :height="height + 6"
          rx="4"
          :stroke="`url(#${edge}${id})`"
          stroke-width="1.6"
        />
      </template>
    </g>
  </svg>
</template>
