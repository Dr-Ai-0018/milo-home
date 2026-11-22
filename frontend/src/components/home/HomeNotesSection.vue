<script setup lang="ts">
import { computed, ref } from 'vue'
import type { PublicNoteSummary } from '../../api/client'
import PaperSheet from '../ink/PaperSheet.vue'
import { HOME_RECENT_NOTES } from '../../data/homeNotes'
import type { HomeRecentNote } from '../../data/homeNotes'
import { formatNoteDate } from '../../utils/publicNotes'
import { usePublicNotesFocus } from '../../composables/usePublicNotesFocus'
import '../../styles/home-preview-sections.css'

const props = withDefaults(
  defineProps<{
    notes?: readonly PublicNoteSummary[]
    hasLoaded?: boolean
    legacyMode?: boolean
    loading?: boolean
    problem?: string
    refreshProblem?: string
    /** 仅开发环境首页预览使用静态样本，不触发接口交互。 */
    staticPreviewNotes?: readonly HomeRecentNote[]
  }>(),
  {
    notes: () => [],
    hasLoaded: false,
    legacyMode: false,
    loading: false,
    problem: '',
    refreshProblem: '',
    staticPreviewNotes: undefined,
  },
)

const emit = defineEmits<{
  reload: []
}>()

const sectionElement = ref<HTMLElement | null>(null)
usePublicNotesFocus(
  sectionElement,
  [() => props.loading, () => props.hasLoaded, () => props.problem, () => props.legacyMode],
  '#home-notes-title',
)

const isPreview = computed(() => props.staticPreviewNotes !== undefined)
const displayLegacy = computed(() => !isPreview.value && props.legacyMode)
const staticItems = computed<readonly HomeRecentNote[]>(() =>
  isPreview.value ? (props.staticPreviewNotes ?? []) : HOME_RECENT_NOTES,
)

const liveLead = computed(() => props.notes[0] ?? null)
const liveMore = computed(() => props.notes.slice(1))

const staticLead = computed(() => staticItems.value[0] ?? null)
const staticMore = computed(() => staticItems.value.slice(1))

const showHistoryLink = computed(
  () => !isPreview.value && !props.legacyMode && props.hasLoaded && props.notes.length > 0,
)

const headingSubcopy = computed(() => {
  if (displayLegacy.value) return '随站保留的旧近况，暂不能读取更新'
  return '只记已经做完、可公开的小家变化'
})

function onReload() {
  if (!props.loading) emit('reload')
}
</script>

<template>
  <section
    ref="sectionElement"
    class="home-notes-section"
    aria-labelledby="home-notes-title"
    :aria-busy="loading"
  >
    <div class="home-notes-heading">
      <div class="home-notes-heading-copy">
        <h2
          id="home-notes-title"
          tabindex="-1"
          class="home-section-eyebrow"
        >
          家里最近
        </h2>
        <p class="home-notes-sub">{{ headingSubcopy }}</p>
      </div>

      <div
        v-if="!isPreview"
        class="home-notes-actions"
      >
        <RouterLink
          v-if="showHistoryLink"
          class="home-notes-all-link"
          to="/notes"
          >全部近况</RouterLink
        >
        <button
          v-if="hasLoaded || legacyMode"
          type="button"
          class="text-button home-notes-reload"
          :aria-disabled="loading"
          :aria-busy="loading"
          @click="onReload"
        >
          {{ loading ? '正在读…' : '重读' }}
        </button>
      </div>
    </div>

    <p
      v-if="refreshProblem && hasLoaded"
      class="home-notes-notice is-warn"
      role="status"
    >
      {{ refreshProblem }}
    </p>

    <p
      v-if="displayLegacy"
      class="home-notes-notice"
      role="status"
    >
      当前后端暂未开放公开近况接口，下面仅显示随站保留的旧近况，暂不能读取更新或打开详情。
    </p>

    <!-- 1. 静态预览态（开发预览页）或旧后端 404 降级态：纯静态只读，不伪造历史或详情链接。 -->
    <div
      v-if="isPreview || displayLegacy"
      class="home-notes-stack"
    >
      <PaperSheet
        v-if="staticLead"
        as="article"
        class="home-lead-note"
        paper="ruled"
        tape="left"
        :tilt="-0.4"
      >
        <p class="home-note-meta">
          <time :datetime="staticLead.isoDate">{{ staticLead.date }}</time>
          <template v-if="staticLead.category">
            <span aria-hidden="true">·</span>
            <span class="home-note-kind">{{ staticLead.category }}</span>
          </template>
        </p>
        <h3 class="home-lead-note-title">{{ staticLead.title }}</h3>
        <p class="home-lead-note-text">{{ staticLead.text }}</p>
      </PaperSheet>

      <div
        v-if="staticMore.length"
        class="home-index-block"
      >
        <p class="home-index-label">再往前两条</p>
        <ol class="home-index-list">
          <li
            v-for="note in staticMore"
            :key="note.id"
            class="home-index-item"
          >
            <p class="home-note-meta">
              <time :datetime="note.isoDate">{{ note.date }}</time>
              <template v-if="note.category">
                <span aria-hidden="true">·</span>
                <span>{{ note.category }}</span>
              </template>
            </p>
            <h3 class="home-index-title">{{ note.title }}</h3>
            <p class="home-index-text">{{ note.text }}</p>
          </li>
        </ol>
      </div>
    </div>

    <!-- 2. 首次加载中 -->
    <div
      v-else-if="loading && !hasLoaded"
      class="home-notes-state"
      role="status"
    >
      <p>正在读取家里近况…</p>
    </div>

    <!-- 3. 首次读取失败（503 / 500 / 网络）：不退回静态旧近况，不挡作息与入口 -->
    <div
      v-else-if="problem && !hasLoaded"
      class="home-notes-state is-error"
      role="alert"
    >
      <p>{{ problem }}</p>
      <button
        type="button"
        class="text-button home-notes-retry"
        :aria-disabled="loading"
        :aria-busy="loading"
        @click="onReload"
      >
        {{ loading ? '正在重读…' : '重新读取' }}
      </button>
    </div>

    <!-- 4. 新接口 200 真实空态：不回填随站旧近况 -->
    <div
      v-else-if="hasLoaded && notes.length === 0"
      class="home-notes-state"
      role="status"
    >
      <p>还没有公开近况。</p>
    </div>

    <!-- 5. 新接口 200 真实近况（最多 3 条） -->
    <div
      v-else
      class="home-notes-stack"
    >
      <PaperSheet
        v-if="liveLead"
        as="article"
        class="home-lead-note"
        paper="ruled"
        tape="left"
        :tilt="-0.4"
      >
        <p class="home-note-meta">
          <time :datetime="liveLead.content_date">{{
            formatNoteDate(liveLead.content_date, { withYear: false })
          }}</time>
          <template v-if="liveLead.category">
            <span aria-hidden="true">·</span>
            <span class="home-note-kind">{{ liveLead.category }}</span>
          </template>
        </p>
        <h3 class="home-lead-note-title">
          <RouterLink
            class="home-note-link"
            :to="`/notes/${liveLead.id}`"
            >{{ liveLead.title }}</RouterLink
          >
        </h3>
        <p class="home-lead-note-text">{{ liveLead.summary }}</p>
        <p class="home-note-readmore">
          <RouterLink :to="`/notes/${liveLead.id}`">读全文</RouterLink>
        </p>
      </PaperSheet>

      <div
        v-if="liveMore.length"
        class="home-index-block"
      >
        <p class="home-index-label">{{ liveMore.length === 1 ? '再往前一条' : '再往前两条' }}</p>
        <ol class="home-index-list">
          <li
            v-for="note in liveMore"
            :key="note.id"
            class="home-index-item"
          >
            <p class="home-note-meta">
              <time :datetime="note.content_date">{{
                formatNoteDate(note.content_date, { withYear: false })
              }}</time>
              <template v-if="note.category">
                <span aria-hidden="true">·</span>
                <span>{{ note.category }}</span>
              </template>
            </p>
            <h3 class="home-index-title">
              <RouterLink
                class="home-note-link"
                :to="`/notes/${note.id}`"
                >{{ note.title }}</RouterLink
              >
            </h3>
            <p class="home-index-text">{{ note.summary }}</p>
          </li>
        </ol>
      </div>
    </div>
  </section>
</template>
