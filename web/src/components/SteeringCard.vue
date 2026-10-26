<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Check, CornerDownRight, Pencil, Trash2, X } from 'lucide-vue-next'
import type { SteeringItem } from '../types'

const props = defineProps<{ item: SteeringItem; agentName?: string }>()
const emit = defineEmits<{
  save: [content: string]
  remove: []
}>()

const { t } = useI18n()
const editing = ref(false)
const draft = ref('')
const editor = ref<HTMLTextAreaElement>()
const canSave = computed(() => Boolean(draft.value.trim() || props.item.images.length))

// no reset on item change needed: the list keys on item.key, so editing a
// different entry always remounts this component

function startEdit() {
  draft.value = props.item.content
  editing.value = true
  void nextTick(() => {
    editor.value?.focus()
    editor.value?.setSelectionRange(draft.value.length, draft.value.length)
  })
}

function save() {
  if (!canSave.value) return
  editing.value = false
  emit('save', draft.value.trim())
}

function handleKeydown(event: KeyboardEvent) {
  if (event.isComposing) return
  if (event.key === 'Escape') {
    event.preventDefault()
    editing.value = false
  } else if (event.key === 'Enter' && (event.metaKey || event.ctrlKey || !event.shiftKey)) {
    event.preventDefault()
    save()
  }
}
</script>

<template>
  <section class="steering-card" :class="{ editing }" :aria-label="t('steering.title')">
    <header class="steering-header">
      <span class="steering-dot" aria-hidden="true" />
      <CornerDownRight class="steering-icon" :size="12" aria-hidden="true" />
      <span class="steering-label">{{ agentName ? t('steering.steeringFor', { name: agentName }) : t('steering.title') }}</span>
      <span class="steering-actions">
        <button v-if="!editing" class="icon-button" type="button" :title="t('steering.edit')" :aria-label="t('steering.edit')" @click="startEdit"><Pencil :size="12" /></button>
        <button class="icon-button danger" type="button" :title="t('steering.remove')" :aria-label="t('steering.remove')" @click="emit('remove')"><Trash2 :size="12" /></button>
      </span>
    </header>
    <div v-if="editing" class="steering-editor">
      <textarea ref="editor" v-model="draft" rows="2" :aria-label="t('steering.edit')" @keydown="handleKeydown" />
      <div class="steering-footer">
        <button class="steering-cancel" type="button" @click="editing = false"><X :size="12" />{{ t('common.cancel') }}</button>
        <button class="steering-save" type="button" :disabled="!canSave" @click="save"><Check :size="12" />{{ t('steering.save') }}</button>
      </div>
    </div>
    <div v-else class="steering-body" @dblclick="startEdit">
      <p v-if="item.content" class="steering-content">{{ item.content }}</p>
      <div v-if="item.images.length" class="steering-images">
        <img v-for="(image, index) in item.images" :key="index" :src="image.value" :alt="t('stream.attachedImage')">
      </div>
    </div>
  </section>
</template>
