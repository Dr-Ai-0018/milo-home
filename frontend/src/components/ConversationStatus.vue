<script setup lang="ts">
import { computed } from 'vue'
import type { ConversationStatusView } from '../utils/activity'
import { stampTime } from '../utils/time'
import '../styles/conversation-status.css'

// 紧凑的暖纸状态块：把排队事实与持续服务证据分开说清楚，不做成机房监控屏。
const props = withDefaults(
  defineProps<{
    status: ConversationStatusView
    latestReply?: number | null
    connectionLine?: string
    showInlineRetry?: boolean
    retrying?: boolean
    canRecheck?: boolean
    rechecking?: boolean
    variant?: 'card' | 'inline'
  }>(),
  {
    latestReply: null,
    connectionLine: '',
    showInlineRetry: false,
    retrying: false,
    canRecheck: false,
    rechecking: false,
    variant: 'card',
  },
)

defineEmits<{ retry: []; recheck: [] }>()

const hasStatusBody = computed(
  () =>
    !!(
      props.status.headline ||
      props.status.detail ||
      props.status.receptionNote ||
      props.status.revisitNote ||
      props.status.serviceNote
    ),
)
const hasAnything = computed(
  () => hasStatusBody.value || !!props.latestReply || !!props.connectionLine,
)
</script>

<template>
  <div
    v-if="hasAnything"
    class="conv-status"
    :class="[
      variant === 'inline' ? 'letter-activity is-inline' : 'chat-status is-card',
      `is-tone-${status.tone}`,
      { 'is-live': status.tone === 'live' },
    ]"
    role="status"
  >
    <div
      v-if="hasStatusBody"
      class="conv-status-main"
    >
      <p
        v-if="status.headline"
        class="conv-status-headline"
        :class="[
          variant === 'card' ? 'chat-activity' : '',
          { 'is-live': status.tone === 'live', 'is-warn': status.tone === 'warn' },
        ]"
      >
        <span>{{ status.headline }}</span>
      </p>
      <p
        v-if="status.detail"
        class="conv-status-detail"
      >
        {{ status.detail }}
      </p>
      <p
        v-if="status.revisitNote"
        class="conv-status-revisit"
      >
        {{ status.revisitNote }}
      </p>
      <p
        v-if="status.receptionNote"
        class="conv-status-reception"
      >
        {{ status.receptionNote }}
      </p>
      <p
        v-if="status.serviceNote"
        class="conv-status-service"
      >
        {{ status.serviceNote }}
      </p>
    </div>

    <p
      v-if="latestReply"
      class="chat-latest-reply"
    >
      米洛上次在这里回你：{{ stampTime(latestReply) }}
    </p>

    <p
      v-if="connectionLine"
      :class="variant === 'inline' ? 'letter-offline conv-status-offline' : 'chat-offline'"
    >
      <span>{{ connectionLine }}</span>
      <button
        v-if="showInlineRetry"
        type="button"
        class="text-button chat-inline-retry"
        :aria-disabled="retrying"
        :aria-busy="retrying"
        @click="$emit('retry')"
      >
        马上重读
      </button>
    </p>

    <p
      v-if="canRecheck && !connectionLine"
      class="conv-status-actions"
    >
      <button
        type="button"
        class="text-button conv-status-recheck"
        :aria-disabled="rechecking"
        :aria-busy="rechecking"
        @click="$emit('recheck')"
      >
        {{ rechecking ? '正在核对状态…' : '再核对状态' }}
      </button>
    </p>
  </div>
</template>
