<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { FileQuestion, Maximize2, Minimize2, X } from 'lucide-vue-next'
import ResizeHandle from './ResizeHandle.vue'
import { api } from '../api'
import { previewKind } from '../preview'
import type { FileEntry } from '../types'

const props = defineProps<{ agentId: string; entry: FileEntry }>()
const emit = defineEmits<{ resize: [delta: number]; close: [] }>()

const kind = computed(() => previewKind(props.entry.media_type))
const contentUrl = computed(() => api.contentUrl(props.agentId, props.entry.path))

const text = ref('')
const loading = ref(false)
const error = ref('')
const fullscreen = ref(false)

watch(() => props.entry.path, () => {
  error.value = ''
  if (kind.value !== 'text') return
  loading.value = true
  text.value = ''
  api.textContent(props.agentId, props.entry.path)
    .then(value => { text.value = value })
    .catch(reason => { error.value = reason instanceof Error ? reason.message : 'Could not preview file' })
    .finally(() => { loading.value = false })
}, { immediate: true })
</script>

<template>
  <Teleport to="body" :disabled="!fullscreen">
    <section class="file-preview" :class="{ 'is-fullscreen': fullscreen }">
      <ResizeHandle v-if="!fullscreen" orientation="vertical" @drag="delta => emit('resize', delta)" />
      <header>
        <span>{{ entry.path }}</span>
        <div class="preview-actions">
          <button class="icon-button" :title="fullscreen ? 'Exit fullscreen' : 'Fullscreen'" :aria-pressed="fullscreen" @click="fullscreen = !fullscreen">
            <Minimize2 v-if="fullscreen" :size="14" />
            <Maximize2 v-else :size="14" />
          </button>
          <button class="icon-button" title="Close preview" @click="emit('close')"><X :size="15" /></button>
        </div>
      </header>

      <template v-if="kind === 'image'">
        <img v-show="!error" :src="contentUrl" :alt="entry.name" @error="error = 'Could not load image'">
        <div v-if="error" class="preview-error">{{ error }}</div>
      </template>

      <template v-else-if="kind === 'text'">
        <div v-if="error" class="preview-error">{{ error }}</div>
        <pre v-else>{{ loading ? 'Loading…' : text }}</pre>
      </template>

      <iframe v-else-if="kind === 'pdf'" class="preview-document" :src="contentUrl" :title="`Preview of ${entry.name}`" />

      <div v-else class="preview-unsupported">
        <FileQuestion :size="20" />
        <span>No preview for {{ entry.media_type || 'this file' }}</span>
      </div>
    </section>
  </Teleport>
</template>
