<script setup lang="ts">
import { computed } from 'vue'
import type { Run } from '../api/client'
import { isoTime, stampTime } from '../utils/time'
import InkStamp from './ink/InkStamp.vue'

const props = defineProps<{
  runs: Run[]
  known: boolean
  loading: boolean
  problem: string
}>()
const emit = defineEmits<{ retry: [] }>()
const latest = computed(() => props.runs[0])
const descriptions: Record<Run['status'], string> = {
  queued: '这次读信已经排好，等待开始。',
  running: '米洛正在读信。完成并保存后，回信才会出现在对应的信里。',
  stopping: '正在停止读信，尚未确认结束。这次不会继续发布回信。',
  succeeded: '这次的回信已经保存。之后写的，会等下一次读信。',
  failed: '这次读信没有完成，没有发布回信。你写的都还在。',
  cancelled: '这次读信已经取消，进程已结束，没有发布回信。',
  timed_out: '这次读信达到期限，进程已结束，没有发布回信。',
}
const labels: Record<Run['status'], string> = {
  queued: '等待开始',
  running: '正在读信',
  stopping: '正在停止',
  succeeded: '回信已保存',
  failed: '未完成',
  cancelled: '已取消',
  timed_out: '已超时',
}
const failures: Record<string, string> = {
  configuration_error: '读信入口暂时不可用。',
  invalid_result: '回信没有通过完整性检查。',
  host_cpu_limit: '服务器忙碌，这次提前停止。',
  output_limit: '输出超出允许范围，这次提前停止。',
  interrupted: '这次运行中断，需要米洛确认后再读。',
}
</script>

<template>
  <aside
    class="run-status"
    :class="{ 'is-unavailable': !known }"
    aria-label="最近一次读信"
    :aria-busy="loading"
  >
    <InkStamp
      v-if="known"
      :label="latest ? labels[latest.status] : '还没读过'"
      size="sm"
      :tilt="-3"
    />
    <div>
      <p
        v-if="loading && !known"
        role="status"
      >
        正在找回读信记录；信件可以照常查看和续写。
      </p>
      <p v-else-if="!known && !problem">读信记录还没核对。</p>
      <p
        v-if="problem"
        class="feedback error"
        role="alert"
      >
        读信记录暂时没读到：{{ problem }}。信件可以照常查看和续写。
        <span v-if="known">这里保留的是上次读到的记录。</span>
      </p>
      <p v-if="known">
        {{
          latest ? descriptions[latest.status] : '信先放在这里。米洛读信后，会留下真实的处理记录。'
        }}
      </p>
      <p
        v-if="known && latest?.failure_code && failures[latest.failure_code]"
        class="run-reason"
      >
        {{ failures[latest.failure_code] }}
      </p>
      <p class="small-note">
        <template v-if="known && latest"
          >最近一次：<time :datetime="isoTime(latest.created_at)">{{
            stampTime(latest.created_at)
          }}</time>
          ·
        </template>
        <button
          type="button"
          class="text-button"
          :aria-disabled="loading"
          :aria-busy="loading"
          @click="!loading && emit('retry')"
        >
          {{ loading ? '正在核对读信记录…' : '重读读信记录' }}
        </button>
      </p>
    </div>
  </aside>
</template>
