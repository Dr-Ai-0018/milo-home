import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import ChatView from './views/ChatView.vue'
import HomeView from './views/HomeView.vue'
import LetterView from './views/LetterView.vue'
import LettersView from './views/LettersView.vue'
import NoteDetailView from './views/NoteDetailView.vue'
import NotesView from './views/NotesView.vue'
import WorkDetailView from './views/WorkDetailView.vue'
import WorkTaskView from './views/WorkTaskView.vue'
import WorksView from './views/WorksView.vue'
import WorkView from './views/WorkView.vue'
import { startInk } from './theme/inks'
import { rememberScrollPosition, savedScrollPosition } from './utils/scroll'
import './styles/tokens.css'
import './styles/base.css'
import './styles/layout.css'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    {
      path: '/',
      component: HomeView,
      meta: { title: '米洛在家' },
    },
    {
      path: '/notes',
      component: NotesView,
      meta: { title: '家里近况 · 米洛在家' },
    },
    {
      path: '/notes/:id',
      component: NoteDetailView,
      meta: { title: '一条近况 · 米洛在家' },
    },
    {
      path: '/works',
      component: WorksView,
      meta: { title: '作品 · 米洛在家' },
    },
    {
      path: '/works/:id',
      component: WorkDetailView,
      meta: { title: '一件作品 · 米洛在家' },
    },
    ...(import.meta.env.DEV
      ? [
          {
            path: '/preview/home',
            component: () => import('./views/HomePreviewView.vue'),
            meta: { title: '首页设计预览 · 米洛在家' },
          },
          {
            path: '/preview/work-detail',
            component: () => import('./views/WorkDetailPreviewView.vue'),
            meta: { title: '交办详情样板预览 · 米洛在家' },
          },
        ]
      : []),
    {
      path: '/letters',
      component: LettersView,
      meta: { title: '书信 · 米洛在家', private: true },
    },
    {
      path: '/letters/:id',
      component: LetterView,
      meta: { title: '一封信 · 米洛在家', private: true },
    },
    {
      path: '/chat',
      component: ChatView,
      meta: { title: '传纸条 · 米洛在家', private: true },
    },
    {
      path: '/work',
      component: WorkView,
      meta: { title: '交办 · 米洛在家', private: true },
    },
    {
      path: '/work/:id',
      component: WorkTaskView,
      meta: { title: '一张交办单 · 米洛在家', private: true },
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/',
    },
  ],
  scrollBehavior(to, _from, saved) {
    if (saved) return saved
    // 聊天自己决定落在最新纸条还是页头。
    if (to.path === '/chat') return false
    const list =
      to.path === '/letters' || to.path === '/work' || to.path === '/notes' || to.path === '/works'
    const remembered = list ? savedScrollPosition(to.fullPath) : undefined
    return { top: remembered ?? 0 }
  },
})

router.beforeEach((_to, from) => {
  rememberScrollPosition(from.fullPath, scrollY)
})

// 站内跳转不清空私密状态：登录凭据在 cookie 里，清了也照样能点回来，只会让每次进出都重读一遍。
// 退出、登录失效或换了账号时由 usePrivateSession 自己清空。
router.afterEach((route) => {
  document.title = String(route.meta.title)
})

startInk()
createApp(App).use(router).mount('#app')
