<script setup lang="ts">
import { useId } from 'vue'

defineProps<{ sending: boolean }>()
const filename = defineModel<string>({ required: true })
const id = useId()
</script>

<template>
  <div class="deliverable-field">
    <label :for="id">指定单个 .txt 交回文件（可选）</label>
    <input
      :id="id"
      v-model="filename"
      type="text"
      maxlength="100"
      placeholder="例如：小纸条.txt"
      :readonly="sending"
      :aria-describedby="`${id}-help`"
    />
    <p
      :id="`${id}-help`"
      class="small-note"
    >
      需要其他格式或多份附件时，直接写在上面的交办内容里即可；这里只在快捷指定单个 .txt
      文件名时填写，留空不影响交办或交付附件。
    </p>
  </div>
</template>
