import { computed, ref } from 'vue'
import type { ComponentPublicInstance } from 'vue'
import { api } from './api'
import i18n from './i18n'

export type UploadGroup = { dir: string; files: File[] }
export type UploadNotice = { files: string[]; status: 'uploading' | 'complete' | 'failed'; error?: string }

type DirectoryReader = {
  readEntries: (ok: (entries: FileSystemEntry[]) => void, err: (error: DOMException) => void) => void
}

// Everything a folder-capable uploader needs: hidden inputs, drag state, a
// progress notice, and the create-directories-then-upload flow. currentDir
// is where uploads land, relative to the directory being browsed.
export function useFileUpload(options: {
  agentId: () => string
  currentDir: () => string
  enabled: () => boolean
  onError: (message: string) => void
  onUploaded: () => Promise<void> | void
}) {
  const fileInput = ref<HTMLInputElement | null>(null)
  const uploading = ref(false)
  const notice = ref<UploadNotice | null>(null)
  const dragDepth = ref(0)
  const dragActive = computed(() => dragDepth.value > 0)

  // One hidden input serves both pickers; webkitdirectory is consulted when
  // the picker opens, so toggling it before click is enough.
  function browse(folder: boolean) {
    const input = fileInput.value
    if (!input) return
    input.toggleAttribute('webkitdirectory', folder)
    input.toggleAttribute('directory', folder)
    input.click()
  }

  function setFileInput(element: Element | ComponentPublicInstance | null) {
    fileInput.value = element as HTMLInputElement | null
  }

  function onPick(event: Event) {
    const input = event.target as HTMLInputElement
    const files = Array.from(input.files ?? [])
    input.value = ''
    void run(groupsFromFiles(files))
  }

  async function onDrop(event: DragEvent) {
    dragDepth.value = 0
    if (event.dataTransfer) await run(await collectDrop(event.dataTransfer))
  }

  async function run(groups: UploadGroup[]) {
    const names = groups.flatMap(group => group.files.map(file => (group.dir ? `${group.dir}/` : '') + file.name))
    if (!names.length || !options.enabled() || uploading.value) return
    const agentId = options.agentId()
    const base = options.currentDir()
    const target = (dir: string) => [base, dir].filter(Boolean).join('/')
    uploading.value = true
    notice.value = { files: names, status: 'uploading' }
    try {
      for (const dir of chainDirectories(groups)) {
        await makeDir(agentId, target(dir))
      }
      for (const group of groups) {
        if (group.files.length) await api.upload(agentId, target(group.dir), group.files)
      }
      if (notice.value) notice.value.status = 'complete'
      await options.onUploaded()
    } catch (reason) {
      const message = reason instanceof Error && reason.message
        ? reason.message
        : i18n.global.t('files.uploadFailed')
      options.onError(message)
      if (notice.value) notice.value = { ...notice.value, status: 'failed', error: message }
    } finally {
      uploading.value = false
    }
  }

  async function makeDir(agentId: string, dir: string) {
    try {
      await api.createDirectory(agentId, dir)
    } catch (reason) {
      // directories may already exist; anything else should surface
      if ((reason as { status?: number }).status !== 409) throw reason
    }
  }

  function onDragEnter(event: DragEvent) {
    if (options.enabled() && event.dataTransfer?.types.includes('Files')) dragDepth.value += 1
  }

  function onDragLeave() {
    dragDepth.value = Math.max(0, dragDepth.value - 1)
  }

  return { setFileInput, browse, uploading, notice, dragActive, onPick, onDrop, onDragEnter, onDragLeave }
}

function groupsFromFiles(files: File[]): UploadGroup[] {
  const groups = new Map<string, UploadGroup>()
  for (const file of files) addToGroup(groups, file.webkitRelativePath || file.name, file)
  return [...groups.values()]
}

// Captures a drop as directory groups. webkitGetAsEntry is only valid
// synchronously and DataTransfer.files hides folder contents, so entries are
// grabbed up front and walked below.
async function collectDrop(data: DataTransfer): Promise<UploadGroup[]> {
  const entries = Array.from(data.items).map(item => item.kind === 'file'
    ? (item as unknown as { webkitGetAsEntry?: () => FileSystemEntry | null }).webkitGetAsEntry?.() ?? null
    : null)
  if (!entries.some(Boolean)) return groupsFromFiles(Array.from(data.files))
  const groups = new Map<string, UploadGroup>()
  for (const entry of entries) {
    if (entry) await walk(entry, '', groups)
  }
  return [...groups.values()]
}

async function walk(entry: FileSystemEntry, prefix: string, groups: Map<string, UploadGroup>) {
  const rel = prefix + entry.name
  if (entry.isFile) {
    const file = await new Promise<File | null>(resolve =>
      (entry as FileSystemFileEntry).file(found => resolve(found), () => resolve(null)))
    if (file) addToGroup(groups, rel, file)
    return
  }
  // register every directory, empty ones included; chainDirectories fills gaps
  getGroup(groups, rel)
  const reader = (entry as FileSystemEntry & { createReader?: () => DirectoryReader }).createReader?.()
  if (!reader) return
  for (const child of await readAll(reader)) await walk(child, `${rel}/`, groups)
}

function getGroup(groups: Map<string, UploadGroup>, dir: string): UploadGroup {
  const found = groups.get(dir)
  if (found) return found
  const group: UploadGroup = { dir, files: [] }
  groups.set(dir, group)
  return group
}

function addToGroup(groups: Map<string, UploadGroup>, rel: string, file: File) {
  const cut = rel.lastIndexOf('/')
  getGroup(groups, cut === -1 ? '' : rel.slice(0, cut)).files.push(file)
}

// Every directory a group set needs, ancestors included, parents first.
function chainDirectories(groups: UploadGroup[]): string[] {
  const dirs = new Set<string>()
  for (const { dir } of groups) {
    for (let cut = dir.length; cut > 0; cut = dir.lastIndexOf('/', cut - 1)) dirs.add(dir.slice(0, cut))
  }
  return [...dirs].sort((a, b) => a.split('/').length - b.split('/').length)
}

async function readAll(reader: DirectoryReader): Promise<FileSystemEntry[]> {
  // readEntries hands out ~100 items per call until it returns empty
  const all: FileSystemEntry[] = []
  for (;;) {
    const batch = await new Promise<FileSystemEntry[]>((resolve, reject) => reader.readEntries(resolve, reject))
    if (!batch.length) return all
    all.push(...batch)
  }
}
