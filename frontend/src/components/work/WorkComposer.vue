<script setup lang="ts">
import { computed, useId } from 'vue'

// 只负责展示：交出去、补几句、另开一件都用它；保存逻辑和重试标识在页面里。
// held 时草稿留着、可以改，但不能发，并写明为什么。
const draft = defineModel<string>({ required: true })
const props = withDefaults(
  defineProps<{
    label: string
    action: string
    sending: boolean
    held?: string
    notice?: string
    placeholder?: string
  }>(),
  { held: '', notice: '', placeholder: '写清楚要做什么、做到什么程度算完。' },
)
defineEmits<{ send: [] }>()

const id = useId()
const cannotSend = computed(() => props.sending || !!props.held || !draft.value.trim())
</script>

<template>
  <form
    class="work-composer"
    @submit.prevent="$emit('send')"
  >
    <label :for="`body${id}`">{{ label }}</label>
    <textarea
      :id="`body${id}`"
      v-model="draft"
      :readonly="sending"
      required
      maxlength="4000"
      rows="4"
      :aria-describedby="`help${id}`"
      :placeholder="placeholder"
    />
    <p
      v-if="held"
      class="composer-held"
    >
      {{ held }}
    </p>
    <slot name="deliverable" />
    <slot name="trouble" />
    <div class="composer-row">
      <span
        :id="`help${id}`"
        class="small-note"
        >{{ draft.length }} / 4000 · 纯文本</span
      >
      <button
        class="primary"
        type="submit"
        :aria-disabled="cannotSend"
        :aria-busy="sending"
      >
        {{ sending ? '正在交…' : action }}
      </button>
    </div>
    <p
      class="small-note"
      role="status"
    >
      {{ notice }}
    </p>
  </form>
</template>
