<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { explain } from '../api/client'
import type { WorkStatusGroup } from '../api/client'
import PrivateGate from '../components/PrivateGate.vue'
import ClipBoard from '../components/work/ClipBoard.vue'
import DeliverableField from '../components/work/DeliverableField.vue'
import MaintenanceNotice from '../components/work/MaintenanceNotice.vue'
import WorkComposer from '../components/work/WorkComposer.vue'
import WorkStatusFilter from '../components/work/WorkStatusFilter.vue'
import { useMaintenance } from '../composables/useMaintenance'
import { usePrivateScope } from '../composables/usePrivateScope'
import { usePrivateSession } from '../composables/usePrivateSession'
import { statusGroupMeta, useWorkDesk } from '../composables/useWorkDesk'
import { describeReception } from '../utils/noncompulsory'
import { dayLabel } from '../utils/time'
import { effectiveTaskCopy, sheetNumber, taskTitle } from '../utils/work'
import '../styles/work.css'
import '../styles/work-list.css'

const router = useRouter()
const session = usePrivateSession()
const {
  desk,
  reception,
  activeGroup,
  tasks,
  known,
  listed,
  groupLoaded,
  loading,
  loadingOlder,
  nextBefore,
  problem,
  olderProblem,
  returnTargetId,
  returningFromDetail,
  load,
  loadOlder,
  selectGroup,
  revisit,
  create,
  markOpened,
  clearReturnFlag,
} = useWorkDesk()

const filterRef = ref<InstanceType<typeof WorkStatusFilter> | null>(null)
const hooksTitleEl = ref<HTMLElement | null>(null)
const listEl = ref<HTMLElement | null>(null)

const currentMeta = computed(() => statusGroupMeta(activeGroup.value))
const receptionNote = computed(() => describeReception(reception.value, 'work'))
const maintenance = useMaintenance()

const draft = ref('')
const filename = ref('')
const sending = ref(false)
const trouble = ref('')
let attempt: { body: string; name: string | null; key: string } | null = null
// 交出去的回应迟到时已经离开列表或换了账号，就不再跳转、不再改草稿。
const scope = usePrivateScope(() => 'desk')

function onSelectGroup(group: WorkStatusGroup) {
  void selectGroup(group)
}

function focusFallbackControl() {
  if (!filterRef.value?.focusGroup(activeGroup.value)) {
    hooksTitleEl.value?.focus({ preventScroll: true })
  }
}

async function restoreReturnFocus() {
  if (!returningFromDetail.value) return
  const targetId = returnTargetId.value
  clearReturnFlag()
  // 等过 App.vue 里 router.afterEach 的 nextTick 焦点逻辑，再把焦点落回刚才打开的单子。
  await nextTick()
  await nextTick()
  const activeEl = document.activeElement
  if (activeEl && activeEl !== document.body && activeEl.id !== 'main-content') return
  if (targetId) {
    const link = listEl.value?.querySelector<HTMLElement>(
      `[data-task-id="${CSS.escape(targetId)}"]`,
    )
    if (link) {
      link.focus({ preventScroll: true })
      return
    }
  }
  focusFallbackControl()
}

// 同一段正文重试沿用同一个键，服务端只记一件。
async function send() {
  if (sending.value || !draft.value.trim()) return
  const body = draft.value.trim()
  const name = filename.value.trim() || null
  if (!attempt || attempt.body !== body || attempt.name !== name) {
    attempt = { body, name, key: crypto.randomUUID() }
  }
  const current = scope.capture()
  sending.value = true
  trouble.value = ''
  try {
    const task = await create(body, attempt.key, name)
    if (!task || !current()) return
    draft.value = ''
    filename.value = ''
    attempt = null
    markOpened(task.id)
    void router.push(`/work/${task.id}`)
  } catch (error) {
    if (current()) trouble.value = explain(error)
  } finally {
    if (current()) sending.value = false
  }
}

watch(
  () => desk.value?.id ?? null,
  (deskId) => {
    if (!deskId) {
      maintenance.reset()
      return
    }
    if (!maintenance.loadedOnce.value) void maintenance.load()
  },
  { immediate: true },
)

watch(
  () => session.user.value?.id,
  async (id) => {
    draft.value = ''
    filename.value = ''
    attempt = null
    sending.value = false
    trouble.value = ''
    if (!id) return
    if (listed.value) {
      const pendingFocus = restoreReturnFocus()
      await revisit()
      await pendingFocus
      await nextTick()
      if (document.activeElement === document.body) focusFallbackControl()
      return
    }
    await load()
  },
  { immediate: true },
)
</script>

<template>
  <div class="work-desk">
    <header class="work-heading">
      <h1
        id="private-title"
        class="overprint"
        tabindex="-1"
      >
        交办
      </h1>
      <p>交给米洛去做的事，一件一张单子，每一步都盖章记下来。</p>
      <p class="work-offline">交办先存好，做到哪一步，看单子上的记录。</p>
    </header>
    <PrivateGate
      below
      @reload="load"
    >
      <div
        v-if="problem && !desk"
        class="work-list-alert"
        role="alert"
      >
        <p>{{ problem }}</p>
        <button
          type="button"
          class="work-retry-button"
          :disabled="loading"
          @click="load"
        >
          {{ loading ? '正在读取…' : '重新读取' }}
        </button>
      </div>
      <p
        v-else-if="loading && !listed"
        class="empty-history"
        role="status"
      >
        正在翻交办单…
      </p>
      <p
        v-else-if="known && listed && !desk"
        class="empty-history"
      >
        这个账号没有交办权限。
      </p>
      <template v-else-if="desk">
        <p
          v-if="receptionNote"
          class="work-reception-note"
          role="status"
        >
          {{ receptionNote }}
        </p>
        <MaintenanceNotice
          :summary="maintenance.summary.value"
          :visible="maintenance.visible.value"
          :loading="maintenance.loading.value"
          :problem="maintenance.problem.value"
          @reload="maintenance.reload"
        />
        <ClipBoard class="new-task">
          <p class="sheet-no">交办单 · 新的一张</p>
          <WorkComposer
            v-model="draft"
            label="要米洛做什么"
            action="交出去"
            :sending="sending"
            @send="send"
          >
            <template #deliverable>
              <DeliverableField
                v-model="filename"
                :sending="sending"
              />
            </template>
            <template #trouble>
              <p
                v-if="trouble"
                class="feedback error"
                role="alert"
              >
                {{ trouble }}
                <template v-if="draft">正文还在，内容不变时再交一次不会变成两件。</template>
              </p>
            </template>
          </WorkComposer>
        </ClipBoard>
        <section
          class="task-hooks"
          aria-labelledby="hooks-title"
        >
          <h2
            id="hooks-title"
            ref="hooksTitleEl"
            class="hooks-heading"
            tabindex="-1"
          >
            交过的事
          </h2>

          <WorkStatusFilter
            ref="filterRef"
            :active-group="activeGroup"
            :loading="loading"
            :group-loaded="groupLoaded"
            :task-count="tasks.length"
            :has-more="nextBefore !== null"
            :problem="problem"
            @select="onSelectGroup"
          />

          <div
            v-if="problem"
            class="work-list-alert"
            role="alert"
          >
            <p>{{ problem }}</p>
            <button
              type="button"
              class="work-retry-button"
              :disabled="loading"
              @click="load"
            >
              {{ loading ? '正在重试…' : '重试这一组' }}
            </button>
          </div>

          <p
            v-if="loading && !groupLoaded && !problem"
            class="empty-history"
          >
            正在翻「{{ currentMeta.label }}」的交办单…
          </p>
          <p
            v-else-if="groupLoaded && !tasks.length && !problem"
            class="empty-history"
          >
            {{ currentMeta.empty }}
          </p>
          <ul
            v-else-if="tasks.length"
            ref="listEl"
          >
            <li
              v-for="task in tasks"
              :key="task.id"
            >
              <RouterLink
                class="task-tag"
                :class="`is-${effectiveTaskCopy(task).toneKey}`"
                :data-task-id="task.id"
                :to="`/work/${task.id}`"
                @click="markOpened(task.id)"
              >
                <span class="sheet-no"
                  >{{ sheetNumber(task) }} · {{ dayLabel(task.created_at) }}</span
                >
                <span class="task-tag-title">{{ taskTitle(task) }}</span>
                <span class="task-tag-state">{{ effectiveTaskCopy(task).line }}</span>
              </RouterLink>
            </li>
          </ul>

          <div
            v-if="olderProblem"
            class="work-list-alert is-older"
            role="alert"
          >
            <p>{{ olderProblem }}</p>
            <button
              type="button"
              class="work-retry-button"
              :disabled="loading || loadingOlder"
              @click="loadOlder"
            >
              {{ loadingOlder ? '正在翻更早的交办…' : '重试加载' }}
            </button>
          </div>

          <div
            v-if="nextBefore !== null && groupLoaded"
            class="work-more-row"
          >
            <button
              type="button"
              class="work-more-button"
              :aria-disabled="loading || loadingOlder"
              @click="!loading && !loadingOlder && loadOlder()"
            >
              {{ loadingOlder ? '正在翻更早的交办…' : '更早的交办' }}
            </button>
          </div>
        </section>
      </template>
    </PrivateGate>
  </div>
</template>
