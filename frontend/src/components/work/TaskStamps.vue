<script setup lang="ts">
import { computed } from 'vue'
import type { WorkInput, WorkTask } from '../../api/client'
import type { StampedEvent } from '../../composables/useWorkTask'
import { inkAt, seedOf } from '../../utils/stampInk'
import { clockTime, isoTime, sameDay, stampDate, stampTime } from '../../utils/time'
import { eventLine, stampFor } from '../../utils/work'
import PencilRing from '../ink/PencilRing.vue'
import RubberStamp from '../ink/RubberStamp.vue'

// 每条记录按顺序盖一枚章，越往后印泥越干；结论章重新蘸过墨，压在右上角。
// 章只是装饰（aria-hidden），同样的内容在下面的逐条记录里用文字写明。
const props = defineProps<{ task: WorkTask; events: StampedEvent[]; inputs: WorkInput[] }>()

interface Mark {
  key: string
  seed: number
  fresh: boolean
  delay: number
  date: string
}

const marks = computed(() => {
  const flow: (Mark & { label: string; tone: 'ink' | 'stamp'; ink: number })[] = []
  const verdicts: (Mark & { label: string | null })[] = []
  // v15 之前的旧任务没有逐条记录，不补造过程，只按现在的状态盖一枚。
  const source = props.events.length
    ? props.events.map((event) => ({
        key: `${event.task_id}:${event.sequence}`,
        spec: stampFor(event),
        at: event.recorded_at,
        fresh: event.fresh,
      }))
    : [
        {
          key: `${props.task.id}:0`,
          spec: stampFor(props.task.status),
          at: props.task.updated_at,
          fresh: false,
        },
      ]
  let landing = 0
  for (const { key, spec, at, fresh } of source) {
    if (!spec) continue
    const seed = seedOf(key)
    const delay = fresh ? 420 * landing++ : 0
    if (spec.kind === 'rect') {
      flow.push({ key, seed, fresh, delay, date: stampDate(at), ...spec, ink: inkAt(flow.length) })
    } else {
      const label = spec.kind === 'round' ? spec.label : null
      verdicts.push({ key, seed, fresh, delay, date: stampDate(at, true), label })
    }
  }
  return { flow, verdicts }
})

function when(seconds: number) {
  return sameDay(seconds, props.task.created_at) ? clockTime(seconds) : stampTime(seconds)
}
</script>

<template>
  <section
    class="task-stamps"
    aria-labelledby="stamps-title"
  >
    <h2
      id="stamps-title"
      class="sheet-label"
    >
      记录
    </h2>
    <div class="stamp-field">
      <RubberStamp
        v-for="mark in marks.flow"
        :key="mark.key"
        :label="mark.label"
        :date="mark.date"
        :seed="mark.seed"
        :tone="mark.tone"
        :ink="mark.ink"
        :fresh="mark.fresh"
        :delay="mark.delay"
      />
      <div
        v-if="marks.verdicts.length"
        class="stamp-verdicts"
      >
        <template
          v-for="mark in marks.verdicts"
          :key="mark.key"
        >
          <RubberStamp
            v-if="mark.label"
            shape="round"
            tone="stamp"
            :label="mark.label"
            :date="mark.date"
            :seed="mark.seed"
            :fresh="mark.fresh"
            :delay="mark.delay"
          />
          <PencilRing
            v-else
            :seed="mark.seed"
          />
        </template>
      </div>
    </div>
    <p
      v-if="!events.length"
      class="small-note"
    >
      这件是旧记录，没有逐条过程。
    </p>
    <details
      v-else
      class="work-log"
    >
      <summary>逐条记录（{{ events.length }} 条）</summary>
      <ol>
        <li
          v-for="event in events"
          :key="event.sequence"
        >
          <time :datetime="isoTime(event.recorded_at)">记于 {{ when(event.recorded_at) }}</time>
          <span>{{ eventLine(event, inputs) }}</span>
        </li>
      </ol>
    </details>
  </section>
</template>
