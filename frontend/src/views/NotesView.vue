<script setup lang="ts">
import { ref } from 'vue'
import { HOME_RECENT_NOTES } from '../data/homeNotes'
import { usePublicNotesHistory } from '../composables/usePublicNotes'
import { usePublicNotesFocus } from '../composables/usePublicNotesFocus'
import { formatNoteDate } from '../utils/publicNotes'
import '../styles/public-notes.css'

const {
  notes,
  nextBefore,
  hasLoaded,
  reachedEnd,
  legacyMode,
  loading,
  loadingMore,
  problem,
  refreshProblem,
  moreProblem,
  refresh,
  loadMore,
} = usePublicNotesHistory()

const pageElement = ref<HTMLElement | null>(null)
usePublicNotesFocus(pageElement, [loading, loadingMore, nextBefore, problem], '.notes-page-title')

function onRefresh() {
  if (!loading.value && !loadingMore.value) void refresh()
}

function onLoadMore() {
  if (!loading.value && !loadingMore.value && nextBefore.value !== null) void loadMore()
}
</script>

<template>
  <div
    ref="pageElement"
    class="public-notes-page"
    :aria-busy="loading || loadingMore"
  >
    <nav
      class="notes-back-row"
      aria-label="页面返回"
    >
      <RouterLink
        class="notes-back-link"
        to="/"
        >← 回到首页</RouterLink
      >
    </nav>

    <header class="notes-header">
      <div class="notes-header-copy">
        <h1
          class="notes-page-title overprint"
          tabindex="-1"
        >
          家里近况
        </h1>
        <p class="notes-page-sub">
          {{
            legacyMode
              ? '随站保留的旧近况，暂不能读取更新'
              : '按发布顺序留下的公开小记，最新的在最上面。'
          }}
        </p>
      </div>

      <button
        type="button"
        class="text-button notes-refresh-btn"
        :aria-disabled="loading || loadingMore"
        :aria-busy="loading"
        @click="onRefresh"
      >
        {{ loading ? '正在重读…' : '刷新' }}
      </button>
    </header>

    <p
      v-if="refreshProblem && hasLoaded"
      class="notes-inline-notice is-warn"
      role="status"
    >
      {{ refreshProblem }}
    </p>

    <!-- 1. 旧后端列表 404 降级：展示随站保留的旧近况与回首页入口，不留不可用详情链接 -->
    <section
      v-if="legacyMode"
      class="notes-legacy-block"
      aria-label="随站保留的旧近况"
    >
      <p
        class="notes-inline-notice"
        role="status"
      >
        当前站点接口暂未开放近况历史，这里只保留随站自带的三条旧近况，暂不能读取更新或打开详情。
      </p>
      <ol class="notes-list">
        <li
          v-for="item in HOME_RECENT_NOTES"
          :key="item.id"
          class="notes-list-item is-legacy"
        >
          <p class="notes-item-meta">
            <time :datetime="item.isoDate">{{
              formatNoteDate(item.isoDate, { withYear: true })
            }}</time>
            <template v-if="item.category">
              <span aria-hidden="true">·</span>
              <span class="notes-item-kind">{{ item.category }}</span>
            </template>
          </p>
          <h2 class="notes-item-title">{{ item.title }}</h2>
          <p class="notes-item-summary">{{ item.text }}</p>
        </li>
      </ol>
      <p class="notes-end-row">
        <RouterLink to="/">回到首页</RouterLink>
      </p>
    </section>

    <!-- 2. 首次读取中 -->
    <div
      v-else-if="loading && !hasLoaded"
      class="notes-state-box"
      role="status"
    >
      <p>正在读取公开近况…</p>
    </div>

    <!-- 3. 首次读取失败 -->
    <div
      v-else-if="problem && !hasLoaded"
      class="notes-state-box is-error"
      role="alert"
    >
      <p>{{ problem }}</p>
      <div class="notes-state-actions">
        <button
          type="button"
          class="text-button notes-retry-btn"
          :aria-disabled="loading"
          :aria-busy="loading"
          @click="onRefresh"
        >
          {{ loading ? '正在重读…' : '重新读取' }}
        </button>
        <RouterLink to="/">回到首页</RouterLink>
      </div>
    </div>

    <!-- 4. 真实 200 空列表 -->
    <div
      v-else-if="hasLoaded && notes.length === 0"
      class="notes-state-box"
      role="status"
    >
      <p>还没有公开近况。</p>
      <div class="notes-state-actions">
        <RouterLink to="/">回到首页</RouterLink>
      </div>
    </div>

    <!-- 5. 真实公开历史列表 -->
    <template v-else>
      <ol class="notes-list">
        <li
          v-for="note in notes"
          :key="note.id"
          class="notes-list-item"
        >
          <p class="notes-item-meta">
            <time :datetime="note.content_date">{{
              formatNoteDate(note.content_date, { withYear: true })
            }}</time>
            <template v-if="note.category">
              <span aria-hidden="true">·</span>
              <span class="notes-item-kind">{{ note.category }}</span>
            </template>
          </p>
          <h2 class="notes-item-title">
            <RouterLink
              class="notes-item-link"
              :to="`/notes/${note.id}`"
              >{{ note.title }}</RouterLink
            >
          </h2>
          <p class="notes-item-summary">{{ note.summary }}</p>
        </li>
      </ol>

      <div class="notes-pager">
        <p
          v-if="moreProblem"
          class="notes-pager-error"
          role="alert"
        >
          {{ moreProblem }}
        </p>

        <button
          v-if="nextBefore !== null"
          type="button"
          class="notes-more-btn"
          :aria-disabled="loading || loadingMore"
          :aria-busy="loadingMore"
          @click="onLoadMore"
        >
          {{ loadingMore ? '正在翻更早的…' : '翻看更早的近况' }}
        </button>

        <p
          v-else-if="reachedEnd"
          class="notes-end-note"
          role="status"
        >
          没有更早的公开近况了。
        </p>
      </div>
    </template>
  </div>
</template>
