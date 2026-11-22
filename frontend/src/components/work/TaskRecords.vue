<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { WorkInput, WorkResult, WorkTask } from '../../api/client'
import type { StampedEvent } from '../../composables/useWorkRecords'
import { isMoving } from '../../utils/work'
import TaskBrief from './TaskBrief.vue'
import TaskStamps from './TaskStamps.vue'
import TurnAnswers from './TurnAnswers.vue'

const props = defineProps<{
  task: WorkTask
  inputs: WorkInput[]
  results: WorkResult[]
  events: StampedEvent[]
  loading: boolean
  problem: string
}>()
const emit = defineEmits<{ retry: [] }>()
const moving = computed(() => isMoving(props.task))
const open = ref(moving.value)
const userToggled = ref(false)
const initial = computed(() => props.inputs.find((input) => input.kind === 'initial') ?? null)
const supplements = computed(() => props.inputs.filter((input) => input.kind === 'supplement'))
const summary = computed(() => {
  const parts: string[] = []
  if (props.results.length) parts.push(`${props.results.length} 轮`)
  if (props.events.length) parts.push(`${props.events.length} 条记录`)
  const suffix = parts.length ? `（${parts.join(' · ')}）` : ''
  return moving.value ? `原交办要求与目前经过${suffix}` : `原交办要求、记录与逐轮答复${suffix}`
})

watch(
  () => props.task.id,
  () => {
    userToggled.value = false
    open.value = moving.value
  },
)
watch(moving, (active) => {
  if (!userToggled.value) open.value = active
})

function onToggle(event: Event) {
  open.value = (event.target as HTMLDetailsElement).open
}
</script>

<template>
  <details
    class="detail-records"
    :open="open"
    @toggle="onToggle"
  >
    <summary @click="userToggled = true">{{ summary }}</summary>
    <div
      class="detail-records-body"
      :aria-busy="loading"
    >
      <p
        v-if="loading"
        class="detail-empty"
        role="status"
      >
        正在核对补话和处理记录；任务当前状态显示在上方。
      </p>
      <div
        v-if="problem"
        class="detail-banner is-alert"
        role="alert"
      >
        <p>记录还没读全：{{ problem }}。已读到的内容先留在这里。</p>
      </div>
      <button
        type="button"
        class="detail-button is-quiet"
        :aria-disabled="loading"
        :aria-busy="loading"
        @click="!loading && emit('retry')"
      >
        {{ loading ? '正在重读…' : '重读记录' }}
      </button>
      <TaskBrief
        :task="task"
        :initial="initial"
        :supplements="supplements"
      />
      <TaskStamps
        v-if="events.length || !loading"
        :task="task"
        :events="events"
        :inputs="inputs"
      />
      <TurnAnswers
        :results="results"
        :inputs="inputs"
      />
    </div>
  </details>
</template>
