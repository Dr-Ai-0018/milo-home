<script setup lang="ts">
import { computed } from 'vue'

export type HomeAccessStatus = 'checking' | 'anonymous' | 'no-access' | 'granted' | 'unavailable'

const props = defineProps<{
  access: HomeAccessStatus
  retryingWork?: boolean
  restored?: boolean
  compact?: boolean
}>()

const emit = defineEmits<{
  retryWork: []
}>()

const hasWorkAccess = computed(() => props.access === 'granted')
const isSignedNoWork = computed(() => props.access === 'no-access')
const isWorkFailed = computed(() => props.access === 'unavailable')
const showRetry = computed(() => isWorkFailed.value || props.retryingWork)

const guidance = computed(() => {
  if (hasWorkAccess.value) {
    return '回来啦。书信、纸条和工作桌都在这儿。'
  }
  if (isSignedNoWork.value) {
    return '回来啦。长信和随手纸条都在手边。'
  }
  if (isWorkFailed.value) {
    return '暂时没核对上交办入口，不影响先去看作息、写信或传纸条。'
  }
  if (props.access === 'checking') {
    return '书信与纸条现在就能点；正在顺便看交办入口。'
  }
  return '书信与纸条凭邀请或已有账号进入；不登录也能先看作息和家里近况。'
})

function onRetryWork() {
  if (!props.retryingWork) emit('retryWork')
}
</script>

<template>
  <div
    class="home-entrance"
    :class="{ 'is-compact': compact }"
  >
    <!-- 主按钮行保持稳定：任何权限或作息延迟到达，都不会把已摆出的按钮往上下推走。 -->
    <div class="entrance-actions">
      <RouterLink
        v-if="hasWorkAccess"
        class="button primary"
        to="/work"
      >
        去交办
      </RouterLink>
      <RouterLink
        class="button"
        :class="{ primary: !hasWorkAccess }"
        to="/letters"
      >
        {{ access === 'anonymous' ? '去书信页登录' : '进入书信' }}
      </RouterLink>
      <RouterLink
        :class="hasWorkAccess ? 'button text-button entrance-tertiary-link' : 'button'"
        to="/chat"
      >
        传张纸条
      </RouterLink>
    </div>

    <p
      class="entrance-guidance"
      :class="{ 'is-warn': isWorkFailed }"
    >
      {{ guidance }}
    </p>

    <!-- 权限核对失败时在按钮下方独立 slot 给重试，避免把按钮或下方模块推跳。 -->
    <div class="entrance-feedback-slot">
      <div
        v-if="showRetry"
        class="entrance-retry-line"
        role="alert"
      >
        <span>交办入口暂未接通。</span>
        <button
          type="button"
          class="text-button entrance-retry-btn"
          :aria-disabled="retryingWork"
          :aria-busy="retryingWork"
          @click="onRetryWork"
        >
          {{ retryingWork ? '正在重新核对…' : '重新核对交办权限' }}
        </button>
      </div>
      <p
        v-else-if="restored && hasWorkAccess"
        class="entrance-restored-note"
        role="status"
      >
        已重新核对成功：「去交办」入口与顶栏已同步开放。
      </p>
    </div>
  </div>
</template>
