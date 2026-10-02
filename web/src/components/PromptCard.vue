<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Check, ChevronDown, CircleHelp } from 'lucide-vue-next'
import type { PendingPrompt } from '../types'

const props = defineProps<{ prompt: PendingPrompt; agentName?: string; submitting?: boolean; error?: string }>()
const emit = defineEmits<{ submit: [value: string] }>()
const { t } = useI18n()
const selected = ref('')
const extra = ref('')
const activeChoice = computed(() => extra.value.trim() ? '' : selected.value)
const message = computed(() => props.prompt.message || props.prompt.prompt)

watch(() => props.prompt, prompt => {
  selected.value = prompt.default || ''
  extra.value = ''
}, { immediate: true })

function submit() {
  const value = extra.value.trim() || selected.value
  if (value) emit('submit', value)
}
</script>

<template>
  <section class="prompt-card" :aria-labelledby="`prompt-title-${prompt.id}`">
    <div class="prompt-heading">
      <span class="eyebrow">{{ agentName ? t('prompt.agentAsking', { name: agentName }) : t('prompt.agentRequest') }}</span>
      <span class="prompt-icon" aria-hidden="true"><CircleHelp :size="14" /></span>
      <h2 :id="`prompt-title-${prompt.id}`">{{ prompt.title || t('prompt.choiceRequired') }}</h2>
    </div>
    <p v-if="prompt.subtitle" class="prompt-subtitle">{{ prompt.subtitle }}</p>
    <details v-if="message.length > 500" class="prompt-message-long">
      <summary>{{ t('prompt.viewFull') }} <ChevronDown :size="13" /></summary>
      <p class="prompt-message">{{ message }}</p>
    </details>
    <p v-else class="prompt-message">{{ message }}</p>
    <div class="prompt-choices" role="group" :aria-label="prompt.title || t('prompt.choiceRequired')">
      <button v-for="(choice, index) in prompt.choices" :key="choice" type="button" :class="{ selected: activeChoice === choice }" :aria-pressed="activeChoice === choice" :disabled="submitting" @click="selected = choice; extra = ''">
        <span class="prompt-choice-index" aria-hidden="true">{{ String(index + 1).padStart(2, '0') }}</span>
        <span class="prompt-choice-label">{{ choice }}</span>
        <Check v-if="activeChoice === choice" :size="15" aria-hidden="true" />
      </button>
    </div>
    <p v-if="error" class="prompt-error" role="alert">{{ error }}</p>
    <div class="prompt-footer">
      <input v-if="prompt.allow_extra" v-model="extra" :disabled="submitting" :placeholder="t('prompt.extraPlaceholder')" :aria-label="t('prompt.extraPlaceholder')" @keydown.enter="submit">
      <button class="primary-button" :disabled="submitting || (!selected && !extra.trim())" @click="submit">{{ submitting ? t('prompt.submitting') : t('prompt.submit') }}</button>
    </div>
  </section>
</template>