<script setup lang="ts">
import { AlertCircle, Check, File, LoaderCircle, X } from 'lucide-vue-next'

defineProps<{
  files: string[]
  status: 'uploading' | 'complete' | 'failed'
  error?: string
}>()

defineEmits<{ dismiss: [] }>()
</script>

<template>
  <Teleport to="body">
    <section class="upload-notice" :class="status" role="status" aria-live="polite">
      <header>
        <span class="upload-status-icon" :class="status" aria-hidden="true">
          <Check v-if="status === 'complete'" :size="16" />
          <AlertCircle v-else-if="status === 'failed'" :size="16" />
          <LoaderCircle v-else :size="16" />
        </span>
        <div class="upload-heading">
          <strong>{{ status === 'complete' ? 'Upload complete' : status === 'failed' ? 'Upload failed' : 'Uploading files' }}</strong>
          <span>{{ files.length }} {{ files.length === 1 ? 'file' : 'files' }}</span>
        </div>
        <button class="upload-dismiss" title="Dismiss upload status" @click="$emit('dismiss')"><X :size="17" /></button>
      </header>

      <div class="upload-notice-body">
        <div class="upload-file-list">
          <div v-for="(name, index) in files.slice(0, 3)" :key="`${name}-${index}`" class="upload-file-row">
            <File :size="14" aria-hidden="true" />
            <span class="upload-file-name" :title="name">{{ name }}</span>
          </div>
          <div v-if="files.length > 3" class="upload-file-more">+{{ files.length - 3 }} more {{ files.length - 3 === 1 ? 'file' : 'files' }}</div>
        </div>
        <small v-if="error" class="upload-error">{{ error }}</small>
      </div>
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
.upload-file-row { min-width: 0; height: 32px; padding: 0 8px; display: grid; grid-template-columns: 16px minmax(0, 1fr); gap: 8px; align-items: center; border-radius: 4px; }
.upload-file-row + .upload-file-row { border-top: 1px solid color-mix(in srgb, var(--line) 58%, transparent); }
.upload-file-row:hover { background: color-mix(in srgb, var(--hover) 70%, transparent); }
.upload-file-row svg { color: var(--muted); }
.upload-file-name { overflow: hidden; color: var(--ink); font-size: 10px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.upload-file-more { height: 28px; padding: 0 8px 0 32px; display: flex; align-items: center; border-top: 1px solid color-mix(in srgb, var(--line) 58%, transparent); color: var(--muted); font: 9px/1 'Fira Code', monospace; }
.upload-error { display: block; margin: 6px 8px 3px; color: var(--danger); font-size: 10px; line-height: 1.4; overflow-wrap: anywhere; }

@keyframes upload-notice-in { from { opacity: 0; transform: translateY(8px); } }
@keyframes upload-spin { to { transform: rotate(360deg); } }

@media (max-width: 600px) {
  .upload-notice { right: 10px; bottom: 10px; width: calc(100vw - 20px); }
}

@media (prefers-reduced-motion: reduce) {
  .upload-notice, .upload-status-icon.uploading svg { animation: none; }
}
</style>