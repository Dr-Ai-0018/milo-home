<script setup lang="ts">
import type { WorkInput, WorkResult } from '../../api/client'
import { isoTime, stampTime } from '../../utils/time'
import { actionsLine, answerLine, inputName, outcomeCopy } from '../../utils/work'

// 每一轮的结果放在任务结论下面、小一号：「这一轮结束了」不等于整件做完。
// 答复正文是不可信纯文本，只做文本绑定，不渲染 HTML、不自动打开链接或加载图片。
defineProps<{ results: WorkResult[]; inputs: WorkInput[] }>()
</script>

<template>
  <section
    v-if="results.length"
    class="turn-answers"
    aria-labelledby="answers-title"
  >
    <h2
      id="answers-title"
      class="sheet-label"
    >
      每一轮
    </h2>
    <article
      v-for="result in results"
      :key="result.id"
      class="turn-answer"
    >
      <p class="turn-head">
        {{ inputName(inputs, result.input_id) }}：{{ outcomeCopy[result.outcome] }}
        <time :datetime="isoTime(result.observed_at)"
          >· 记于 {{ stampTime(result.observed_at) }}</time
        >
      </p>
      <template v-if="result.output.answer.state === 'available'">
        <h3 class="answer-label">米洛的答复</h3>
        <p class="answer-body">{{ result.output.answer.body }}</p>
      </template>
      <p
        v-else
        class="answer-missing"
      >
        {{ answerLine(result) }}
      </p>
      <p
        v-if="actionsLine(result)"
        class="turn-actions"
      >
        {{ actionsLine(result) }}
        <span class="small-note">（命令也可能改文件；次数不代表结果）</span>
      </p>
    </article>
  </section>
</template>
