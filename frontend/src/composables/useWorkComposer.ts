import { computed, ref, type Ref } from 'vue'
import { useRouter } from 'vue-router'
import { ApiFailure, explain } from '../api/client'
import type { WorkInput, WorkTask } from '../api/client'
import { canClarifyInPlace } from '../utils/work'
import { useWorkDesk } from './useWorkDesk'

interface UseWorkComposerOptions {
  task: Ref<WorkTask | null>
  capture: () => () => boolean
  append: (body: string, key: string, activeTurn: string | null) => Promise<WorkInput | null>
  onFollowUpCreated: () => void
}

/** Manage supplement / follow-up task draft, idempotency key, and submission state. */
export function useWorkComposer(options: UseWorkComposerOptions) {
  const router = useRouter()
  const desk = useWorkDesk()

  const draft = ref('')
  const filename = ref('')
  const sending = ref(false)
  const trouble = ref('')
  const notice = ref('')
  const attempt = ref<{
    body: string
    key: string
    mode: 'append' | 'create'
    name: string | null
    turn: string | null
  } | null>(null)

  const status = computed(() => options.task.value?.status ?? null)
  const clarifyInPlace = computed(() => canClarifyInPlace(options.task.value))
  const retryAppend = computed(
    () => attempt.value?.mode === 'append' && attempt.value.body === draft.value.trim(),
  )
  const composer = computed(() => {
    if (retryAppend.value) {
      return { label: '刚才补的话', action: '确认补话是否存好', held: '' }
    }
    if (clarifyInPlace.value) {
      return { label: '补充说明，接着这件做', action: '补上说明', held: '' }
    }
    if (status.value === 'queued') return { label: '补几句', action: '补上', held: '' }
    if (status.value === 'running' && options.task.value?.active_turn) {
      return { label: '给这一轮补几句', action: '补给这一轮', held: '' }
    }
    if (status.value === 'running' || status.value === 'stopping') {
      return {
        label: '想补的话先写在这里',
        action: '补上',
        held:
          status.value === 'stopping'
            ? '正在停下，这时不能补话。'
            : '现在不能补给这一轮，草稿先留着。',
      }
    }
    if (status.value === 'needs_reconciliation') {
      return {
        label: '另开一件交办',
        action: '另开一件交办',
        held: '情况还不清楚，先别重新交一遍；等米洛核对完再说。',
      }
    }
    return { label: '另开一件交办', action: '另开一件交办', held: '' }
  })

  async function send() {
    if (sending.value || composer.value.held || !draft.value.trim()) return
    const body = draft.value.trim()
    const clarifying = clarifyInPlace.value
    const turn = clarifying ? null : (options.task.value?.active_turn ?? null)
    const mode =
      clarifying || status.value === 'queued' || (status.value === 'running' && turn)
        ? 'append'
        : 'create'
    const name = mode === 'create' ? filename.value.trim() || null : null
    if (
      !retryAppend.value &&
      (!attempt.value ||
        attempt.value.body !== body ||
        attempt.value.mode !== mode ||
        attempt.value.name !== name)
    ) {
      attempt.value = { body, key: crypto.randomUUID(), mode, name, turn }
    }
    const pending = attempt.value
    if (!pending) return
    const current = options.capture()
    sending.value = true
    trouble.value = ''
    notice.value = ''
    try {
      if (pending.mode === 'create') {
        if (!desk.desk.value) await desk.load()
        if (!current()) return
        const created = await desk.create(body, pending.key, pending.name)
        if (!created || !current()) return
        draft.value = ''
        filename.value = ''
        attempt.value = null
        options.onFollowUpCreated()
        void router.push(`/work/${created.id}`)
        return
      }
      const saved = await options.append(body, pending.key, pending.turn)
      if (!saved || !current()) return
      draft.value = ''
      attempt.value = null
      notice.value = clarifying
        ? '补充说明已存回这张原单，米洛会接着这份原单继续看。'
        : '补话存好了。递没递到，看单上的补话记录。'
    } catch (error) {
      if (current()) {
        trouble.value = explain(error)
        if (error instanceof ApiFailure && error.status === 409) attempt.value = null
      }
    } finally {
      if (current()) sending.value = false
    }
  }

  function resetComposer() {
    draft.value = ''
    filename.value = ''
    attempt.value = null
    sending.value = false
    trouble.value = ''
    notice.value = ''
  }

  return {
    draft,
    filename,
    sending,
    trouble,
    notice,
    composer,
    clarifyInPlace,
    retryAppend,
    send,
    resetComposer,
  }
}
