<script setup lang="ts">
import { computed, type ComponentPublicInstance } from 'vue'
import type { WorkStatusGroup } from '../../api/client'
import { statusGroupMeta, WORK_STATUS_GROUPS } from '../../composables/useWorkDesk'

const props = defineProps<{
  activeGroup: WorkStatusGroup
  loading: boolean
  groupLoaded: boolean
  taskCount: number
  hasMore: boolean
  problem: string
}>()

const emit = defineEmits<{
  select: [group: WorkStatusGroup]
}>()

const buttonRefs = new Map<WorkStatusGroup, HTMLButtonElement>()

function setButtonRef(groupId: WorkStatusGroup, el: Element | ComponentPublicInstance | null) {
  if (el instanceof HTMLButtonElement) buttonRefs.set(groupId, el)
  else buttonRefs.delete(groupId)
}

const activeMeta = computed(() => statusGroupMeta(props.activeGroup))

const statusLine = computed(() => {
  const meta = activeMeta.value
  if (props.loading && !props.groupLoaded) {
    return `正在翻「${meta.label}」的交办…`
  }
  if (props.problem && !props.groupLoaded) {
    return `「${meta.label}」暂时没有读出来。`
  }
  if (props.loading && props.groupLoaded) {
    return `正在核对「${meta.label}」最新进度 · ${meta.hint}`
  }
  if (props.taskCount === 0) {
    return `${meta.label}：${meta.empty}`
  }
  const suffix = props.hasMore ? '，下方还可以继续翻更早的' : '，这一组已全部读完'
  return `当前看「${meta.label}」· 已读出 ${props.taskCount} 张${suffix} · ${meta.hint}`
})

function focusGroup(groupId: WorkStatusGroup) {
  const button = buttonRefs.get(groupId)
  if (!button) return false
  button.focus({ preventScroll: true })
  return true
}

defineExpose({ focusGroup })
</script>

<template>
  <div class="work-filter-block">
    <div
      class="work-filters"
      role="group"
      aria-label="按状态筛选交办"
    >
      <button
        v-for="group in WORK_STATUS_GROUPS"
        :key="group.id"
        :ref="(el) => setButtonRef(group.id, el)"
        type="button"
        class="filter-chip"
        :class="{ 'is-current': group.id === activeGroup }"
        :data-status-group="group.id"
        :aria-pressed="group.id === activeGroup"
        @click="emit('select', group.id)"
      >
        {{ group.label }}
      </button>
    </div>

    <p
      class="work-filter-status"
      role="status"
      aria-live="polite"
    >
      {{ statusLine }}
    </p>
  </div>
</template>
