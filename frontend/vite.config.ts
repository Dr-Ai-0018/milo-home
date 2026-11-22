import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'MILO_DEV_')

  return {
    plugins: [vue()],
    server: {
      strictPort: true,
      port: 7473,
      proxy: {
        '/api': {
          target: env.MILO_DEV_API_TARGET || 'http://127.0.0.1:7484',
        },
      },
    },
  }
})
