<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { File, Folder, ImagePlus, Send, Square, X } from 'lucide-vue-next'
import { api } from '../api'
import { useInputHistoryStore } from '../stores/inputHistory'
import type { CommandInfo, FileEntry } from '../types'

const props = defineProps<{
  modelValue: string
  commands: CommandInfo[]
  agentId: string
  filesAvailable: boolean
  placeholder: string
  disabled: boolean
  supportsVision: boolean
  images: Array<{ file: File; url: string }>
  running: boolean
  cancelling: boolean
  sending: boolean
  error: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
  (e: 'send'): void
  (e: 'stop'): void
  (e: 'attach', files: File[]): void
  (e: 'remove-image', index: number): void
}>()

const history = useInputHistoryStore()
const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif'])
const MAX_IMAGES = 8
let historyIndex: number | null = null
let preHistoryDraft = ''
let appliedHistory = false

function exitHistory() {
  historyIndex = null
  preHistoryDraft = ''
}

function set(value: string) {
  if (value !== input.value) { appliedHistory = true; input.value = value }
}

function navigateHistory(direction: 1 | -1) {
  const entries = history.entries
  if (!entries.length) return
  if (historyIndex === null) {
    if (direction === 1) return  // already at newest; Ctrl+↓ from the draft is a no-op (readline)
    preHistoryDraft = input.value
    historyIndex = 0
  } else if (direction === -1) {
    historyIndex = Math.min(historyIndex + 1, entries.length - 1)
  } else if (historyIndex > 0) {
    historyIndex--
  } else {
    set(preHistoryDraft)
    exitHistory()
    return
  }
  set(entries[historyIndex])
  nextTick(() => textarea.value?.focus())
}

const input = computed({
  get: () => props.modelValue,
  set: value => emit('update:modelValue', value),
})
const textarea = ref<HTMLTextAreaElement>()
const imageInput = ref<HTMLInputElement>()
const menu = ref<HTMLElement>()
const cursor = ref(0)
const files = ref<FileEntry[]>([])
const menuDismissed = ref(false)
let composing = false
let fileRequest = 0

function handleCompositionStart() {
  composing = true
}

function handleCompositionEnd() {
  composing = false
}

const commandQuery = computed(() => input.value.match(/^\/([^\s]*)$/)?.[1].toLowerCase() ?? null)
const fileQuery = computed(() => {
  if (!props.filesAvailable) return null
  const position = Math.min(cursor.value, input.value.length)
  const match = input.value.slice(0, position).match(/@([^\s@]*)$/)
  if (!match) return null
  const value = match[1]
  const slash = value.lastIndexOf('/')
  return {
    directory: slash === -1 ? '' : value.slice(0, slash),
    query: value.slice(slash + 1).toLowerCase(),
    start: position - value.length - 1,
    end: position,
  }
})
watch(
  [() => fileQuery.value?.directory ?? null, () => props.agentId, () => props.filesAvailable],
  async ([directory, agentId, available]) => {
    const request = ++fileRequest
    files.value = []
    if (directory === null || !agentId || !available) return
    try {
      const listing = await api.files(agentId, directory)
      if (request === fileRequest) files.value = listing.entries
    } catch {
      if (request === fileRequest) files.value = []
    }
  },
)

const filteredCommands = computed<CommandInfo[]>(() => {
  const query = commandQuery.value
  if (query === null) return []
  return props.commands
    .map(command => {
      const name = command.name.toLowerCase()
      const rank = name === query ? 0 : name.startsWith(query) ? 1 : name.includes(query) ? 2
        : command.description.toLowerCase().includes(query) ? 3 : 4
      return { command, rank }
    })
    .filter(entry => entry.rank < 4)
    .sort((a, b) => a.rank - b.rank)
    .map(entry => entry.command)
})

const filteredFiles = computed<FileEntry[]>(() => {
  const match = fileQuery.value
  if (!match) return []
  return files.value
    .map(file => {
      const name = file.name.toLowerCase()
      const rank = name === match.query ? 0 : name.startsWith(match.query) ? 1
        : name.includes(match.query) ? 2 : 3
      return { file, rank }
    })
    .filter(entry => entry.rank < 3)
    .sort((a, b) => a.rank - b.rank || a.file.path.localeCompare(b.file.path))
    .map(entry => entry.file)
})

const menuKind = computed<'command' | 'file' | null>(() => {
  if (menuDismissed.value) return null
  if (filteredCommands.value.length) return 'command'
  if (filteredFiles.value.length) return 'file'
  return null
})
const optionCount = computed(() => menuKind.value === 'command' ? filteredCommands.value.length : filteredFiles.value.length)

const selectedOption = ref(0)
watch([filteredCommands, filteredFiles], () => { selectedOption.value = 0 })
watch(selectedOption, () => nextTick(() => {
  const list = menu.value
  const item = list?.children[selectedOption.value] as HTMLElement | undefined
  if (!list || !item) return
  if (item.offsetTop < list.scrollTop) list.scrollTop = item.offsetTop
  else if (item.offsetTop + item.offsetHeight > list.scrollTop + list.clientHeight) {
    list.scrollTop = item.offsetTop + item.offsetHeight - list.clientHeight
  }
}))

function resize() {
  const area = textarea.value
  if (!area) return
  area.style.height = 'auto'
  area.style.height = `${Math.min(area.scrollHeight, 180)}px`
}
watch(input, () => {
  menuDismissed.value = false
  if (appliedHistory) { appliedHistory = false }
  else { historyIndex = null; preHistoryDraft = '' }  // user typing exits history browsing
  nextTick(resize)
})
onMounted(() => nextTick(resize))

function chooseCommand(command: CommandInfo) {
  input.value = `/${command.name} `
  selectedOption.value = 0
  nextTick(() => textarea.value?.focus())
}

function chooseFile(file: FileEntry) {
  const match = fileQuery.value
  if (!match) return
  const before = input.value.slice(0, match.start)
  const after = input.value.slice(match.end)
  const suffix = file.kind === 'directory' ? '/' : (!after || !/^\s/.test(after) ? ' ' : '')
  input.value = `${before}@${file.path}${suffix}${after}`
  const position = before.length + file.path.length + 1 + suffix.length
  selectedOption.value = 0
  nextTick(() => {
    textarea.value?.focus()
    textarea.value?.setSelectionRange(position, position)
    cursor.value = position
  })
}

function updateCursor() {
  const position = textarea.value?.selectionStart ?? input.value.length
  if (position !== cursor.value) menuDismissed.value = false
  cursor.value = position
}

function handleKeydown(event: KeyboardEvent) {
  if (composing || event.isComposing) return
  const { key } = event
  if (key === 'Escape' && menuKind.value) {
    event.preventDefault()
    menuDismissed.value = true
    return
  }
  if (key === 'ArrowUp' || key === 'ArrowDown') {
    if (event.metaKey || event.ctrlKey) {
      event.preventDefault()
      navigateHistory(key === 'ArrowUp' ? -1 : 1)
      return
    }
    if (optionCount.value) {
      event.preventDefault()
      selectedOption.value = (selectedOption.value + (key === 'ArrowDown' ? 1 : -1) + optionCount.value) % optionCount.value
      return
    }
  }
  if (key === 'Tab' || (key === 'Enter' && !event.metaKey && !event.ctrlKey && !event.shiftKey)) {
    const command = menuKind.value === 'command' ? filteredCommands.value[selectedOption.value] : null
    const file = menuKind.value === 'file' ? filteredFiles.value[selectedOption.value] : null
    if (command || file) {
      event.preventDefault()
      if (command) chooseCommand(command)
      else if (file) chooseFile(file)
      return
    }
  }
  if (key === 'Enter' && (event.metaKey || event.ctrlKey)) {
    event.preventDefault()
    emit('send')
  }
}

function selectImages(event: Event) {
  const target = event.target as HTMLInputElement
  attachImages(Array.from(target.files || []))
  target.value = ''
}

function attachImages(files: File[]) {
  const remaining = MAX_IMAGES - props.images.length
  if (!props.supportsVision || remaining <= 0) return false
  const images = files.filter(file => IMAGE_TYPES.has(file.type)).slice(0, remaining)
  if (!images.length) return false
  emit('attach', images)
  return true
}

function handlePaste(event: ClipboardEvent) {
  const files = Array.from(event.clipboardData?.files || [])
  if (attachImages(files)) event.preventDefault()
}

const hint = computed(() => {
  if (props.cancelling) return 'Cancelling execution...'
  if (menuKind.value) return '↑↓ navigate · Enter/Tab select · Ctrl/⌘+Enter send'
  return 'Ctrl/⌘+Enter send · Enter new line · Ctrl/⌘+↑↓ history'
})

const isCommand = computed(() => input.value.startsWith('/'))
</script>

<template>
  <div class="composer-wrap">
    <div v-if="menuKind" ref="menu" class="command-menu">
      <button
        v-if="menuKind === 'command'"
        v-for="(command, index) in filteredCommands"
        :key="command.name"
        :class="{ selected: index === selectedOption }"
        @mousedown.prevent="chooseCommand(command)"
      >
        <code>/{{ command.name }}</code><span>{{ command.description }}</span>
      </button>
      <button
        v-for="(file, index) in menuKind === 'file' ? filteredFiles : []"
        :key="file.path"
        class="file-option"
        :class="{ selected: index === selectedOption }"
        @mousedown.prevent="chooseFile(file)"
      >
        <Folder v-if="file.kind === 'directory'" :size="14" />
        <File v-else :size="14" />
        <code>{{ file.path }}</code><span>{{ file.kind }}</span>
      </button>
    </div>
    <div v-if="images.length" class="image-tray">
      <div v-for="(image, index) in images" :key="image.url" class="image-preview">
        <img :src="image.url" :alt="image.file.name">
        <button type="button" :title="`Remove ${image.file.name}`" @click="emit('remove-image', index)"><X :size="13" /></button>
      </div>
    </div>
    <div class="composer">
      <input v-if="supportsVision" ref="imageInput" class="visually-hidden" type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple @change="selectImages">
      <button v-if="supportsVision" class="attach-button" type="button" title="Attach images" :disabled="sending || running || images.length >= 8" @click="imageInput?.click()"><ImagePlus :size="18" /></button>
      <textarea
        ref="textarea"
        v-model="input"
        rows="1"
        :class="{ 'is-command': isCommand }"
        :placeholder="placeholder"
        :disabled="disabled"
        @compositionstart="handleCompositionStart"
        @compositionend="handleCompositionEnd"
        @keydown="handleKeydown"
        @input="updateCursor"
        @click="updateCursor"
        @keyup="updateCursor"
        @paste="handlePaste"
      />
      <button v-if="running" class="stop-button" :title="cancelling ? 'Cancelling' : 'Stop'" :disabled="cancelling" @click="emit('stop')"><Square :size="15" fill="currentColor" /></button>
      <button v-else class="send-button" title="Send" :disabled="disabled || sending || (!input.trim() && !images.length)" @click="emit('send')"><Send :size="18" /></button>
    </div>
    <span v-if="error" class="composer-error">{{ error }}</span>
    <span class="composer-hint">{{ hint }}</span>
  </div>
</template>
