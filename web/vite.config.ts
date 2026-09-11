import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import VueDevTools from 'vite-plugin-vue-devtools'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const basePath = `/${(env.VITE_XUN_BASE_PATH || '').replace(/^\/+|\/+$/g, '')}`.replace(/^\/$/, '')
  const backend = env.VITE_XUN_BACKEND || 'http://127.0.0.1:18960'
  const proxyHeaders = { Authorization: `Bearer ${env.VITE_XUN_TOKEN || 'xun-dev'}` }

  return {
    base: './',
    plugins: [vue(), mode === 'development' && VueDevTools()].filter(Boolean),
    build: {
      outDir: '../src/xun/assets/web',
      emptyOutDir: true,
    },
    server: {
      proxy: {
        '/api/sessions': { target: backend, headers: proxyHeaders },
        [`${basePath}/api`]: { target: backend, headers: proxyHeaders },
        [`${basePath}/ws`]: { target: backend, headers: proxyHeaders, ws: true },
      },
    },
  }
})
