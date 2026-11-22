<script setup lang="ts">
import { computed, ref } from 'vue'
import type { PublicWorkSummary } from '../../api/client'
import PaperSheet from '../ink/PaperSheet.vue'
import WorkCoverFigure from '../works/WorkCoverFigure.vue'
import { usePublicNotesFocus } from '../../composables/usePublicNotesFocus'
import { formatNoteDate } from '../../utils/publicNotes'
import '../../styles/home-works.css'

const props = withDefaults(
  defineProps<{
    works?: readonly PublicWorkSummary[]
    hasLoaded?: boolean
    legacyMode?: boolean
    loading?: boolean
    problem?: string
    refreshProblem?: string
  }>(),
  {
    works: () => [],
    hasLoaded: false,
    legacyMode: false,
    loading: false,
    problem: '',
    refreshProblem: '',
  },
)

const emit = defineEmits<{
  reload: []
}>()

const sectionElement = ref<HTMLElement | null>(null)
usePublicNotesFocus(
  sectionElement,
  [() => props.loading, () => props.hasLoaded, () => props.problem, () => props.legacyMode],
  '#home-works-title',
)

const leadWork = computed(() => props.works[0] ?? null)
const secondaryWork = computed(() => props.works[1] ?? null)
const showCatalogLink = computed(
  () => !props.legacyMode && props.hasLoaded && props.works.length > 0,
)

function onReload() {
  if (!props.loading) emit('reload')
}
</script>

<template>
  <section
    ref="sectionElement"
    class="home-works-section"
    aria-labelledby="home-works-title"
    :aria-busy="loading"
  >
    <div class="home-works-heading">
      <div class="home-works-heading-copy">
        <h2
          id="home-works-title"
          tabindex="-1"
          class="home-section-eyebrow"
        >
          作品
        </h2>
        <p class="home-works-sub">
          {{ legacyMode ? '作品还没接到这里' : '明确选出来、可以直接访问或带走的公开成果' }}
        </p>
      </div>

      <div class="home-works-actions">
        <RouterLink
          v-if="showCatalogLink"
          class="home-works-all-link"
          to="/works"
          >全部作品</RouterLink
        >
        <button
          v-if="hasLoaded || legacyMode"
          type="button"
          class="text-button home-works-reload"
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
      class="home-works-notice is-warn"
      role="status"
    >
      {{ refreshProblem }}
    </p>

    <!-- 1. 旧后端列表 404：说明还没接到这里，隐藏详情入口，不捏造随站作品 -->
    <div
      v-if="legacyMode"
      class="home-works-state"
      role="status"
    >
      <p>作品还没接到这里，当前后端暂未开放公开作品接口。</p>
    </div>

    <!-- 2. 首次读取中 -->
    <div
      v-else-if="loading && !hasLoaded"
      class="home-works-state"
      role="status"
    >
      <p>正在读取公开作品…</p>
    </div>

    <!-- 3. 首次读取失败（503 / 500 / 网络）：不挡作息、近况与入口 -->
    <div
      v-else-if="problem && !hasLoaded"
      class="home-works-state is-error"
      role="alert"
    >
      <p>{{ problem }}</p>
      <button
        type="button"
        class="text-button home-works-retry"
        :aria-disabled="loading"
        :aria-busy="loading"
        @click="onReload"
      >
        {{ loading ? '正在重读…' : '重新读取' }}
      </button>
    </div>

    <!-- 4. 真实 200 空态：安静说明，不放合成或假入口 -->
    <div
      v-else-if="hasLoaded && works.length === 0"
      class="home-works-state"
      role="status"
    >
      <p>还没有公开作品。</p>
    </div>

    <!-- 5. 真实公开作品：最多两件，一主一辅；只有一件时自然独呈 -->
    <div
      v-else
      class="home-works-stack"
    >
      <PaperSheet
        v-if="leadWork"
        as="article"
        class="home-lead-work"
        :class="{ 'has-cover': !!leadWork.cover }"
        paper="plain"
      >
        <div class="home-lead-work-body">
          <p class="work-meta-line">
            <time :datetime="leadWork.content_date">{{
              formatNoteDate(leadWork.content_date, { withYear: true })
            }}</time>
            <template v-if="leadWork.category">
              <span aria-hidden="true">·</span>
              <span class="work-meta-kind">{{ leadWork.category }}</span>
            </template>
            <template v-if="leadWork.version_label">
              <span aria-hidden="true">·</span>
              <span class="work-meta-version">{{ leadWork.version_label }}</span>
            </template>
          </p>

          <h3 class="home-lead-work-title">
            <RouterLink
              class="work-title-link"
              :to="`/works/${leadWork.id}`"
              >{{ leadWork.title }}</RouterLink
            >
          </h3>

          <p class="home-lead-work-summary">{{ leadWork.summary }}</p>

          <p class="home-work-readmore">
            <RouterLink :to="`/works/${leadWork.id}`">看看这件作品</RouterLink>
          </p>
        </div>

        <WorkCoverFigure
          v-if="leadWork.cover"
          :cover="leadWork.cover"
          variant="home"
        />
      </PaperSheet>

      <article
        v-if="secondaryWork"
        class="home-secondary-work"
      >
        <p class="home-secondary-label">再看一件</p>
        <div
          class="home-secondary-row"
          :class="{ 'has-cover': !!secondaryWork.cover }"
        >
          <div class="home-secondary-copy">
            <p class="work-meta-line">
              <time :datetime="secondaryWork.content_date">{{
                formatNoteDate(secondaryWork.content_date, { withYear: true })
              }}</time>
              <template v-if="secondaryWork.category">
                <span aria-hidden="true">·</span>
                <span class="work-meta-kind">{{ secondaryWork.category }}</span>
              </template>
              <template v-if="secondaryWork.version_label">
                <span aria-hidden="true">·</span>
                <span class="work-meta-version">{{ secondaryWork.version_label }}</span>
              </template>
            </p>
            <h3 class="home-secondary-title">
              <RouterLink
                class="work-title-link"
                :to="`/works/${secondaryWork.id}`"
                >{{ secondaryWork.title }}</RouterLink
              >
            </h3>
            <p class="home-secondary-summary">{{ secondaryWork.summary }}</p>
          </div>

          <WorkCoverFigure
            v-if="secondaryWork.cover"
            :cover="secondaryWork.cover"
            variant="catalog"
          />
        </div>
      </article>
    </div>
  </section>
</template>
