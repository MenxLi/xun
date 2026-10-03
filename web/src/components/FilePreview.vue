<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { BookOpenText, CodeXml, FileQuestion, Maximize2, Minimize2, Pencil, RotateCcw, Save, X } from 'lucide-vue-next'
import ResizeHandle from './ResizeHandle.vue'
import MarkdownText from './MarkdownText.vue'
import { api, errorMessage, writeConflict } from '../api'
import { highlightFile } from '../highlight'
import { previewKind, renderKind } from '../preview'
import type { FileInfo, SavedFile } from '../types'

type PreviewView = 'render' | 'source' | 'edit'

const props = defineProps<{ agentId: string; entry: FileInfo; openInEdit?: boolean }>()
const emit = defineEmits<{
  resize: [delta: number]
  close: []
  changed: [saved: SavedFile]
  dirtyChange: [dirty: boolean]
}>()
const { t } = useI18n()

const kind = computed(() => previewKind(props.entry))
const render = computed(() => renderKind(props.entry.media_type, props.entry.path))
const contentUrl = computed(() => api.contentUrl(props.agentId, props.entry.path))
const dirUrl = computed(() => new URL('./', new URL(contentUrl.value, location.origin)).href)

const text = ref('')
const buffer = ref('')
const loading = ref(false)
const error = ref('')
const fullscreen = ref(false)
const view = ref<PreviewView>('render')
const savedAt = ref(0)
const saving = ref(false)
const saveError = ref('')
const conflict = ref<'stale' | 'exists' | null>(null)
const editor = ref<HTMLTextAreaElement>()

const dirty = computed(() => buffer.value !== text.value)
const canEdit = computed(() => kind.value === 'text' && !error.value && !loading.value && fullscreen.value)
const highlighted = computed(() => highlightFile(props.entry.path, buffer.value))
const documentUrl = computed(() => `${contentUrl.value}${contentUrl.value.includes('?') ? '&' : '?'}v=${savedAt.value}`)
const saveTitle = computed(() => saving.value ? t('preview.saving') : t('preview.save'))
const DRAFT_GUARD = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self' data:; script-src 'none'">`
const liveDraft = computed(() => `${DRAFT_GUARD}<base href="${dirUrl.value}">${buffer.value}`)

// except the modified_at our own save echoes back through the parent
watch([() => props.entry.path, () => props.entry.modified_at], ([path, modifiedAt], previous) => {
  if (previous && previous[0] === path && modifiedAt === savedAt.value) return
  error.value = ''
  saveError.value = ''
  conflict.value = null
  saving.value = false
  savedAt.value = modifiedAt
  text.value = ''
  buffer.value = ''
  view.value = kind.value === 'text' ? (props.openInEdit ? 'edit' : render.value ? 'render' : 'source') : 'render'
  fullscreen.value = view.value === 'edit'
  if (props.openInEdit) return  // a staged new file has nothing on disk to read yet
  if (kind.value !== 'text') return
  loading.value = true
  api.textContent(props.agentId, path)
    .then(value => { text.value = value; buffer.value = value })
    .catch(reason => { error.value = errorMessage(reason, t('preview.textError')) })
    .finally(() => { loading.value = false })
}, { immediate: true })

watch(fullscreen, active => {
  if (!active && view.value === 'edit') view.value = 'source'
})

watch(view, mode => {
  if (mode === 'edit') void nextTick(() => editor.value?.focus())
}, { immediate: true })

watch(dirty, value => emit('dirtyChange', value))

function enterEdit() {
  if (kind.value !== 'text' || error.value || loading.value) return
  fullscreen.value = true
  view.value = 'edit'
}

function onContentDblClick(event: MouseEvent) {
  if ((event.target as Element | null)?.closest?.('a, button')) return
  enterEdit()
}

function onDocumentLoad(event: Event) {
  const frame = event.target as HTMLIFrameElement
  try {
    frame.contentDocument?.addEventListener('dblclick', onContentDblClick)
  } catch {
    // cross-origin
  }
}

async function write(force: boolean): Promise<'saved' | 'conflict' | 'error'> {
  try {
    const saved = await api.writeText(props.agentId, props.entry.path, buffer.value, force ? null : savedAt.value)
    text.value = buffer.value
    savedAt.value = saved.modified_at
    conflict.value = null
    emit('changed', saved)
    return 'saved'
  } catch (reason) {
    const refusal = writeConflict(reason)
    if (refusal) conflict.value = refusal
    else saveError.value = errorMessage(reason, t('preview.saveFailed'))
    return refusal ? 'conflict' : 'error'
  }
}

async function matchesDisk(): Promise<boolean> {
  return await api.textContent(props.agentId, props.entry.path).catch(() => null) === buffer.value
}

// a conflict over unchanged bytes is not worth flagging: force it silently
async function save(force = false) {
  if (saving.value || !dirty.value) return
  saving.value = true
  saveError.value = ''
  try {
    if (await write(force) === 'conflict' && !force && await matchesDisk()) await write(true)
  } finally {
    saving.value = false
  }
}

async function reload() {
  saveError.value = ''
  conflict.value = null
  try {
    const [info, fresh] = await Promise.all([
      api.fileInfo(props.agentId, props.entry.path),
      api.textContent(props.agentId, props.entry.path),
    ])
    text.value = fresh
    buffer.value = fresh
    savedAt.value = info.modified_at
    if (view.value === 'edit') view.value = render.value ? 'render' : 'source'
    emit('changed', { path: info.path, size: info.size ?? 0, modified_at: info.modified_at })
  } catch (reason) {
    saveError.value = errorMessage(reason, t('preview.reloadFailed'))
  }
}

function discard() {
  buffer.value = text.value
  saveError.value = ''
}

function onEditorKeydown(event: KeyboardEvent) {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
    event.preventDefault()
    void save()
    return
  }
  if (event.key !== 'Tab') return
  event.preventDefault()
  const area = event.target as HTMLTextAreaElement
  const start = area.selectionStart
  buffer.value = area.value.slice(0, start) + '  ' + area.value.slice(area.selectionEnd)
  void nextTick(() => area.setSelectionRange(start + 2, start + 2))
}
</script>

<template>
  <section class="file-preview" :class="{ 'is-fullscreen': fullscreen }">
    <ResizeHandle v-if="!fullscreen" orientation="vertical" @drag="delta => emit('resize', delta)" />
    <header>
      <span>{{ entry.path }}<i v-if="dirty" class="dirty-mark" :title="t('preview.unsaved')"></i></span>
      <div class="preview-actions">
        <div v-if="kind === 'text' && !error" class="render-toggle" role="group" :aria-label="t('preview.view')">
          <button v-if="render" :class="{ active: view === 'render' }" :title="t('preview.render')" :aria-pressed="view === 'render'" @click="view = 'render'">
            <BookOpenText :size="13" />
          </button>
          <button :class="{ active: view === 'source' }" :title="t('preview.showSource')" :aria-pressed="view === 'source'" @click="view = 'source'">
            <CodeXml :size="13" />
          </button>
          <button v-if="canEdit" :class="{ active: view === 'edit' }" :title="t('preview.edit')" :aria-pressed="view === 'edit'" @click="view = 'edit'">
            <Pencil :size="13" />
          </button>
        </div>
        <template v-if="view === 'edit'">
          <button class="icon-button" :title="saveTitle" :disabled="!dirty || saving" @click="save()">
            <Save :size="14" />
          </button>
          <button class="icon-button" :title="t('preview.discard')" :disabled="!dirty || saving" @click="discard">
            <RotateCcw :size="14" />
          </button>
        </template>
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
      <template v-else>
        <div v-if="conflict" class="preview-banner">
          <span>{{ t(conflict === 'exists' ? 'preview.existsBody' : 'preview.conflictBody', { name: entry.name }) }}</span>
          <button class="banner-button" @click="reload">{{ t('preview.reloadFile') }}</button>
          <button class="banner-button danger" :disabled="saving" @click="save(true)">{{ t('preview.forceOverwrite') }}</button>
        </div>
        <div v-else-if="saveError" class="preview-banner error">{{ saveError }}</div>
        <textarea
          v-if="view === 'edit'"
          ref="editor"
          v-model="buffer"
          class="preview-editor"
          spellcheck="false"
          :aria-label="t('preview.editOf', { name: entry.name })"
          @keydown="onEditorKeydown"
        />
        <template v-else-if="render && view === 'render'">
          <MarkdownText v-if="render === 'markdown'" class="preview-rendered" :content="buffer" :enabled="!loading" :base-href="dirUrl" @dblclick="onContentDblClick" />
          <iframe v-else-if="dirty" key="draft" class="preview-document" :srcdoc="liveDraft" sandbox="allow-same-origin" :title="t('preview.previewOf', { name: entry.name })" @load="onDocumentLoad" />
          <iframe v-else key="disk" class="preview-document" :src="documentUrl" sandbox="allow-same-origin" :title="t('preview.previewOf', { name: entry.name })" @load="onDocumentLoad" />
        </template>
        <pre v-else-if="highlighted" v-html="highlighted" @dblclick="onContentDblClick" />
        <pre v-else @dblclick="onContentDblClick">{{ loading ? t('preview.loading') : buffer }}</pre>
      </template>
    </template>

    <iframe v-else-if="kind === 'pdf'" class="preview-document" :src="documentUrl" :title="t('preview.previewOf', { name: entry.name })" />

    <div v-else class="preview-unsupported">
      <FileQuestion :size="20" />
      <span>{{ t('preview.noPreview', { type: entry.media_type || t('preview.noPreviewFallback') }) }}</span>
    </div>
  </section>
</template>
