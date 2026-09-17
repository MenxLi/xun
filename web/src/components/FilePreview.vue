<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { FileQuestion, Maximize2, Minimize2, X } from 'lucide-vue-next'
import ResizeHandle from './ResizeHandle.vue'
import { api } from '../api'
import { highlightFile } from '../highlight'
import { previewKind } from '../preview'
import type { FileInfo } from '../types'

const props = defineProps<{ agentId: string; entry: FileInfo }>()
const emit = defineEmits<{ resize: [delta: number]; close: [] }>()
const { t } = useI18n()

const kind = computed(() => previewKind(props.entry.media_type))
const contentUrl = computed(() => api.contentUrl(props.agentId, props.entry.path))

const text = ref('')
const loading = ref(false)
const error = ref('')
const fullscreen = ref(false)
const highlighted = computed(() => highlightFile(props.entry.path, text.value))

watch(() => props.entry.path, () => {
  error.value = ''
  if (kind.value !== 'text') return
  loading.value = true
  text.value = ''
  api.textContent(props.agentId, props.entry.path)
    .then(value => { text.value = value })
    .catch(reason => { error.value = reason instanceof Error ? reason.message : t('preview.textError') })
    .finally(() => { loading.value = false })
}, { immediate: true })
</script>

<template>
  <section class="file-preview" :class="{ 'is-fullscreen': fullscreen }">
    <ResizeHandle v-if="!fullscreen" orientation="vertical" @drag="delta => emit('resize', delta)" />
    <header>
      <span>{{ entry.path }}</span>
      <div class="preview-actions">
        <button class="icon-button" :title="fullscreen ? t('preview.exitFullscreen') : t('preview.fullscreen')" :aria-pressed="fullscreen" @click="fullscreen = !fullscreen">
          <Minimize2 v-if="fullscreen" :size="14" />
          <Maximize2 v-else :size="14" />
        </button>
        <button class="icon-button" :title="t('preview.closePreview')" @click="emit('close')"><X :size="15" /></button>
      </div>
    </header>

    <template v-if="kind === 'image'">
      <img v-show="!error" :src="contentUrl" :alt="entry.name" @error="error = t('preview.imageError')">
      <div v-if="error" class="preview-error">{{ error }}</div>
    </template>

    <template v-else-if="kind === 'text'">
      <div v-if="error" class="preview-error">{{ error }}</div>
      <pre v-else-if="highlighted" v-html="highlighted" />
      <pre v-else>{{ loading ? t('preview.loading') : text }}</pre>
    </template>

    <iframe v-else-if="kind === 'pdf'" class="preview-document" :src="contentUrl" :title="t('preview.previewOf', { name: entry.name })" />

    <div v-else class="preview-unsupported">
      <FileQuestion :size="20" />
      <span>{{ t('preview.noPreview', { type: entry.media_type || t('preview.noPreviewFallback') }) }}</span>
    </div>
  </section>
</template>
