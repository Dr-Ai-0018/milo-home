<script setup lang="ts">
import { inkThemes, previewInk, useInk } from '../../theme/inks'

// 仅在开发环境挂载，方便对照七套油墨；正式页面不给来访者换主题。
const { ink } = useInk()
</script>

<template>
  <details class="ink-preview">
    <summary>油墨预览（开发）</summary>
    <div
      class="ink-preview-list"
      role="group"
      aria-label="预览主题"
    >
      <button
        v-for="theme in inkThemes"
        :key="theme.key"
        type="button"
        :aria-pressed="ink.key === theme.key"
        @click="previewInk(theme.key)"
      >
        <span
          class="ink-preview-swatch"
          aria-hidden="true"
        >
          <span
            class="mul"
            :style="{ background: theme.ink }"
          />
          <span
            class="mul"
            :style="{ background: theme.accent }"
          />
        </span>
        {{ theme.mood }}
      </button>
    </div>
  </details>
</template>
