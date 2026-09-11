<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { ImagePlus, Send, Square, X } from 'lucide-vue-next'
import { useInputHistoryStore } from '../stores/inputHistory'
import type { CommandInfo } from '../types'

const props = defineProps<{
  modelValue: string
  commands: CommandInfo[]
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
let composing = false

function handleCompositionStart() {
  composing = true
}

function handleCompositionEnd() {
  composing = false
}

const commandQuery = computed(() => input.value.match(/^\/([^\s]*)$/)?.[1].toLowerCase() ?? null)

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

const selectedCommand = ref(0)
watch(filteredCommands, () => { selectedCommand.value = 0 })
watch(selectedCommand, () => nextTick(() => {
  const list = menu.value
  const item = list?.children[selectedCommand.value] as HTMLElement | undefined
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
  if (appliedHistory) { appliedHistory = false }
  else { historyIndex = null; preHistoryDraft = '' }  // user typing exits history browsing
  nextTick(resize)
})
onMounted(() => nextTick(resize))

function chooseCommand(command: CommandInfo) {
  input.value = `/${command.name} `
  selectedCommand.value = 0
  nextTick(() => textarea.value?.focus())
}

function handleKeydown(event: KeyboardEvent) {
  if (composing || event.isComposing) return
  const { key } = event
  if (key === 'ArrowUp' || key === 'ArrowDown') {
    if (event.metaKey || event.ctrlKey) {
      event.preventDefault()
      navigateHistory(key === 'ArrowUp' ? -1 : 1)
      return
    }
    const options = filteredCommands.value
    if (options.length) {
      event.preventDefault()
      selectedCommand.value = (selectedCommand.value + (key === 'ArrowDown' ? 1 : -1) + options.length) % options.length
      return
    }
  }
  if (key === 'Tab') {
    const command = filteredCommands.value[selectedCommand.value]
    if (command) {
      event.preventDefault()
      chooseCommand(command)
      return
    }
  }
  if (key === 'Enter' && event.shiftKey) {
    event.preventDefault()
    emit('send')
  }
}

function selectImages(event: Event) {
  const target = event.target as HTMLInputElement
  emit('attach', Array.from(target.files || []).slice(0, 8 - props.images.length))
  target.value = ''
}

const hint = computed(() => {
  if (props.cancelling) return 'Cancelling execution...'
  if (filteredCommands.value.length) return '↑↓ navigate · Tab select · Shift+Enter send'
  return 'Shift+Enter send · Enter new line · Ctrl+↑↓ history'
})

const isCommand = computed(() => input.value.startsWith('/'))
</script>

<template>
  <div class="composer-wrap">
    <div v-if="filteredCommands.length" ref="menu" class="command-menu">
      <button
        v-for="(command, index) in filteredCommands"
        :key="command.name"
        :class="{ selected: index === selectedCommand }"
        @mousedown.prevent="chooseCommand(command)"
      >
        <code>/{{ command.name }}</code><span>{{ command.description }}</span>
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
      />
      <button v-if="running" class="stop-button" :title="cancelling ? 'Cancelling' : 'Stop'" :disabled="cancelling" @click="emit('stop')"><Square :size="15" fill="currentColor" /></button>
      <button v-else class="send-button" title="Send" :disabled="disabled || sending || (!input.trim() && !images.length)" @click="emit('send')"><Send :size="18" /></button>
    </div>
    <span v-if="error" class="composer-error">{{ error }}</span>
    <span class="composer-hint">{{ hint }}</span>
  </div>
</template>
