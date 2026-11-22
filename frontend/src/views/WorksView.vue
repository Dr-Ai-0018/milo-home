<script setup lang="ts">
import { nextTick, onMounted, ref } from 'vue'
import WorkCoverFigure from '../components/works/WorkCoverFigure.vue'
import {
  consumeLastOpenedWork,
  rememberOpenedWork,
  usePublicWorksCatalog,
} from '../composables/usePublicWorks'
import { usePublicNotesFocus } from '../composables/usePublicNotesFocus'
import { formatNoteDate } from '../utils/publicNotes'
import '../styles/home-works.css'
import '../styles/public-works.css'

const {
  works,
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
} = usePublicWorksCatalog()

const pageElement = ref<HTMLElement | null>(null)
usePublicNotesFocus(pageElement, [loading, loadingMore, nextBefore, problem], '.works-page-title')

// 从作品详情返回目录时，若用户尚未把焦点移到别处，就把焦点轻量交回刚才打开的那件作品。
onMounted(() => {
  const lastId = consumeLastOpenedWork()
  if (!lastId || !hasLoaded.value) return
  void nextTick(() => {
    window.requestAnimationFrame(() => {
      const root = pageElement.value
      if (!root?.isConnected) return
      const active = document.activeElement
      if (
        active &&
        active !== document.body &&
        active.id !== 'main-content' &&
        !root.contains(active)
      ) {
        return
      }
      const target = [...root.querySelectorAll<HTMLAnchorElement>('.work-title-link')].find(
        (el) => el.dataset.workId === lastId,
      )
      target?.focus({ preventScroll: true })
    })
  })
})

function onRefresh() {
  if (!loading.value && !loadingMore.value) void refresh()
}

function onLoadMore() {
  if (!loading.value && !loadingMore.value && nextBefore.value !== null) void loadMore()
}

function onOpenWork(workId: string) {
  rememberOpenedWork(workId)
}
</script>

<template>
  <div
    ref="pageElement"
    class="public-works-page"
    :aria-busy="loading || loadingMore"
  >
    <nav
      class="works-back-row"
      aria-label="页面返回"
    >
      <RouterLink
        class="works-back-link"
        to="/"
        >← 回到首页</RouterLink
      >
    </nav>

    <header class="works-header">
      <div class="works-header-copy">
        <h1
          class="works-page-title overprint"
          tabindex="-1"
        >
          作品
        </h1>
        <p class="works-page-sub">
          {{
            legacyMode ? '作品还没接到这里' : '明确选出来、可以长期介绍、直接访问或带走的公开成果。'
          }}
        </p>
      </div>

      <button
        type="button"
        class="text-button works-refresh-btn"
        :aria-disabled="loading || loadingMore"
        :aria-busy="loading"
        @click="onRefresh"
      >
        {{ loading ? '正在重读…' : '刷新' }}
      </button>
    </header>

    <p
      v-if="refreshProblem && hasLoaded"
      class="works-inline-notice is-warn"
      role="status"
    >
      {{ refreshProblem }}
    </p>

    <!-- 1. 旧后端列表 404：说明还没接到这里，不伪造作品或无效详情入口 -->
    <div
      v-if="legacyMode"
      class="works-state-box"
      role="status"
    >
      <p>作品还没接到这里，当前后端暂未开放公开作品目录。</p>
      <div class="works-state-actions">
        <RouterLink to="/">回到首页</RouterLink>
      </div>
    </div>

    <!-- 2. 首次读取中 -->
    <div
      v-else-if="loading && !hasLoaded"
      class="works-state-box"
      role="status"
    >
      <p>正在读取公开作品…</p>
    </div>

    <!-- 3. 首次读取失败 -->
    <div
      v-else-if="problem && !hasLoaded"
      class="works-state-box is-error"
      role="alert"
    >
      <p>{{ problem }}</p>
      <div class="works-state-actions">
        <button
          type="button"
          class="text-button works-retry-btn"
          :aria-disabled="loading"
          :aria-busy="loading"
          @click="onRefresh"
        >
          {{ loading ? '正在重读…' : '重新读取' }}
        </button>
        <RouterLink to="/">回到首页</RouterLink>
      </div>
    </div>

    <!-- 4. 真实 200 空目录 -->
    <div
      v-else-if="hasLoaded && works.length === 0"
      class="works-state-box"
      role="status"
    >
      <p>还没有公开作品。</p>
      <div class="works-state-actions">
        <RouterLink to="/">回到首页</RouterLink>
      </div>
    </div>

    <!-- 5. 真实公开作品目录（10 条向前翻，细线目录不做卡片墙） -->
    <template v-else>
      <ol class="works-list">
        <li
          v-for="item in works"
          :key="item.id"
          class="works-list-item"
          :class="{ 'has-cover': !!item.cover }"
        >
          <div class="works-item-copy">
            <p class="work-meta-line">
              <time :datetime="item.content_date">{{
                formatNoteDate(item.content_date, { withYear: true })
              }}</time>
              <template v-if="item.category">
                <span aria-hidden="true">·</span>
                <span class="work-meta-kind">{{ item.category }}</span>
              </template>
              <template v-if="item.version_label">
                <span aria-hidden="true">·</span>
                <span class="work-meta-version">{{ item.version_label }}</span>
              </template>
            </p>
            <h2 class="works-item-title">
              <RouterLink
                class="work-title-link"
                :data-work-id="item.id"
                :to="`/works/${item.id}`"
                @click="onOpenWork(item.id)"
                >{{ item.title }}</RouterLink
              >
            </h2>
            <p class="works-item-summary">{{ item.summary }}</p>
          </div>

          <WorkCoverFigure
            v-if="item.cover"
            :cover="item.cover"
            variant="catalog"
          />
        </li>
      </ol>

      <div class="works-pager">
        <p
          v-if="moreProblem"
          class="works-pager-error"
          role="alert"
        >
          {{ moreProblem }}
        </p>

        <button
          v-if="nextBefore !== null"
          type="button"
          class="works-more-btn"
          :aria-disabled="loading || loadingMore"
          :aria-busy="loadingMore"
          @click="onLoadMore"
        >
          {{ loadingMore ? '正在翻更早的…' : '翻看更早的作品' }}
        </button>

        <p
          v-else-if="reachedEnd"
          class="works-end-note"
          role="status"
        >
          没有更早的公开作品了。
        </p>
      </div>
    </template>
  </div>
</template>
