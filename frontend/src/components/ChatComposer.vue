<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

// 纸条输入：Enter 递出，Shift+Enter 换行；输入法组字时的 Enter 不算递出。
const props = defineProps<{
  disabled: boolean
  ownerKey?: string | null
}>()
const emit = defineEmits<{ send: [body: string] }>()
const draft = ref('')
const field = ref<HTMLTextAreaElement | null>(null)
const MAX_LENGTH = 4000

let composing = false
let compositionEndedAt = 0

const canSend = computed(() => !props.disabled && !!draft.value.trim())
const remaining = computed(() => MAX_LENGTH - draft.value.length)

function maxHeight() {
  return Math.min(200, Math.max(112, Math.floor(window.innerHeight * 0.3)))
}

function fit() {
  const el = field.value
  if (!el) return
  el.style.height = 'auto'
  const limit = maxHeight()
  el.style.height = `${Math.min(el.scrollHeight, limit)}px`
  el.style.overflowY = el.scrollHeight > limit ? 'auto' : 'hidden'
}

watch(draft, () => void nextTick(fit))

// 退出或切换账号后清空草稿，不把旧账号正文带进新会话；首读从空切到当前号时不动。
watch(
  () => props.ownerKey,
  (current, previous) => {
    if (previous && current !== previous) {
      draft.value = ''
      void nextTick(fit)
    }
  },
)

onMounted(() => {
  fit()
  window.addEventListener('resize', fit, { passive: true })
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', fit)
})

function onCompositionStart() {
  composing = true
}

function onCompositionEnd() {
  composing = false
  compositionEndedAt = performance.now()
}

function submit() {
  const body = draft.value.trim()
  if (!body || props.disabled) return
  emit('send', body)
  draft.value = ''
  field.value?.focus({ preventScroll: true })
}

function keydown(event: KeyboardEvent) {
  if (event.key !== 'Enter' || event.shiftKey || event.altKey) return
  if (
    event.isComposing ||
    composing ||
    event.keyCode === 229 ||
    performance.now() - compositionEndedAt < 60
  )
    return
  event.preventDefault()
  if (event.repeat) return
  submit()
}
</script>

<template>
  <form
    class="chat-composer"
    @submit.prevent="submit"
  >
    <label
      class="visually-hidden"
      for="chat-input"
      >写一张纸条</label
    >
    <div
      class="chat-composer-box"
      :class="{ 'is-disabled': disabled }"
    >
      <textarea
        id="chat-input"
        ref="field"
        v-model="draft"
        rows="1"
        :maxlength="MAX_LENGTH"
        enterkeyhint="send"
        :placeholder="disabled ? '正在打开聊天，稍等一下…' : '写一句，按 Enter 递出去'"
        aria-describedby="chat-help"
        :disabled="disabled"
        @compositionstart="onCompositionStart"
        @compositionend="onCompositionEnd"
        @keydown="keydown"
      />
      <button
        type="submit"
        class="primary chat-send"
        :aria-disabled="!canSend"
      >
        递出去
      </button>
    </div>
    <p
      id="chat-help"
      class="small-note chat-help"
    >
      <span
        ><span class="chat-help-key">Shift + Enter 换行 · </span>纯文本 · 只有你和米洛看得到</span
      >
      <span
        v-if="remaining <= 500"
        class="chat-help-count"
        aria-live="polite"
        >还可写 {{ remaining }} 字</span
      >
    </p>
  </form>
</template>
