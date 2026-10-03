import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import VueDevTools from 'vite-plugin-vue-devtools'

const version = readFileSync('../pyproject.toml', 'utf8').match(/^version = "(.+)"/m)?.[1] ?? 'dev'
let commit: string | undefined
try {
  commit = execFileSync('git', ['rev-parse', '--short', 'HEAD'], { cwd: '..', encoding: 'utf8' }).trim() || undefined
} catch { /* built from a source tarball */ }

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const serviceRoot = `/${(env.VITE_XUN_BASE_PATH || '').replace(/^\/+|\/+$/g, '')}`.replace(/^\/$/, '')
  const backend = env.VITE_XUN_BACKEND || 'http://127.0.0.1:18960'
  const proxyHeaders = { Authorization: `Bearer ${env.VITE_XUN_TOKEN || 'xun-dev'}` }

  return {
    base: './',
    define: {
      __XUN_VERSION__: JSON.stringify(commit ? `${version} (${commit})` : version),
    },
    plugins: [vue(), mode === 'development' && VueDevTools()].filter(Boolean),
    build: {
      outDir: '../src/xun/assets/web',
      emptyOutDir: true,
    },
    server: {
      proxy: {
        [`${serviceRoot}/api/sessions`]: { target: backend, headers: proxyHeaders },
        [`${serviceRoot}/session`]: { target: backend, headers: proxyHeaders, ws: true },
      },
    },
  }
})
