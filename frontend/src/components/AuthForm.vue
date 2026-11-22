<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref } from 'vue'
import { api, explain } from '../api/client'
import PaperSheet from './ink/PaperSheet.vue'

const emit = defineEmits<{ authenticated: [] }>()
const mode = ref<'login' | 'invite'>('login')
const handle = ref('')
const displayName = ref('')
const token = ref('')
const password = ref('')
const busy = ref(false)
const passwordField = ref<HTMLInputElement | null>(null)
const error = ref('')
const controller = new AbortController()
// 每次报错轻摇一下纸片；交替类名让连续报错也能重播。
const shakes = ref(0)
const shakeClass = computed(() =>
  shakes.value === 0 ? '' : shakes.value % 2 ? 'is-shake-a' : 'is-shake-b',
)

function switchMode() {
  mode.value = mode.value === 'login' ? 'invite' : 'login'
  token.value = ''
  password.value = ''
  error.value = ''
}

async function submit() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  let failed = false
  try {
    if (mode.value === 'invite') {
      await api.activate(
        {
          token: token.value.trim(),
          display_name: displayName.value.trim(),
          password: password.value,
        },
        controller.signal,
      )
    } else {
      await api.login(
        {
          handle: handle.value.trim(),
          password: password.value,
        },
        controller.signal,
      )
    }
    token.value = ''
    password.value = ''
    if (!controller.signal.aborted) emit('authenticated')
  } catch (failure) {
    if (!controller.signal.aborted) {
      error.value = explain(failure)
      shakes.value += 1
      failed = true
    }
  } finally {
    busy.value = false
  }
  // 提交时整组输入暂时不可用，焦点会掉出表单；没成功就放回密码框，读屏念完错误后从这里重输。
  if (failed) {
    await nextTick()
    passwordField.value?.focus()
  }
}

onBeforeUnmount(() => {
  controller.abort()
  token.value = ''
  password.value = ''
})
</script>

<template>
  <PaperSheet
    as="section"
    class="auth-space"
    :class="shakeClass"
    aria-labelledby="auth-title"
  >
    <h2
      id="auth-title"
      class="auth-title"
    >
      {{ mode === 'login' ? '先认一下门' : '第一次来，欢迎' }}
    </h2>
    <p class="muted auth-intro">
      {{
        mode === 'login'
          ? '登录后，你之前写的信和纸条都在这里。'
          : '填写收到的邀请，设置以后登录用的密码。账号名以邀请中为准。'
      }}
    </p>
    <form @submit.prevent="submit">
      <fieldset :disabled="busy">
        <template v-if="mode === 'login'">
          <label for="handle">账号名</label>
          <input
            id="handle"
            v-model="handle"
            name="username"
            autocomplete="username"
            required
            pattern="[a-z][a-z0-9_\-]{2,31}"
            maxlength="32"
            aria-describedby="handle-help"
          />
          <p
            id="handle-help"
            class="small-note field-help"
          >
            邀请里的英文账号名，包含小写字母、数字、下划线或短横线。
          </p>
        </template>
        <template v-else>
          <label for="invitation">邀请口令</label>
          <input
            id="invitation"
            v-model="token"
            type="password"
            autocomplete="off"
            required
            minlength="43"
            maxlength="64"
          />
          <label for="display-name">怎么称呼你</label>
          <input
            id="display-name"
            v-model="displayName"
            autocomplete="nickname"
            required
            maxlength="40"
          />
        </template>
        <label for="password">{{ mode === 'login' ? '密码' : '设置密码' }}</label>
        <input
          id="password"
          ref="passwordField"
          v-model="password"
          type="password"
          :autocomplete="mode === 'login' ? 'current-password' : 'new-password'"
          required
          minlength="12"
          maxlength="128"
          aria-describedby="password-help"
        />
        <p
          id="password-help"
          class="small-note field-help"
        >
          12–128 个字符。
        </p>
        <button
          class="primary"
          type="submit"
        >
          {{ busy ? '正在确认…' : mode === 'login' ? '进入我的对话' : '收下邀请，进门' }}
        </button>
      </fieldset>
      <p
        class="feedback error"
        role="alert"
      >
        {{ error }}
      </p>
    </form>
    <button
      class="text-button"
      type="button"
      :disabled="busy"
      @click="switchMode"
    >
      {{ mode === 'login' ? '有邀请？第一次来这里' : '已有账号，去登录' }}
    </button>
    <p class="small-note auth-footnote">暂不开放自行注册。还没有邀请的话，下次见面找米洛要一份。</p>
  </PaperSheet>
</template>
