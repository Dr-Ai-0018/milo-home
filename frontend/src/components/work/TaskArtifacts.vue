<script setup lang="ts">
import { computed } from 'vue'
import type { WorkArtifact, WorkTask } from '../../api/client'
import { stampTime } from '../../utils/time'

const props = defineProps<{ artifacts: WorkArtifact[]; task: WorkTask }>()
const deliveryNote = computed(() => {
  const item = props.task.deliverable
  if (!item || props.artifacts.some((artifact) => artifact.path === item.path)) return ''
  if (item.state === 'missing') return '处理结束时没找到约定的文件。'
  if (item.state === 'withheld') return '约定的文件没能保存为可查看的文本。'
  if (props.task.status === 'completed') return '还没读到成果记录，可以点「重新读取」。'
  if (['queued', 'running', 'stopping'].includes(props.task.status)) return '还没有交回这份文件。'
  return '这次没留下可核对的文件。'
})
</script>

<template>
  <section
    class="sheet-body"
    aria-labelledby="artifacts-title"
  >
    <h2
      id="artifacts-title"
      class="sheet-label"
    >
      交办成果
    </h2>
    <p
      v-if="task.deliverable"
      class="small-note artifact-path"
    >
      约定交回：{{ task.deliverable.filename
      }}<template v-if="deliveryNote"> · {{ deliveryNote }}</template>
    </p>
    <p
      v-if="!artifacts.length && !task.deliverable"
      class="small-note"
    >
      还没有可核对的成果记录。
    </p>
    <template v-if="artifacts.length">
      <p class="small-note">这里保留的是核对时的文件内容。工作桌上的文件后来可能有改动。</p>
      <ul class="task-artifacts">
        <li
          v-for="artifact in artifacts"
          :key="artifact.path"
        >
          <details>
            <summary>
              <span class="artifact-path">{{ artifact.path }}</span>
              <span class="small-note">核对于 {{ stampTime(artifact.checked_at) }}</span>
            </summary>
            <pre
              v-if="artifact.text.length"
              class="artifact-content"
              >{{ artifact.text }}</pre>
            <p
              v-else
              class="small-note"
            >
              这是一个空文件。
            </p>
            <p class="small-note artifact-digest">SHA-256：{{ artifact.sha256 }}</p>
          </details>
        </li>
      </ul>
    </template>
  </section>
</template>
