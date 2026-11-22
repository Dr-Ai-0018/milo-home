<script setup lang="ts">
import { computed } from 'vue'
import { useKnock } from '../composables/useKnock'
import { clockTime } from '../utils/time'
import '../styles/knock.css'

const props = defineProps<{
  conversationId: string | null
}>()

const { state, receipt, reason, cooling, knock } = useKnock(() => props.conversationId)

// 敲门那一刻的作息快照，只说明当时的情况；现在的状态另看活动。
const phaseNote: Record<string, string> = {
  resting: '敲的时候米洛在休息。',
  winding_down: '敲的时候米洛在收尾。',
  paused: '敲的时候读信是暂停的。',
}

// 合并窗口按服务器给的两个时刻相减，不拿本机钟去比服务器钟。
function waitLabel(result: { received_at: number; next_allowed_at: number }) {
  const minutes = Math.round((result.next_allowed_at - result.received_at) / 60)
  return minutes < 1 ? '一会儿' : `约 ${minutes} 分钟`
}

// 平时只放按钮；敲过才出回执，把记下没有、会怎样、什么时候能再敲说清楚。
// 敲门从不单独叫醒米洛：有在等的信时随正常读信一起带上，没有就先记着。
const line = computed(() => {
  switch (state.value) {
    case 'sending':
      return '正在敲，等服务器记下…'
    case 'unknown':
      return '不确定敲到没有，可能是网络断了。再敲一次也只算这一声。'
    case 'failed':
      return `没敲出去：${reason.value}`
    case 'saved': {
      const result = receipt.value
      if (!result) return ''
      const phase = phaseNote[result.response_phase] ?? ''
      const again = cooling.value ? `过${waitLabel(result)}可以再敲。` : ''
      const heard =
        result.disposition === 'coalesced'
          ? '刚才已经敲过了（也可能是在别处敲的），这一声并进那一声，不另算。'
          : `${clockTime(result.received_at)} 敲门记下了。`
      return `${heard}${phase}敲门只留下一声来过提示，不会立刻叫醒米洛或改变排班，他下次读这里时会一起看到。${again}`
    }
    default:
      return ''
  }
})

// 敲过或正在敲时只标 aria-disabled，不用 disabled，免得键盘焦点掉回页面开头。
const label = computed(() => {
  if (state.value === 'sending') return '敲门中'
  if (state.value === 'unknown' || state.value === 'failed') return '再敲一次'
  return cooling.value ? '敲过了' : '敲敲门'
})
</script>

<template>
  <div
    class="knock-door"
    :class="`is-${state}`"
  >
    <button
      type="button"
      class="knock-button"
      :disabled="!conversationId"
      :aria-disabled="state === 'sending' || cooling"
      :aria-busy="state === 'sending'"
      title="留下一声来过提示，不会立刻叫醒米洛"
      @click="knock"
    >
      <svg
        class="knock-glyph"
        viewBox="0 0 16 20"
        aria-hidden="true"
      >
        <path d="M2.5 19V3.5A2 2 0 0 1 4.5 1.5h7a2 2 0 0 1 2 2V19" />
        <path d="M1 19h14" />
        <circle
          cx="10.6"
          cy="11"
          r="1"
        />
      </svg>
      {{ label }}
    </button>
    <p
      class="knock-receipt"
      role="status"
    >
      {{ line }}
    </p>
  </div>
</template>
