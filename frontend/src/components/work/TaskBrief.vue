<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { WorkInput, WorkTask } from '../../api/client'
import { isoTime, stampTime } from '../../utils/time'
import { inputDeliveryLine } from '../../utils/work'

const props = defineProps<{
  task: WorkTask
  initial: WorkInput | null
  supplements: WorkInput[]
}>()

const unfolded = ref(false)
watch(
  () => props.task.id,
  () => {
    unfolded.value = false
  },
)

const isLong = computed(() => {
  const body = props.task.initial_input.body
  return body.length > 240 || body.split('\n').length > 6
})
</script>

<template>
  <section
    class="detail-section brief-section"
    aria-labelledby="brief-heading"
  >
    <h2 id="brief-heading">当时交办的内容</h2>

    <div class="brief-card">
      <div
        class="brief-body"
        :class="{ 'is-clamped': isLong && !unfolded }"
      >
        {{ task.initial_input.body }}
      </div>
      <button
        v-if="isLong"
        type="button"
        class="detail-button is-quiet"
        :aria-expanded="unfolded"
        @click="unfolded = !unfolded"
      >
        {{ unfolded ? '收起原文' : '展开全文' }}
      </button>

      <div class="brief-footnote">
        <p
          v-if="task.deliverable"
          class="brief-note"
        >
          要求写出文本交付：{{ task.deliverable.filename }}
        </p>
        <p
          v-if="initial"
          class="brief-note"
        >
          {{ inputDeliveryLine(initial) }}
        </p>
      </div>
    </div>

    <div
      v-if="supplements.length"
      class="supplements-block"
    >
      <h3>中途补上的话（{{ supplements.length }}）</h3>
      <ol class="supplement-list">
        <li
          v-for="(item, index) in supplements"
          :key="item.id"
          class="supplement-item"
        >
          <div class="supplement-head">
            <span>第 {{ index + 1 }} 段</span>
            <time :datetime="isoTime(item.created_at)">{{ stampTime(item.created_at) }}</time>
          </div>
          <p class="supplement-body">{{ item.body }}</p>
          <p class="supplement-delivery">{{ inputDeliveryLine(item) }}</p>
        </li>
      </ol>
    </div>
  </section>
</template>
