<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ArrowLeft, Download, File, FileText, Folder, FolderArchive, Image, RefreshCw, Trash2, Upload, X } from 'lucide-vue-next'
import FilePreview from './FilePreview.vue'
import ResizeHandle from './ResizeHandle.vue'
import { api } from '../api'
import { previewKind } from '../preview'
import type { AgentInfo, FileEntry } from '../types'
import { useSettingsStore } from '../stores/settings'

const props = defineProps<{ agents: AgentInfo[]; agentId: string }>()
const emit = defineEmits<{ close: [] }>()

const settings = useSettingsStore()

const path = ref('')
const entries = ref<FileEntry[]>([])
const previewEntry = ref<FileEntry | null>(null)
const loading = ref(false)
const uploading = ref(false)
const dragActive = ref(false)
const error = ref('')
const fileInput = ref<HTMLInputElement>()
let dragDepth = 0
const currentAgent = computed(() => props.agents.find(agent => agent.identifier === props.agentId))
const parentPath = computed(() => path.value.split('/').slice(0, -1).join('/'))

function archiveName(path: string) {
  return `${path.split('/').pop() || 'workspace'}.zip`
}

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value))

function resizeWidth(delta: number) {
  settings.filesWidth = clamp(settings.filesWidth - delta, 220, 640)
}

function resizePreview(delta: number) {
  settings.previewHeight = clamp(settings.previewHeight - delta, 120, window.innerHeight - 220)
}

watch(() => props.agentId, () => { path.value = ''; previewEntry.value = null; void refresh() })

async function refresh() {
  if (!props.agentId) {
    entries.value = []
    return
  }
  loading.value = true
  error.value = ''
  try {
    entries.value = (await api.files(props.agentId, path.value)).entries
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Could not load files'
  } finally {
    loading.value = false
  }
}

function open(entry: FileEntry) {
  if (entry.kind === 'directory') {
    path.value = entry.path
    previewEntry.value = null
    void refresh()
  } else {
    // FilePreview renders inline or offers a download, based on media type.
    previewEntry.value = entry
  }
}

async function upload(files: FileList | null) {
  if (!files?.length) return
  uploading.value = true
  error.value = ''
  try {
    await api.upload(props.agentId, path.value, Array.from(files))
    await refresh()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Upload failed'
  } finally {
    uploading.value = false
    if (fileInput.value) fileInput.value.value = ''
  }
}

function dragEnter(event: DragEvent) {
  if (!event.dataTransfer?.types.includes('Files')) return
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

function goUp() {
  path.value = parentPath.value
  previewEntry.value = null
  void refresh()
}

void refresh()
</script>

<template>
  <aside class="file-browser" :class="{ 'drag-active': dragActive }" :style="{ width: `${settings.filesWidth}px` }" @dragenter.prevent="dragEnter" @dragover.prevent @dragleave.prevent="dragLeave" @drop.prevent="dropFiles">
    <ResizeHandle orientation="horizontal" @drag="resizeWidth" @reset="settings.filesWidth = 310" />
    <header class="file-header">
      <div>
        <span class="eyebrow">Workspace</span>
        <strong>{{ currentAgent?.name || 'Files' }}</strong>
      </div>
      <button class="icon-button mobile-close" title="Close files" @click="emit('close')"><X :size="18" /></button>
    </header>

    <div class="file-toolbar">
      <button class="icon-button" title="Parent folder" :disabled="!path" @click="goUp"><ArrowLeft :size="16" /></button>
      <div class="crumb" :title="path || currentAgent?.workdir">{{ path || '/' }}</div>
      <button class="icon-button" title="Refresh" @click="refresh"><RefreshCw :size="16" :class="{ spinning: loading }" /></button>
      <a class="icon-button" :href="api.archiveUrl(agentId, path)" :download="archiveName(path)" title="Download this folder as zip"><FolderArchive :size="16" /></a>
      <button class="icon-button" title="Upload files" :disabled="uploading" @click="fileInput?.click()"><Upload :size="16" :class="{ spinning: uploading }" /></button>
      <input ref="fileInput" hidden type="file" multiple @change="upload(($event.target as HTMLInputElement).files)">
    </div>

    <div v-if="error" class="file-error">{{ error }}</div>
    <div class="file-list" :aria-busy="loading || uploading">
      <div v-if="!loading && !entries.length" class="file-empty">This folder is empty.</div>
      <div v-for="entry in entries" :key="entry.path" class="file-row" @dblclick="open(entry)">
        <button class="file-name" :title="entry.name" @click="open(entry)">
          <Folder v-if="entry.kind === 'directory'" :size="16" />
          <Image v-else-if="previewKind(entry.media_type) === 'image'" :size="16" />
          <FileText v-else-if="previewKind(entry.media_type) === 'text'" :size="16" />
          <File v-else :size="16" />
          <span>{{ entry.name }}</span>
        </button>
        <div class="file-actions">
          <a v-if="entry.kind === 'file'" class="icon-button" :href="api.downloadUrl(agentId, entry.path)" :download="entry.name" title="Download"><Download :size="14" /></a>
          <a v-else class="icon-button" :href="api.archiveUrl(agentId, entry.path)" :download="archiveName(entry.path)" title="Download folder as zip"><FolderArchive :size="14" /></a>
          <button class="icon-button danger" title="Delete" @click="remove(entry)"><Trash2 :size="14" /></button>
        </div>
      </div>
    </div>

    <FilePreview v-if="previewEntry" :agent-id="agentId" :entry="previewEntry" :style="{ height: `${settings.previewHeight}px` }" @resize="resizePreview" @close="previewEntry = null" />

    <div v-if="dragActive" class="file-drop-target">
      <Upload :size="28" />
      <strong>Drop files to upload</strong>
      <span>{{ path || '/' }}</span>
    </div>
  </aside>
</template>
