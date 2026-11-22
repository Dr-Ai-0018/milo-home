<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import InkScribble from './components/ink/InkScribble.vue'
import { usePrivateSession } from './composables/usePrivateSession'

// 主题预览只在开发环境加载，生产构建不包含它。
const InkPreviewSwitcher = import.meta.env.DEV
  ? defineAsyncComponent(() => import('./components/ink/InkPreviewSwitcher.vue'))
  : null

// 单封信、单件交办与单件作品是独立路由，按严格路径边界标为当前，避免 /works 误标 /work。
const route = useRoute()
const isWorksRoute = computed(() => route.path === '/works' || route.path.startsWith('/works/'))
const isWorkRoute = computed(() => route.path === '/work' || route.path.startsWith('/work/'))
const isLettersRoute = computed(
  () => route.path === '/letters' || route.path.startsWith('/letters/'),
)
// 交办只给有工作权限的人看；登录状态还没读过时也不显示。
const { homeAccess, refreshHomeAccess } = usePrivateSession()

// 首页没有私密页面的登录门；导航只查交办权限，不预读书信。
watch(
  () => route.matched[0]?.path,
  (path) => {
    if (path === '/') void refreshHomeAccess()
  },
  { immediate: true },
)

function recheckVisibleHome() {
  if (!document.hidden && route.matched[0]?.path === '/') void refreshHomeAccess()
}

onMounted(() => document.addEventListener('visibilitychange', recheckVisibleHome))
onBeforeUnmount(() => document.removeEventListener('visibilitychange', recheckVisibleHome))

function skipToContent() {
  const content = document.querySelector<HTMLElement>('#main-content')
  content?.focus()
  content?.scrollIntoView()
}

// 换页后把焦点交给正文，但不滚动：滚到哪里由路由的位置记忆和各页自己决定。
useRouter().afterEach(async () => {
  await nextTick()
  document.querySelector<HTMLElement>('#main-content')?.focus({ preventScroll: true })
})
</script>

<template>
  <a
    class="skip-link"
    href="#main-content"
    @click.prevent="skipToContent"
    >跳到正文</a
  >
  <div class="site-shell">
    <header class="site-header">
      <RouterLink
        class="wordmark overprint"
        to="/"
        aria-label="米洛在家，首页"
        >米洛在家</RouterLink
      >
      <nav
        class="site-nav"
        aria-label="主要导航"
      >
        <RouterLink
          to="/"
          exact-active-class="is-current"
          ><InkScribble>来坐坐</InkScribble></RouterLink
        >
        <RouterLink
          to="/works"
          :class="{ 'is-current': isWorksRoute }"
          :aria-current="isWorksRoute ? 'page' : undefined"
          ><InkScribble>作品</InkScribble></RouterLink
        >
        <RouterLink
          to="/letters"
          :class="{ 'is-current': isLettersRoute }"
          :aria-current="isLettersRoute ? 'page' : undefined"
          ><InkScribble>书信</InkScribble></RouterLink
        >
        <RouterLink
          to="/chat"
          active-class="is-current"
          ><InkScribble>传纸条</InkScribble></RouterLink
        >
        <RouterLink
          v-if="homeAccess === 'granted'"
          to="/work"
          :class="{ 'is-current': isWorkRoute }"
          :aria-current="isWorkRoute ? 'page' : undefined"
          ><InkScribble>交办</InkScribble></RouterLink
        >
      </nav>
    </header>
    <main
      id="main-content"
      tabindex="-1"
    >
      <RouterView />
    </main>
    <footer class="site-footer">
      <span>米洛的小家 · 慢慢添，认真住。</span>
      <span>时间都按北京时间</span>
    </footer>
  </div>
  <component
    :is="InkPreviewSwitcher"
    v-if="InkPreviewSwitcher"
  />
</template>
