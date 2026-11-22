<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { ApiFailure, explain } from '../api/client'
import { usePrivateSession } from '../composables/usePrivateSession'

// 开一封新信：标题可空。同一标题的重试沿用原幂等键，不会开出第二封。
// 平时收成一个按钮放在门牌下沿，要开时再展开；取消后焦点回到开信按钮。
const emit = defineEmits<{ created: [id: string] }>()
const session = usePrivateSession()
const opened = ref(false)
const openBtn = ref<HTMLButtonElement | null>(null)
const field = ref<HTMLInputElement | null>(null)
const title = ref('')
const busy = ref(false)
const error = ref('')
let attempt: { title: string; key: string } | null = null

function resetForm() {
  opened.value = false
  title.value = ''
  busy.value = false
  error.value = ''
  attempt = null
}

watch(
  () => session.user.value?.id ?? null,
  (now, before) => {
    if (now !== before) resetForm()
  },
)

async function expand() {
  opened.value = true
  await nextTick()
  field.value?.focus({ preventScroll: true })
}

async function collapse() {
  if (busy.value) return
  opened.value = false
  error.value = ''
  await nextTick()
  openBtn.value?.focus({ preventScroll: true })
}

function handleEscape(event: KeyboardEvent) {
  if (event.isComposing || busy.value) return
  void collapse()
}

async function submit() {
  if (busy.value) return
  const clean = title.value.trim()
  if (!attempt || attempt.title !== clean) attempt = { title: clean, key: crypto.randomUUID() }
  const ownerAtStart = session.user.value?.id ?? null
  busy.value = true
  error.value = ''
  try {
    const letter = await session.createLetter(clean || null, attempt.key)
    if (!letter || (session.user.value?.id ?? null) !== ownerAtStart) return
    attempt = null
    title.value = ''
    emit('created', letter.id)
  } catch (failure) {
    if (session.signal().aborted || (session.user.value?.id ?? null) !== ownerAtStart) return
    if (failure instanceof ApiFailure && failure.status === 401) session.fail(failure)
    else error.value = explain(failure)
  } finally {
    if ((session.user.value?.id ?? null) === ownerAtStart) busy.value = false
  }
}
</script>

<template>
  <div
    v-if="!opened"
    class="new-letter is-closed"
  >
    <button
      ref="openBtn"
      type="button"
      class="text-button new-letter-open"
      @click="expand"
    >
      ＋ 开一封新信
    </button>
    <span class="small-note">想换个话题时开一封新的。</span>
  </div>
  <form
    v-else
    class="new-letter"
    @submit.prevent="submit"
    @keydown.escape="handleEscape"
  >
    <label for="letter-title">开一封新信</label>
    <p
      id="letter-title-help"
      class="small-note"
    >
      想换个话题时开一封新的。标题可以不写，最多 80 字，只有你和米洛看得到。
    </p>
    <div class="new-letter-row">
      <input
        id="letter-title"
        ref="field"
        v-model="title"
        type="text"
        maxlength="80"
        autocomplete="off"
        placeholder="比如：最近在读的书"
        aria-describedby="letter-title-help"
        :readonly="busy"
      />
      <button
        type="submit"
        class="primary"
        :aria-disabled="busy"
        :aria-busy="busy"
      >
        {{ busy ? '正在开…' : '开信' }}
      </button>
      <button
        type="button"
        class="text-button"
        :aria-disabled="busy"
        @click="collapse"
      >
        先不开了
      </button>
    </div>
    <p
      v-if="error"
      class="feedback error"
      role="alert"
    >
      {{ error }} 标题不变时再点一次，不会开出两封。
    </p>
  </form>
</template>
