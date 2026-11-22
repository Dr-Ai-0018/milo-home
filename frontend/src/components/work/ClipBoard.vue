<script setup lang="ts">
import '../../styles/clipboard.css'

// 夹板：木板、金属夹、下面垫着两张纸，夹着一张打了孔的单子。内容放进插槽。
withDefaults(defineProps<{ small?: boolean }>(), { small: false })
</script>

<template>
  <div
    class="clip-board"
    :class="{ 'is-small': small }"
  >
    <span
      class="clip-under"
      aria-hidden="true"
    />
    <span
      class="clip-under is-second"
      aria-hidden="true"
    />
    <svg
      class="clip-metal"
      viewBox="0 0 132 46"
      aria-hidden="true"
      focusable="false"
    >
      <path
        d="M30 8 Q66 -6 102 8"
        fill="none"
        stroke="#6b6f7a"
        stroke-width="3"
      />
      <rect
        x="14"
        y="10"
        width="104"
        height="30"
        rx="5"
        fill="#a7abb6"
        stroke="#5d616b"
        stroke-width="1.5"
      />
      <rect
        x="18"
        y="13"
        width="96"
        height="6"
        rx="3"
        fill="#c9ccd4"
      />
      <rect
        x="18"
        y="31"
        width="96"
        height="5"
        rx="2.5"
        fill="#8c909b"
      />
      <circle
        cx="30"
        cy="25"
        r="3.2"
        fill="#7b7f89"
        stroke="#4d5059"
      />
      <circle
        cx="102"
        cy="25"
        r="3.2"
        fill="#7b7f89"
        stroke="#4d5059"
      />
    </svg>
    <div class="clip-sheet">
      <span
        class="clip-hole"
        aria-hidden="true"
      />
      <span
        class="clip-hole is-right"
        aria-hidden="true"
      />
      <slot />
    </div>
  </div>
</template>
