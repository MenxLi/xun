<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { ComponentPublicInstance } from 'vue'
import { useI18n } from 'vue-i18n'
import { ArrowLeft, Download, File, Folder, FolderArchive, FolderPlus, Info, MoreHorizontal, Pencil, RefreshCw, Trash2, Upload, X } from 'lucide-vue-next'
import FilePreview from './FilePreview.vue'
import AppDialog from './AppDialog.vue'
import ResizeHandle from './ResizeHandle.vue'
import UploadNotice from './UploadNotice.vue'
import { api } from '../api'
import type { AgentInfo, FileEntry, FileInfo } from '../types'
import { useSettingsStore } from '../stores/settings'
import type { BrowserState } from '../stores/sessionBuffers'

const props = defineProps<{ agents: AgentInfo[]; agentId: string; available: boolean | null; sessionKey: string }>()
const state = defineModel<BrowserState>('state', { required: true })
const emit = defineEmits<{ close: [] }>()

const settings = useSettingsStore()
const { t } = useI18n()

const path = computed({
  get: () => state.value.path,
  set: value => { state.value.path = value },
})
const entries = ref<FileEntry[]>([])
const previewEntry = ref<FileInfo | null>(null)
const infoEntry = ref<FileInfo | null>(null)
const dialog = ref<'delete' | null>(null)
const dialogEntry = ref<FileEntry | null>(null)
const dialogError = ref('')
const dialogBusy = ref(false)
const inlineAction = ref<'create' | 'move' | null>(null)
const inlineEntry = ref<FileEntry | null>(null)
const inlineValue = ref('')
const inlineBusy = ref(false)
const inlineError = ref('')
const inlineInput = ref<HTMLInputElement>()
const loading = ref(false)
const uploading = ref(false)
const dragActive = ref(false)
const activeMenu = ref<'toolbar' | 'entry' | null>(null)
const activeEntry = ref<FileEntry | null>(null)
const menuPosition = ref({ top: '0px', left: '0px' })
const error = ref('')
const fileInput = ref<HTMLInputElement>()
const uploadNotice = ref<{
  files: string[]
  status: 'uploading' | 'complete' | 'failed'
  error?: string
} | null>(null)
let dragDepth = 0
let listingRequest = 0
let metadataRequest = 0
const currentAgent = computed(() => props.agents.find(agent => agent.identifier === props.agentId))
const parentPath = computed(() => path.value.split('/').slice(0, -1).join('/'))

function archiveName(path: string) {
  return `${path.split('/').pop() || 'workspace'}.zip`
}

// Resolve an edit value: a leading / is workspace-root-relative, anything
// else is relative to the folder being browsed. Collapses ./.. segments.
// Returns null when the result would climb out of the workspace root.
function resolveRelative(value: string): string | null {
  const joined = value.startsWith('/') || !path.value ? value : `${path.value}/${value}`
  const segments: string[] = []
  for (const segment of joined.split('/')) {
    if (!segment || segment === '.') continue
    if (segment === '..') {
      if (!segments.length) return null
      segments.pop()
      continue
    }
    segments.push(segment)
  }
  return segments.join('/')
}

function toggleMenu(kind: 'toolbar' | 'entry', event: MouseEvent, entry?: FileEntry) {
  if (activeMenu.value === kind && (kind === 'toolbar' || activeEntry.value?.path === entry?.path)) {
    closeMenu()
    return
  }
  const button = event.currentTarget as HTMLElement
  const rect = button.getBoundingClientRect()
  const margin = 6
  const width = 176
  const height = kind === 'entry' ? 138 : 106
  const top = rect.bottom + 4 + height <= window.innerHeight - margin
    ? rect.bottom + 4
    : rect.top - height - 4
  menuPosition.value = {
    top: `${Math.max(margin, Math.min(top, window.innerHeight - height - margin))}px`,
    left: `${Math.max(margin, Math.min(rect.right - width, window.innerWidth - width - margin))}px`,
  }
  activeMenu.value = kind
  activeEntry.value = entry ?? null
}

function closeMenu() {
  activeMenu.value = null
  activeEntry.value = null
}

onMounted(() => {
  window.addEventListener('resize', closeMenu)
  document.addEventListener('click', closeMenu)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', closeMenu)
  document.removeEventListener('click', closeMenu)
})

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value))

function resizeWidth(delta: number) {
  settings.filesWidth = clamp(settings.filesWidth - delta, 220, 640)
}

function resizePreview(delta: number) {
  settings.previewHeight = clamp(settings.previewHeight - delta, 120, window.innerHeight - 220)
}

function setPreview(entry: FileInfo | null) {
  previewEntry.value = entry
  state.value.previewPath = entry?.path ?? null
}

async function restoreBrowserState() {
  const sessionKey = props.sessionKey
  const agentId = props.agentId
  const previewPath = state.value.previewPath
  metadataRequest += 1
  closeMenu()
  cancelInlineEdit(true)
  previewEntry.value = null
  infoEntry.value = null
  await refresh()
  if (!previewPath || !props.available || !agentId || sessionKey !== props.sessionKey || agentId !== props.agentId) return
  const request = ++metadataRequest
  try {
    const info = await api.fileInfo(agentId, previewPath)
    if (request === metadataRequest && sessionKey === props.sessionKey && agentId === props.agentId) setPreview(info)
  } catch {
    // The preview may have been removed while this session was inactive.
  }
}

watch([() => props.sessionKey, () => props.agentId, () => props.available],
  ([sessionKey, agentId], [previousSessionKey, previousAgentId]) => {
    if (sessionKey === previousSessionKey && agentId !== previousAgentId) {
      state.value.path = ''
      state.value.previewPath = null
    }
    void restoreBrowserState()
  },
  { immediate: true },
)

async function refresh() {
  const request = ++listingRequest
  if (!props.available || !props.agentId) {
    entries.value = []
    error.value = ''
    return
  }
  loading.value = true
  error.value = ''
  try {
    const listing = await api.files(props.agentId, path.value)
    if (request === listingRequest) entries.value = listing.entries
  } catch (reason) {
    if (request === listingRequest) error.value = reason instanceof Error ? reason.message : t('files.loadError')
  } finally {
    if (request === listingRequest) loading.value = false
  }
}

async function open(entry: FileEntry) {
  if (entry.kind === 'directory') {
    cancelInlineEdit(true)
    path.value = entry.path
    setPreview(null)
    void refresh()
  } else {
    const request = ++metadataRequest
    try {
      const info = await api.fileInfo(props.agentId, entry.path)
      if (request === metadataRequest) setPreview(info)
    } catch (reason) {
      if (request === metadataRequest) error.value = reason instanceof Error ? reason.message : t('files.inspectFileError')
    }
  }
}

async function showInfo(entry: FileEntry) {
  closeMenu()
  const request = ++metadataRequest
  error.value = ''
  try {
    const info = await api.fileInfo(props.agentId, entry.path)
    if (request === metadataRequest) infoEntry.value = info
  } catch (reason) {
    if (request === metadataRequest) error.value = reason instanceof Error ? reason.message : t('files.inspectPathError')
  }
}

function formatSize(size: number | null) {
  if (size === null) return '—'
  return new Intl.NumberFormat(undefined, { style: 'unit', unit: 'byte', unitDisplay: 'short' }).format(size)
}

function formatModified(timestamp: number) {
  return new Date(timestamp * 1000).toLocaleString()
}

async function upload(files: FileList | null) {
  if (!props.available || !props.agentId || !files?.length || uploading.value) return
  const selectedFiles = Array.from(files)
  uploading.value = true
  error.value = ''
  uploadNotice.value = {
    files: selectedFiles.map(file => file.name),
    status: 'uploading',
  }
  try {
    await api.upload(props.agentId, path.value, selectedFiles)
    if (uploadNotice.value) uploadNotice.value.status = 'complete'
    await refresh()
  } catch (reason) {
    const message = reason instanceof Error ? reason.message : t('files.uploadFailed')
    error.value = message
    if (uploadNotice.value) {
      uploadNotice.value.status = 'failed'
      uploadNotice.value.error = message
    }
  } finally {
    uploading.value = false
    if (fileInput.value) fileInput.value.value = ''
  }
}

function dragEnter(event: DragEvent) {
  if (!props.available || !props.agentId || !event.dataTransfer?.types.includes('Files')) return
  dragDepth += 1
  dragActive.value = true
}

function dragLeave() {
  dragDepth = Math.max(0, dragDepth - 1)
  if (!dragDepth) dragActive.value = false
}

function dropFiles(event: DragEvent) {
  dragDepth = 0
  dragActive.value = false
  void upload(event.dataTransfer?.files ?? null)
}

function closeDialog() {
  if (dialogBusy.value) return
  dialog.value = null
  dialogEntry.value = null
  dialogError.value = ''
}

function openDeleteDialog(entry: FileEntry) {
  closeMenu()
  dialogEntry.value = entry
  dialogError.value = ''
  dialog.value = 'delete'
}

async function submitDelete() {
  const entry = dialogEntry.value
  if (!entry) return
  dialogBusy.value = true
  dialogError.value = ''
  try {
    await api.remove(props.agentId, entry.path)
    if (previewEntry.value?.path === entry.path) setPreview(null)
    dialogBusy.value = false
    closeDialog()
    await refresh()
  } catch (reason) {
    dialogError.value = reason instanceof Error ? reason.message : t('files.deleteFailed')
  } finally {
    dialogBusy.value = false
  }
}

function cancelInlineEdit(force = false) {
  if (inlineBusy.value && !force) return
  inlineAction.value = null
  inlineEntry.value = null
  inlineValue.value = ''
  inlineError.value = ''
}

function setInlineInput(element: Element | ComponentPublicInstance | null) {
  inlineInput.value = element instanceof HTMLInputElement ? element : undefined
}

function startInlineEdit(action: 'create' | 'move', entry: FileEntry | null = null) {
  closeMenu()
  inlineAction.value = action
  inlineEntry.value = entry
  inlineValue.value = action === 'create' ? t('files.newFolder') : entry?.name ?? ''
  inlineError.value = ''
  error.value = ''
  void nextTick(() => {
    const input = inlineInput.value
    if (!input) return
    input.focus()
    const extension = action === 'move' && entry?.kind === 'file' ? input.value.lastIndexOf('.') : -1
    input.setSelectionRange(0, extension > 0 ? extension : input.value.length)
  })
}

async function submitInlineEdit() {
  const action = inlineAction.value
  const entry = inlineEntry.value
  const value = inlineValue.value.trim()
  if (!action || inlineBusy.value) return
  if (!value) {
    cancelInlineEdit()
    return
  }

  const target = resolveRelative(value)
  if (action === 'move' && (target === null || target === entry?.path)) {
    cancelInlineEdit()
    return
  }
  if (target === null || !target) {
    inlineError.value = target === null ? t('files.pathEscapes') : t('files.pathRoot')
    error.value = inlineError.value
    void nextTick(() => inlineInput.value?.focus())
    return
  }

  inlineBusy.value = true
  inlineError.value = ''
  error.value = ''
  try {
    if (action === 'create') await api.createDirectory(props.agentId, target)
    if (action === 'move' && entry) {
      await api.move(props.agentId, entry.path, target)
      if (previewEntry.value?.path === entry.path) setPreview(null)
    }
    inlineBusy.value = false
    cancelInlineEdit()
    await refresh()
  } catch (reason) {
    const fallback = action === 'create' ? t('files.createFolderFailed') : t('files.moveFailed')
    inlineError.value = reason instanceof Error ? reason.message : fallback
    error.value = inlineError.value
    void nextTick(() => inlineInput.value?.focus())
  } finally {
    inlineBusy.value = false
  }
}

function goUp() {
  cancelInlineEdit(true)
  path.value = parentPath.value
  setPreview(null)
  void refresh()
}

</script>

<template>
  <aside class="file-browser" :class="{ 'drag-active': dragActive }" :style="{ width: `${settings.filesWidth}px` }" @click="activeMenu = null" @keydown.esc="activeMenu = null" @dragenter.prevent="dragEnter" @dragover.prevent @dragleave.prevent="dragLeave" @drop.prevent="dropFiles">
    <ResizeHandle orientation="horizontal" @drag="resizeWidth" @reset="settings.filesWidth = 310" />
    <header class="file-header">
      <div>
        <span class="eyebrow">{{ t('files.workspace') }}</span>
        <strong>{{ currentAgent?.name || t('files.files') }}</strong>
      </div>
      <button class="icon-button mobile-close" :title="t('files.closeFiles')" @click="emit('close')"><X :size="18" /></button>
    </header>

    <div class="file-toolbar">
      <button class="icon-button" :title="t('files.parentFolder')" :disabled="!available || !agentId || !path" @click="goUp"><ArrowLeft :size="16" /></button>
      <div class="crumb" :title="path || currentAgent?.workdir">{{ path || '/' }}</div>
      <button class="icon-button" :title="t('files.refresh')" :disabled="!available || !agentId" @click="refresh"><RefreshCw :size="16" :class="{ spinning: loading }" /></button>
      <div class="file-menu-wrap" @click.stop>
        <button class="icon-button" :title="t('files.fileActions')" :disabled="!available || !agentId" :aria-expanded="activeMenu === 'toolbar'" @click="toggleMenu('toolbar', $event)"><MoreHorizontal :size="17" /></button>
      </div>
      <input ref="fileInput" hidden type="file" multiple @change="upload(($event.target as HTMLInputElement).files)">
    </div>

    <div v-if="error" class="file-error">{{ error }}</div>
    <div class="file-list" :aria-busy="loading || uploading" @scroll="closeMenu">
      <div v-if="available === false" class="file-empty">{{ t('files.accessDisabled') }}</div>
      <div v-else-if="available && agentId && !loading && !entries.length && inlineAction !== 'create'" class="file-empty">{{ t('files.emptyFolder') }}</div>
      <div v-if="inlineAction === 'create'" class="file-row editing">
        <div class="file-name inline-name">
          <Folder :size="16" />
          <input :ref="setInlineInput" v-model="inlineValue" :disabled="inlineBusy" :aria-invalid="!!inlineError" :title="inlineError || t('files.pathHint')" @input="inlineError = ''; error = ''" @keydown.enter.prevent="submitInlineEdit" @keydown.esc.prevent.stop="cancelInlineEdit()" @blur="submitInlineEdit">
        </div>
      </div>
      <div v-for="entry in entries" :key="entry.path" class="file-row">
        <div v-if="inlineAction === 'move' && inlineEntry?.path === entry.path" class="file-name inline-name">
          <Folder v-if="entry.kind === 'directory'" :size="16" />
          <File v-else :size="16" />
          <input :ref="setInlineInput" v-model="inlineValue" :disabled="inlineBusy" :aria-invalid="!!inlineError" :title="inlineError || t('files.pathHint')" @input="inlineError = ''; error = ''" @keydown.enter.prevent="submitInlineEdit" @keydown.esc.prevent.stop="cancelInlineEdit()" @blur="submitInlineEdit">
        </div>
        <button v-else class="file-name" :title="entry.name" @click="open(entry)">
          <Folder v-if="entry.kind === 'directory'" :size="16" />
          <File v-else :size="16" />
          <span>{{ entry.name }}</span>
        </button>
        <div v-if="inlineEntry?.path !== entry.path" class="file-menu-wrap file-actions" :class="{ open: activeMenu === 'entry' && activeEntry?.path === entry.path }" @click.stop>
          <button class="icon-button" :title="t('files.fileActions')" :aria-expanded="activeMenu === 'entry' && activeEntry?.path === entry.path" @click="toggleMenu('entry', $event, entry)"><MoreHorizontal :size="15" /></button>
        </div>
      </div>
    </div>

    <Teleport to="body">
      <div v-if="activeMenu" class="file-menu" :style="menuPosition" @click.stop>
        <template v-if="activeMenu === 'toolbar'">
          <button @click="startInlineEdit('create')"><FolderPlus :size="14" /><span>{{ t('files.newFolder') }}</span></button>
          <button :disabled="uploading" @click="fileInput?.click(); closeMenu()"><Upload :size="14" /><span>{{ t('files.uploadFiles') }}</span></button>
          <a :href="api.archiveUrl(agentId, path)" :download="archiveName(path)" @click="closeMenu"><FolderArchive :size="14" /><span>{{ t('files.downloadFolder') }}</span></a>
        </template>
        <template v-else-if="activeEntry">
          <button @click="showInfo(activeEntry)"><Info :size="14" /><span>{{ t('files.info') }}</span></button>
          <button @click="startInlineEdit('move', activeEntry)"><Pencil :size="14" /><span>{{ t('files.renameOrMove') }}</span></button>
          <a v-if="activeEntry.kind === 'file'" :href="api.downloadUrl(agentId, activeEntry.path)" :download="activeEntry.name" @click="closeMenu"><Download :size="14" /><span>{{ t('files.download') }}</span></a>
          <a v-else :href="api.archiveUrl(agentId, activeEntry.path)" :download="archiveName(activeEntry.path)" @click="closeMenu"><FolderArchive :size="14" /><span>{{ t('files.downloadFolder') }}</span></a>
          <button class="danger" @click="openDeleteDialog(activeEntry)"><Trash2 :size="14" /><span>{{ t('common.delete') }}</span></button>
        </template>
      </div>

    </Teleport>

    <AppDialog :open="dialog !== null" :title="t('files.deleteTitle')" :confirm-label="t('common.delete')" danger :busy="dialogBusy" :error="dialogError" @close="closeDialog" @confirm="submitDelete">
      <i18n-t keypath="files.deleteConfirm" scope="global" tag="p">
        <template #name><strong>{{ dialogEntry?.name }}</strong></template>
      </i18n-t>
    </AppDialog>
    <AppDialog :open="infoEntry !== null" :title="infoEntry?.name || t('files.info')" @close="infoEntry = null">
      <dl v-if="infoEntry" class="file-info">
        <dt>{{ t('files.path') }}</dt><dd>{{ infoEntry.path }}</dd>
        <dt>{{ t('files.type') }}</dt><dd>{{ t(infoEntry.kind === 'directory' ? 'common.kindDirectory' : 'common.kindFile') }}</dd>
        <template v-if="infoEntry.kind === 'file'">
          <dt>{{ t('files.size') }}</dt><dd>{{ formatSize(infoEntry.size) }}</dd>
          <dt>{{ t('files.mediaType') }}</dt><dd>{{ infoEntry.media_type || t('files.unknown') }}</dd>
        </template>
        <dt>{{ t('files.modified') }}</dt><dd>{{ formatModified(infoEntry.modified_at) }}</dd>
      </dl>
    </AppDialog>

    <UploadNotice v-if="uploadNotice" v-bind="uploadNotice" @dismiss="uploadNotice = null" />

    <FilePreview v-if="previewEntry" :agent-id="agentId" :entry="previewEntry" :style="{ height: `${settings.previewHeight}px` }" @resize="resizePreview" @close="setPreview(null)" />

    <div v-if="dragActive" class="file-drop-target">
      <Upload :size="28" />
      <strong>{{ t('files.dropToUpload') }}</strong>
      <span>{{ path || '/' }}</span>
    </div>
  </aside>
</template>
