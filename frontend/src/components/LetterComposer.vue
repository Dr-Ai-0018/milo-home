<script setup lang="ts">
import { computed } from 'vue'
import { handCopy } from '../theme/handCopy'
import HandNote from './ink/HandNote.vue'
import InkStamp from './ink/InkStamp.vue'

// 只负责展示：保存逻辑、重试标识和成功提示都来自所在页面。
// 出错说明由页面放进 trouble 插槽，贴在按钮上方，按下按钮时就看得到。
const draft = defineModel<string>({ required: true })
const props = withDefaults(
  defineProps<{ sending: boolean; disabled: boolean; notice: string; label?: string }>(),
  { label: '写点什么给米洛' },
)
defineEmits<{ send: [] }>()

const cheer = computed(() => handCopy.cheer(draft.value.length))
const cannotSend = computed(() => props.sending || props.disabled || !draft.value.trim())
</script>

<template>
  <form
    class="letter-composer"
    :class="{ 'is-lifting': sending }"
    @submit.prevent="$emit('send')"
  >
    <label for="message">
      {{ label }}
      <HandNote
        size="sm"
        :tilt="-2"
        >{{ handCopy.composerAside }}</HandNote
      >
    </label>
    <div class="composer-paper">
      <textarea
        id="message"
        v-model="draft"
        :readonly="sending || disabled"
        required
        maxlength="4000"
        aria-describedby="message-help"
        placeholder="从你想说的那句开始。"
      />
    </div>
    <slot name="trouble" />
    <div class="composer-actions">
      <p class="composer-count">
        <span id="message-help">{{ draft.length }} / 4000 · 纯文本 · 只有你和米洛看得到</span>
        <HandNote
          v-if="cheer"
          size="sm"
          :tilt="-2"
          >{{ cheer }}</HandNote
        >
      </p>
      <button
        class="primary composer-send"
        type="submit"
        :aria-disabled="cannotSend"
        :aria-busy="sending"
      >
        {{ sending ? '正在保存…' : '贴进本子' }}
      </button>
    </div>
    <div
      class="composer-done"
      :class="{ 'is-visible': !!notice }"
      role="status"
    >
      <InkStamp
        v-if="notice"
        label="收好了"
        size="sm"
        :tilt="-5"
        pop
        aria-hidden="true"
      />
      <span v-if="notice">{{ notice }}</span>
    </div>
  </form>
</template>
