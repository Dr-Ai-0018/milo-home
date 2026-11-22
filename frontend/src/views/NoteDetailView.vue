<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import PaperSheet from '../components/ink/PaperSheet.vue'
import { usePublicNoteDetail } from '../composables/usePublicNotes'
import { usePublicNotesFocus } from '../composables/usePublicNotesFocus'
import { formatNoteDate, formatNoteRevisedAt } from '../utils/publicNotes'
import '../styles/public-notes.css'

const route = useRoute()
const noteId = computed(() => (typeof route.params.id === 'string' ? route.params.id : ''))

const { note, loading, notFound, problem, refreshProblem, reload } = usePublicNoteDetail(noteId)

const revisedAt = computed(() => (note.value ? formatNoteRevisedAt(note.value) : null))
const pageElement = ref<HTMLElement | null>(null)
usePublicNotesFocus(pageElement, [note, loading, notFound, problem], '.notes-back-link')

watch(
  note,
  (current) => {
    document.title = current ? `${current.title} · 米洛在家` : '一条近况 · 米洛在家'
  },
  { immediate: true },
)

function onReload() {
  if (!loading.value) reload()
}
</script>

<template>
  <div
    ref="pageElement"
    class="public-notes-page note-detail-page"
    :aria-busy="loading"
  >
    <nav
      class="notes-back-row"
      aria-label="页面返回"
    >
      <RouterLink
        class="notes-back-link"
        to="/notes"
        >← 回到近况列表</RouterLink
      >
      <RouterLink
        class="notes-back-secondary"
        to="/"
        >回到首页</RouterLink
      >
    </nav>

    <!-- 1. 首次读取中 -->
    <div
      v-if="loading && !note"
      class="notes-state-box"
      role="status"
    >
      <p>正在打开这条近况…</p>
    </div>

    <!-- 2. 详情 404：草稿 / 撤下 / 不存在统一不可读取，绝不回退静态正文 -->
    <div
      v-else-if="notFound"
      class="notes-state-box"
      role="status"
    >
      <h1 class="notes-state-title">这条近况当前不可读取</h1>
      <p>它可能已经被撤下，还没有公开，或者链接地址不存在。</p>
      <div class="notes-state-actions">
        <RouterLink to="/notes">回到近况列表</RouterLink>
        <RouterLink to="/">回到首页</RouterLink>
      </div>
    </div>

    <!-- 3. 首次读取失败（503 / 500 / 网络） -->
    <div
      v-else-if="problem && !note"
      class="notes-state-box is-error"
      role="alert"
    >
      <h1 class="notes-state-title">暂时没能打开这条近况</h1>
      <p>{{ problem }}</p>
      <div class="notes-state-actions">
        <button
          type="button"
          class="text-button notes-retry-btn"
          :aria-disabled="loading"
          :aria-busy="loading"
          @click="onReload"
        >
          {{ loading ? '正在重读…' : '重新读取' }}
        </button>
        <RouterLink to="/notes">回到近况列表</RouterLink>
      </div>
    </div>

    <!-- 4. 正文详情：主展示纯文本 body，不与 summary 复读 -->
    <template v-else-if="note">
      <p
        v-if="refreshProblem"
        class="notes-inline-notice is-warn"
        role="status"
      >
        {{ refreshProblem }}
      </p>

      <PaperSheet
        as="article"
        class="note-detail-sheet"
        paper="plain"
      >
        <header class="note-detail-header">
          <div class="note-detail-meta-row">
            <p class="notes-item-meta">
              <time :datetime="note.content_date">{{
                formatNoteDate(note.content_date, { withYear: true })
              }}</time>
              <template v-if="note.category">
                <span aria-hidden="true">·</span>
                <span class="notes-item-kind">{{ note.category }}</span>
              </template>
              <template v-if="revisedAt">
                <span aria-hidden="true">·</span>
                <span class="note-detail-revised">修订于 {{ revisedAt }}</span>
              </template>
            </p>

            <button
              type="button"
              class="text-button notes-refresh-btn"
              :aria-disabled="loading"
              :aria-busy="loading"
              @click="onReload"
            >
              {{ loading ? '正在重读…' : '重读' }}
            </button>
          </div>

          <h1 class="note-detail-title">{{ note.title }}</h1>
        </header>

        <div class="note-detail-body">{{ note.body }}</div>
      </PaperSheet>

      <footer class="note-detail-footer">
        <RouterLink to="/notes">← 回到近况列表</RouterLink>
        <RouterLink to="/">回到首页</RouterLink>
      </footer>
    </template>
  </div>
</template>
