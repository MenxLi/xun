<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { AlertCircle, Check, File, LoaderCircle, X } from 'lucide-vue-next'

const props = defineProps<{
  files: string[]
  status: 'uploading' | 'complete' | 'failed'
  error?: string
}>()

const emit = defineEmits<{ dismiss: [] }>()

const { t } = useI18n()

// Completed notices dismiss themselves after a short grace period; failures
// stay until closed. Hovering pauses the countdown and its bar together.
const AUTO_DISMISS_MS = 5000
const paused = ref(false)
let remaining = AUTO_DISMISS_MS
let startedAt = 0
let timer: number | undefined

function clearTimer() {
  window.clearTimeout(timer)
  timer = undefined
}

watch(() => props.status, status => {
  clearTimer()
  paused.value = false
  if (status !== 'complete') return
  remaining = AUTO_DISMISS_MS
  startedAt = performance.now()
  timer = window.setTimeout(() => emit('dismiss'), remaining)
}, { immediate: true })

function pause() {
  if (timer === undefined) return
  paused.value = true
  remaining -= performance.now() - startedAt
  clearTimer()
}

function resume() {
  if (!paused.value) return
  paused.value = false
  startedAt = performance.now()
  timer = window.setTimeout(() => emit('dismiss'), remaining)
}

onBeforeUnmount(clearTimer)
</script>

<template>
  <Teleport to="body">
    <section class="upload-notice" :class="status" role="status" aria-live="polite" @mouseenter="pause" @mouseleave="resume">
      <header>
        <span class="upload-status-icon" :class="status" aria-hidden="true">
          <Check v-if="status === 'complete'" :size="16" />
          <AlertCircle v-else-if="status === 'failed'" :size="16" />
          <LoaderCircle v-else :size="16" />
        </span>
        <div class="upload-heading">
          <strong>{{ t(status === 'complete' ? 'upload.complete' : status === 'failed' ? 'upload.failed' : 'upload.uploading') }}</strong>
          <span>{{ t('upload.filesCount', { n: files.length }) }}</span>
        </div>
        <button class="upload-dismiss" :title="t('upload.dismiss')" @click="$emit('dismiss')"><X :size="17" /></button>
      </header>

      <div class="upload-notice-body">
        <div class="upload-file-list">
          <div v-for="(name, index) in files.slice(0, 3)" :key="`${name}-${index}`" class="upload-file-row">
            <File :size="14" aria-hidden="true" />
            <span class="upload-file-name" :title="name">{{ name }}</span>
          </div>
          <div v-if="files.length > 3" class="upload-file-more">{{ t('upload.moreFiles', { n: files.length - 3 }) }}</div>
        </div>
        <small v-if="error" class="upload-error">{{ error }}</small>
      </div>
      <div v-if="status === 'complete'" class="upload-countdown" :class="{ paused }" :style="{ animationDuration: `${AUTO_DISMISS_MS}ms` }" />
    </section>
  </Teleport>
</template>

<style scoped>
.upload-notice { position: fixed; right: 20px; bottom: 20px; width: min(350px, calc(100vw - 28px)); border: 1px solid var(--line-strong); border-radius: 7px; background: var(--paper); box-shadow: 0 12px 32px color-mix(in srgb, var(--shadow) 72%, transparent); overflow: hidden; z-index: 110; animation: upload-notice-in .2s ease-out; }
.upload-notice header { min-height: 54px; padding: 9px 9px 9px 13px; display: grid; grid-template-columns: 28px minmax(0, 1fr) 30px; gap: 9px; align-items: center; border-bottom: 1px solid color-mix(in srgb, var(--line) 78%, transparent); }
.upload-heading { min-width: 0; display: grid; gap: 3px; }
.upload-heading strong { color: var(--ink); font-size: 12px; font-weight: 700; }
.upload-heading span { color: var(--muted); font: 9px/1.2 'Fira Code', monospace; }
.upload-dismiss { width: 28px; height: 28px; padding: 0; border: 0; border-radius: 5px; display: grid; place-items: center; color: var(--muted); background: transparent; cursor: pointer; }
.upload-dismiss:hover { color: var(--ink); background: var(--hover); }
.upload-notice-body { padding: 5px 7px 7px; }
.upload-status-icon { width: 26px; height: 26px; display: grid; place-items: center; border-radius: 50%; color: var(--accent-dark); background: var(--accent-soft); }
.upload-status-icon.uploading svg { animation: upload-spin .8s linear infinite; }
.upload-status-icon.failed { color: var(--danger); background: color-mix(in srgb, var(--danger) 12%, var(--paper)); }
.upload-file-list { min-width: 0; }
.upload-file-row { min-width: 0; height: 32px; padding: 0 8px; display: grid; grid-template-columns: 16px minmax(0, 1fr); gap: 8px; align-items: center; }
.upload-file-row + .upload-file-row { border-top: 1px solid color-mix(in srgb, var(--line) 58%, transparent); }
.upload-file-row svg { color: var(--muted); }
.upload-file-name { overflow: hidden; color: var(--ink); font-size: 10px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.upload-file-more { height: 28px; padding: 0 8px 0 32px; display: flex; align-items: center; border-top: 1px solid color-mix(in srgb, var(--line) 58%, transparent); color: var(--muted); font: 9px/1 'Fira Code', monospace; }
.upload-error { display: block; margin: 6px 8px 3px; color: var(--danger); font-size: 10px; line-height: 1.4; overflow-wrap: anywhere; }
.upload-countdown { position: absolute; left: 0; bottom: 0; width: 100%; height: 2px; transform-origin: left; background: color-mix(in srgb, var(--accent) 45%, transparent); animation: upload-countdown linear forwards; }
.upload-countdown.paused { animation-play-state: paused; }

@keyframes upload-notice-in { from { opacity: 0; transform: translateY(8px); } }
@keyframes upload-spin { to { transform: rotate(360deg); } }
@keyframes upload-countdown { to { transform: scaleX(0); } }

@media (max-width: 600px) {
  .upload-notice { right: 10px; bottom: 10px; width: calc(100vw - 20px); }
}

@media (prefers-reduced-motion: reduce) {
  .upload-notice, .upload-status-icon.uploading svg { animation: none; }
  .upload-countdown { display: none; }
}
</style>