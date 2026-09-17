<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ArrowLeft, Download, File, Folder, FolderArchive, FolderPlus, Info, MoreHorizontal, Pencil, RefreshCw, Trash2, Upload, X } from 'lucide-vue-next'
import FilePreview from './FilePreview.vue'
import ResizeHandle from './ResizeHandle.vue'
import UploadNotice from './UploadNotice.vue'
import { api } from '../api'
import type { AgentInfo, FileEntry, FileInfo } from '../types'
import { useSettingsStore } from '../stores/settings'

const props = defineProps<{ agents: AgentInfo[]; agentId: string; available: boolean | null }>()
const emit = defineEmits<{ close: [] }>()

const settings = useSettingsStore()

const path = ref('')
const entries = ref<FileEntry[]>([])
const previewEntry = ref<FileInfo | null>(null)
const infoEntry = ref<FileInfo | null>(null)
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

function childPath(name: string) {
  return path.value ? `${path.value}/${name}` : name
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

watch([() => props.agentId, () => props.available], () => {
  metadataRequest += 1
  closeMenu()
  path.value = ''
  previewEntry.value = null
  infoEntry.value = null
  void refresh()
}, { immediate: true })

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
    if (request === listingRequest) error.value = reason instanceof Error ? reason.message : 'Could not load files'
  } finally {
    if (request === listingRequest) loading.value = false
  }
}

async function open(entry: FileEntry) {
  if (entry.kind === 'directory') {
    path.value = entry.path
    previewEntry.value = null
    void refresh()
  } else {
    const request = ++metadataRequest
    try {
      const info = await api.fileInfo(props.agentId, entry.path)
      if (request === metadataRequest) previewEntry.value = info
    } catch (reason) {
      if (request === metadataRequest) error.value = reason instanceof Error ? reason.message : 'Could not inspect file'
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
    if (request === metadataRequest) error.value = reason instanceof Error ? reason.message : 'Could not inspect path'
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
    const message = reason instanceof Error ? reason.message : 'Upload failed'
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

async function remove(entry: FileEntry) {
  if (!window.confirm(`Delete ${entry.name}?`)) return
  try {
    await api.remove(props.agentId, entry.path)
    if (previewEntry.value?.path === entry.path) previewEntry.value = null
    await refresh()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Delete failed'
  }
}


async function createDirectory() {
  closeMenu()
  const name = window.prompt('New folder name')?.trim()
  if (!name) return
  try {
    await api.createDirectory(props.agentId, childPath(name))
    await refresh()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Could not create folder'
  }
}

async function move(entry: FileEntry) {
  closeMenu()
  const destination = window.prompt('New workspace-relative path', entry.path)?.trim()
  if (!destination || destination === entry.path) return
  try {
    await api.move(props.agentId, entry.path, destination)
    if (previewEntry.value?.path === entry.path) previewEntry.value = null
    await refresh()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Could not move file'
  }
}

function goUp() {
  path.value = parentPath.value
  previewEntry.value = null
  void refresh()
}

</script>

<template>
  <aside class="file-browser" :class="{ 'drag-active': dragActive }" :style="{ width: `${settings.filesWidth}px` }" @click="activeMenu = null" @keydown.esc="activeMenu = null" @dragenter.prevent="dragEnter" @dragover.prevent @dragleave.prevent="dragLeave" @drop.prevent="dropFiles">
    <ResizeHandle orientation="horizontal" @drag="resizeWidth" @reset="settings.filesWidth = 310" />
    <header class="file-header">
      <div>
        <span class="eyebrow">Workspace</span>
        <strong>{{ currentAgent?.name || 'Files' }}</strong>
      </div>
      <button class="icon-button mobile-close" title="Close files" @click="emit('close')"><X :size="18" /></button>
    </header>

    <div class="file-toolbar">
      <button class="icon-button" title="Parent folder" :disabled="!available || !agentId || !path" @click="goUp"><ArrowLeft :size="16" /></button>
      <div class="crumb" :title="path || currentAgent?.workdir">{{ path || '/' }}</div>
      <button class="icon-button" title="Refresh" :disabled="!available || !agentId" @click="refresh"><RefreshCw :size="16" :class="{ spinning: loading }" /></button>
      <div class="file-menu-wrap" @click.stop>
        <button class="icon-button" title="File actions" :disabled="!available || !agentId" :aria-expanded="activeMenu === 'toolbar'" @click="toggleMenu('toolbar', $event)"><MoreHorizontal :size="17" /></button>
      </div>
      <input ref="fileInput" hidden type="file" multiple @change="upload(($event.target as HTMLInputElement).files)">
    </div>

    <div v-if="error" class="file-error">{{ error }}</div>
    <div class="file-list" :aria-busy="loading || uploading" @scroll="closeMenu">
      <div v-if="available === false" class="file-empty">File access is disabled.</div>
      <div v-else-if="available && agentId && !loading && !entries.length" class="file-empty">This folder is empty.</div>
      <div v-for="entry in entries" :key="entry.path" class="file-row">
        <button class="file-name" :title="entry.name" @click="open(entry)">
          <Folder v-if="entry.kind === 'directory'" :size="16" />
          <File v-else :size="16" />
          <span>{{ entry.name }}</span>
        </button>
        <div class="file-menu-wrap file-actions" :class="{ open: activeMenu === 'entry' && activeEntry?.path === entry.path }" @click.stop>
          <button class="icon-button" title="File actions" :aria-expanded="activeMenu === 'entry' && activeEntry?.path === entry.path" @click="toggleMenu('entry', $event, entry)"><MoreHorizontal :size="15" /></button>
        </div>
      </div>
    </div>

    <Teleport to="body">
      <div v-if="activeMenu" class="file-menu" :style="menuPosition" @click.stop>
        <template v-if="activeMenu === 'toolbar'">
          <button @click="createDirectory"><FolderPlus :size="14" /><span>New folder</span></button>
          <button :disabled="uploading" @click="fileInput?.click(); closeMenu()"><Upload :size="14" /><span>Upload files</span></button>
          <a :href="api.archiveUrl(agentId, path)" :download="archiveName(path)" @click="closeMenu"><FolderArchive :size="14" /><span>Download folder</span></a>
        </template>
        <template v-else-if="activeEntry">
          <button @click="showInfo(activeEntry)"><Info :size="14" /><span>Info</span></button>
          <button @click="move(activeEntry)"><Pencil :size="14" /><span>Rename or move</span></button>
          <a v-if="activeEntry.kind === 'file'" :href="api.downloadUrl(agentId, activeEntry.path)" :download="activeEntry.name" @click="closeMenu"><Download :size="14" /><span>Download</span></a>
          <a v-else :href="api.archiveUrl(agentId, activeEntry.path)" :download="archiveName(activeEntry.path)" @click="closeMenu"><FolderArchive :size="14" /><span>Download folder</span></a>
          <button class="danger" @click="remove(activeEntry); closeMenu()"><Trash2 :size="14" /><span>Delete</span></button>
        </template>
      </div>

      <div v-if="infoEntry" class="file-info-backdrop" role="presentation" @click.self="infoEntry = null">
        <section class="file-info-dialog" role="dialog" aria-modal="true" aria-labelledby="file-info-title">
          <header>
            <strong id="file-info-title">{{ infoEntry.name }}</strong>
            <button class="icon-button" title="Close info" @click="infoEntry = null"><X :size="16" /></button>
          </header>
          <dl>
            <dt>Path</dt><dd>{{ infoEntry.path }}</dd>
            <dt>Type</dt><dd>{{ infoEntry.kind }}</dd>
            <template v-if="infoEntry.kind === 'file'">
              <dt>Size</dt><dd>{{ formatSize(infoEntry.size) }}</dd>
              <dt>Media type</dt><dd>{{ infoEntry.media_type || 'Unknown' }}</dd>
            </template>
            <dt>Modified</dt><dd>{{ formatModified(infoEntry.modified_at) }}</dd>
          </dl>
        </section>
      </div>

    </Teleport>

    <UploadNotice v-if="uploadNotice" v-bind="uploadNotice" @dismiss="uploadNotice = null" />

    <FilePreview v-if="previewEntry" :agent-id="agentId" :entry="previewEntry" :style="{ height: `${settings.previewHeight}px` }" @resize="resizePreview" @close="previewEntry = null" />

    <div v-if="dragActive" class="file-drop-target">
      <Upload :size="28" />
      <strong>Drop files to upload</strong>
      <span>{{ path || '/' }}</span>
    </div>
  </aside>
</template>
