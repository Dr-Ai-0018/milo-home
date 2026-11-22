<script setup lang="ts">
import type { Conversation } from '../api/client'
import { isoTime } from '../utils/time'
import {
  envelopeLine,
  lastActive,
  letterStatusBadge,
  letterTitle,
  quietLine,
} from '../utils/letters'
import InkStamp from './ink/InkStamp.vue'

defineProps<{ letters: Conversation[] }>()

function badgeOf(letter: Conversation) {
  return letterStatusBadge(letter)
}
</script>

<template>
  <ol class="letter-stack">
    <li
      v-for="letter in letters"
      :key="letter.id"
    >
      <RouterLink
        class="letter-envelope"
        :to="`/letters/${letter.id}`"
      >
        <span
          class="envelope-flap"
          aria-hidden="true"
        />
        <span class="envelope-title">{{ letterTitle(letter) }}</span>
        <span class="envelope-footer">
          <time
            class="envelope-meta"
            :datetime="isoTime(lastActive(letter))"
            >{{ envelopeLine(letter) }}</time
          >
          <InkStamp
            v-if="badgeOf(letter)?.tone === 'waiting'"
            class="envelope-stamp"
            :label="badgeOf(letter)!.label"
            size="sm"
            :tilt="-3"
          />
          <span
            v-else-if="badgeOf(letter)"
            class="envelope-quiet"
            >{{ badgeOf(letter)!.label }}</span
          >
          <span
            v-else-if="quietLine(letter)"
            class="envelope-quiet"
            >{{ quietLine(letter) }}</span
          >
        </span>
      </RouterLink>
    </li>
  </ol>
</template>
