<script setup lang="ts">
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ChevronDown } from 'lucide-vue-next'
import type { PendingPrompt } from '../types'

const props = defineProps<{ prompt: PendingPrompt; agentName?: string; submitting?: boolean; error?: string }>()
const emit = defineEmits<{ submit: [value: string] }>()
const { t } = useI18n()
const selected = ref('')
const extra = ref('')

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
    <span class="eyebrow">{{ agentName ? t('prompt.agentAsking', { name: agentName }) : t('prompt.agentRequest') }}</span>
    <h2 :id="`prompt-title-${prompt.id}`">{{ prompt.title || t('prompt.choiceRequired') }}</h2>
    <p v-if="prompt.subtitle" class="prompt-subtitle">{{ prompt.subtitle }}</p>
    <details v-if="(prompt.message || prompt.prompt).length > 500" class="prompt-message-long">
      <summary>{{ t('prompt.viewFull') }} <ChevronDown :size="13" /></summary>
      <p>{{ prompt.message || prompt.prompt }}</p>
    </details>
    <p v-else>{{ prompt.message || prompt.prompt }}</p>
    <div class="prompt-choices">
      <button v-for="choice in prompt.choices" :key="choice" :class="{ selected: selected === choice }" @click="selected = choice">{{ choice }}</button>
    </div>
    <input v-if="prompt.allow_extra" v-model="extra" :placeholder="t('prompt.extraPlaceholder')" @keydown.enter="submit">
    <p v-if="error" class="prompt-error" role="alert">{{ error }}</p>
    <button class="primary-button" :disabled="submitting || (!selected && !extra.trim())" @click="submit">{{ submitting ? t('prompt.submitting') : t('prompt.submit') }}</button>
  </section>
</template>