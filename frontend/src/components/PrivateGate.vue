<script setup lang="ts">
import { computed, nextTick, onMounted } from 'vue'
import { usePrivateSession } from '../composables/usePrivateSession'
import AuthForm from './AuthForm.vue'
import '../styles/private.css'

// 书信与聊天的共同入口：确认登录，未登录就地显示登录纸片，登录后渲染各自的内容。
const { user, checked, loading, problem, load, reload, revalidate, logout } = usePrivateSession()
// 页面自己的内容（某封信、聊天）也要跟着重读，交给页面处理。
const emit = defineEmits<{ reload: [] }>()
// below：常用页把「重新读取 / 退出登录」放到内容下面当脚注；读取出错时仍放顶上，紧挨着提示。
const props = defineProps<{ below?: boolean }>()
const atFoot = computed(() => props.below && !problem.value)

// 读取中按钮只标 aria-disabled（焦点留在原处），再按一次在这里挡掉。
async function reloadAll() {
  if (loading.value) return
  await reload()
  emit('reload')
}

function leave() {
  if (!loading.value) void logout()
}

async function authenticated() {
  await load()
  await nextTick()
  // 只移焦点不滚动：聊天这时可能已经落到最新的纸条。
  document.querySelector<HTMLElement>('#private-title')?.focus({ preventScroll: true })
}

onMounted(() => {
  if (!user.value) void load()
  else if (!loading.value) void revalidate()
})
</script>

<template>
  <div class="private-gate">
    <p
      v-if="!user && (!checked || loading)"
      class="session-loading"
      role="status"
    >
      正在确认登录状态…
    </p>
    <template v-else-if="!user">
      <div
        v-if="problem"
        class="connection-problem"
      >
        <p
          class="feedback error"
          role="alert"
        >
          {{ problem }}
        </p>
        <button
          type="button"
          class="text-button"
          @click="load"
        >
          重新读取记录
        </button>
      </div>
      <AuthForm @authenticated="authenticated" />
    </template>
    <template v-else>
      <div
        v-if="!atFoot"
        class="account-bar"
      >
        <button
          type="button"
          class="text-button"
          :aria-disabled="loading"
          @click="reloadAll"
        >
          重新读取
        </button>
        <button
          type="button"
          class="text-button"
          :aria-disabled="loading"
          @click="leave"
        >
          退出登录
        </button>
      </div>
      <!-- 读不到时紧贴在「重新读取」下面说明，不沉到页面底部。 -->
      <p
        v-if="problem"
        class="connection-problem feedback error"
        role="alert"
      >
        {{ problem }}
      </p>
      <slot />
      <div
        v-if="atFoot"
        class="account-bar is-foot"
      >
        <button
          type="button"
          class="text-button"
          :aria-disabled="loading"
          @click="reloadAll"
        >
          重新读取
        </button>
        <button
          type="button"
          class="text-button"
          :aria-disabled="loading"
          @click="leave"
        >
          退出登录
        </button>
      </div>
    </template>
  </div>
</template>
