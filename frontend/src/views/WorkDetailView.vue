<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import PaperSheet from '../components/ink/PaperSheet.vue'
import WorkCoverFigure from '../components/works/WorkCoverFigure.vue'
import { usePublicWorkDetail } from '../composables/usePublicWorkDetail'
import { usePublicNotesFocus } from '../composables/usePublicNotesFocus'
import { formatNoteDate, formatNoteRevisedAt } from '../utils/publicNotes'
import { isExternalWorkLink, workLinkHost } from '../utils/publicWorks'
import { formatBytes, shortDigest } from '../utils/work'
import '../styles/home-works.css'
import '../styles/public-works.css'

const route = useRoute()
const workId = computed(() => (typeof route.params.id === 'string' ? route.params.id : ''))

const {
  work,
  loading,
  notFound,
  problem,
  refreshProblem,
  downloadingId,
  fileErrors,
  fileDone,
  reload,
  downloadFile,
} = usePublicWorkDetail(workId)

const revisedAt = computed(() => (work.value ? formatNoteRevisedAt(work.value) : null))
const hasOutcomes = computed(
  () => !!work.value && (work.value.links.length > 0 || work.value.files.length > 0),
)

const pageElement = ref<HTMLElement | null>(null)
usePublicNotesFocus(pageElement, [work, loading, notFound, problem], '.works-back-link')

watch(
  work,
  (current) => {
    document.title = current ? `${current.title} · 米洛在家` : '一件作品 · 米洛在家'
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
    class="public-works-page work-detail-page"
    :aria-busy="loading"
  >
    <nav
      class="works-back-row"
      aria-label="页面返回"
    >
      <RouterLink
        class="works-back-link"
        to="/works"
        >← 回到作品目录</RouterLink
      >
      <RouterLink
        class="works-back-secondary"
        to="/"
        >回到首页</RouterLink
      >
    </nav>

    <!-- 1. 首次读取中 -->
    <div
      v-if="loading && !work"
      class="works-state-box"
      role="status"
    >
      <p>正在打开这件作品…</p>
    </div>

    <!-- 2. 详情 404：撤下 / 草稿 / 不存在统一不可读取，不找回旧正文 -->
    <div
      v-else-if="notFound"
      class="works-state-box"
      role="status"
    >
      <h1 class="works-state-title">这件作品当前不可读取</h1>
      <p>它可能已经被撤下，还没有公开，或者链接地址不存在。</p>
      <div class="works-state-actions">
        <RouterLink to="/works">回到作品目录</RouterLink>
        <RouterLink to="/">回到首页</RouterLink>
      </div>
    </div>

    <!-- 3. 首次读取失败（503 / 500 / 网络） -->
    <div
      v-else-if="problem && !work"
      class="works-state-box is-error"
      role="alert"
    >
      <h1 class="works-state-title">暂时没能打开这件作品</h1>
      <p>{{ problem }}</p>
      <div class="works-state-actions">
        <button
          type="button"
          class="text-button works-retry-btn"
          :aria-disabled="loading"
          :aria-busy="loading"
          @click="onReload"
        >
          {{ loading ? '正在重读…' : '重新读取' }}
        </button>
        <RouterLink to="/works">回到作品目录</RouterLink>
      </div>
    </div>

    <!-- 4. 正文详情：先讲作品与显示版本，再给真实访问与具体文件下载；不复读 summary -->
    <template v-else-if="work">
      <p
        v-if="refreshProblem"
        class="works-inline-notice is-warn"
        role="status"
      >
        {{ refreshProblem }}
      </p>

      <PaperSheet
        as="article"
        class="work-detail-sheet"
        paper="plain"
      >
        <header class="work-detail-header">
          <div class="work-detail-meta-row">
            <p class="work-meta-line">
              <time :datetime="work.content_date">{{
                formatNoteDate(work.content_date, { withYear: true })
              }}</time>
              <template v-if="work.category">
                <span aria-hidden="true">·</span>
                <span class="work-meta-kind">{{ work.category }}</span>
              </template>
              <template v-if="work.version_label">
                <span aria-hidden="true">·</span>
                <span class="work-meta-version">{{ work.version_label }}</span>
              </template>
              <template v-if="revisedAt">
                <span aria-hidden="true">·</span>
                <span class="work-detail-revised">修订于 {{ revisedAt }}</span>
              </template>
            </p>

            <button
              type="button"
              class="text-button works-refresh-btn"
              :aria-disabled="loading"
              :aria-busy="loading"
              @click="onReload"
            >
              {{ loading ? '正在重读…' : '重读' }}
            </button>
          </div>

          <h1 class="work-detail-title">{{ work.title }}</h1>
        </header>

        <WorkCoverFigure
          v-if="work.cover"
          :cover="work.cover"
          variant="detail"
        />

        <div class="work-detail-body">{{ work.body }}</div>

        <section
          v-if="hasOutcomes"
          class="work-outcomes"
          aria-labelledby="work-outcomes-heading"
        >
          <h2
            id="work-outcomes-heading"
            class="work-outcomes-heading"
          >
            访问与带走
          </h2>

          <div
            v-if="work.links.length > 0"
            class="work-links-block"
          >
            <h3 class="work-sub-heading">访问入口</h3>
            <ul class="work-links-list">
              <li
                v-for="link in work.links"
                :key="`${link.label}-${link.url}`"
                class="work-link-row"
              >
                <a
                  class="work-external-link"
                  :href="link.url"
                  target="_blank"
                  rel="noopener noreferrer"
                  >{{ link.label }}</a
                >
                <span class="work-link-meta">
                  <template v-if="isExternalWorkLink(link.url)">站外</template>
                  <template v-if="isExternalWorkLink(link.url) && workLinkHost(link.url)">
                    ·
                  </template>
                  <span v-if="workLinkHost(link.url)">{{ workLinkHost(link.url) }}</span>
                </span>
              </li>
            </ul>
          </div>

          <div
            v-if="work.files.length > 0"
            class="work-files-block"
          >
            <h3 class="work-sub-heading">公开文件</h3>
            <ul class="work-files-list">
              <li
                v-for="file in work.files"
                :key="file.id"
                class="work-file-row"
              >
                <div class="work-file-main">
                  <div class="work-file-info">
                    <span class="work-file-name">{{ file.filename }}</span>
                    <span class="work-file-meta">
                      {{ formatBytes(file.size_bytes) }} · SHA-256 {{ shortDigest(file.sha256) }}
                    </span>
                  </div>

                  <button
                    type="button"
                    class="work-file-download-btn"
                    :aria-disabled="downloadingId !== null"
                    :aria-busy="downloadingId === file.id"
                    @click="downloadFile(file)"
                  >
                    {{ downloadingId === file.id ? '正在准备…' : '下载' }}
                  </button>
                </div>

                <p
                  v-if="fileDone[file.id]"
                  class="work-file-status"
                  role="status"
                >
                  {{ fileDone[file.id] }}
                </p>

                <div
                  v-if="fileErrors[file.id]"
                  class="work-file-error"
                  role="alert"
                >
                  <span>{{ fileErrors[file.id]?.message }}</span>
                  <button
                    v-if="fileErrors[file.id]?.staleAsset"
                    type="button"
                    class="text-button work-file-reload-btn"
                    :aria-disabled="loading"
                    :aria-busy="loading"
                    @click="onReload"
                  >
                    {{ loading ? '正在重读…' : '重读作品' }}
                  </button>
                </div>
              </li>
            </ul>
          </div>
        </section>

        <p
          v-if="work.credits"
          class="work-detail-credits"
        >
          {{ work.credits }}
        </p>
      </PaperSheet>

      <footer class="work-detail-footer">
        <RouterLink to="/works">← 回到作品目录</RouterLink>
        <RouterLink to="/">回到首页</RouterLink>
      </footer>
    </template>
  </div>
</template>
