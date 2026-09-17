<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { X } from 'lucide-vue-next'

const props = withDefaults(defineProps<{
  open: boolean
  title: string
  confirmLabel?: string
  cancelLabel?: string
  danger?: boolean
  busy?: boolean
  error?: string
}>(), {
  confirmLabel: '',
  cancelLabel: '',
  danger: false,
  busy: false,
  error: '',
})

const emit = defineEmits<{ close: []; confirm: [] }>()
const { t } = useI18n()
const dialogElement = ref<HTMLFormElement>()
const cancelText = computed(() => props.cancelLabel || t('common.cancel'))
const closeText = computed(() => t('common.close'))

function close() {
  if (!props.busy) emit('close')
}

function confirm() {
  if (!props.busy) emit('confirm')
}

watch(() => props.open, async open => {
  if (!open) return
  await nextTick()
  const target = dialogElement.value?.querySelector<HTMLElement>('[autofocus]')
    ?? dialogElement.value?.querySelector<HTMLElement>('input, button')
  target?.focus()
})
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="app-dialog-backdrop" @click.self="close" @keydown.esc.stop="close">
      <form ref="dialogElement" class="app-dialog" role="dialog" aria-modal="true" :aria-label="title" @submit.prevent="confirm">
        <header>
          <strong>{{ title }}</strong>
          <button class="icon-button" type="button" :title="closeText" :disabled="busy" @click="close"><X :size="16" /></button>
        </header>
        <div class="app-dialog-body">
          <slot />
          <div v-if="error" class="app-dialog-error">{{ error }}</div>
        </div>
        <footer v-if="confirmLabel">
          <button class="dialog-button" type="button" :disabled="busy" @click="close">{{ cancelText }}</button>
          <button class="dialog-button primary" :class="{ danger }" type="submit" :disabled="busy">{{ confirmLabel }}</button>
        </footer>
      </form>
    </div>
  </Teleport>
</template>
