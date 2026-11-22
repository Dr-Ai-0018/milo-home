<script setup lang="ts">
import ScheduleNote from './ScheduleNote.vue'
import PaperSheet from './ink/PaperSheet.vue'
import '../styles/mailbox.css'

// 常来的人进书信页先看到自己的信箱门牌：名字、待回数和作息章在牌面上，下沿是开新信入口。
defineProps<{ name: string; line: string }>()
</script>

<template>
  <PaperSheet
    as="section"
    class="mailbox-plate"
    aria-labelledby="private-title"
  >
    <div class="plate-face">
      <h1
        id="private-title"
        class="overprint plate-name"
        tabindex="-1"
      >
        {{ name }}的信箱
      </h1>
      <ScheduleNote stamp />
      <p class="plate-line">{{ line }}</p>
    </div>
    <div class="plate-slot">
      <slot />
    </div>
  </PaperSheet>
</template>
