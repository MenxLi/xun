<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { ChevronRight, Scale } from 'lucide-vue-next'
import { eventTime, fullEventTime } from '../api'
import type { ConfirmDisplayEvent } from '../types'

defineProps<{ event: ConfirmDisplayEvent }>()
const { t } = useI18n()
</script>

<template>
  <details class="confirm-hint">
    <summary>
      <Scale :size="12" class="confirm-icon" />
      <span>{{ event.payload.source === 'auto' ? t('confirm.autoConfirmed') : t('confirm.confirmed') }}</span>
      <span v-if="event.payload.choice" class="confirm-pick" :title="event.payload.choice">{{ event.payload.choice }}</span>
      <time :title="fullEventTime(event)">{{ eventTime(event) }}</time>
      <ChevronRight :size="10" class="chevron" />
    </summary>
    <dl>
      <div class="confirm-choices"><dt>{{ t('confirm.choices') }}</dt><dd><span v-for="choice in event.payload.choices" :key="choice" class="confirm-choice" :class="{ selected: choice === event.payload.choice }">{{ choice }}</span></dd></div>
      <div v-if="event.payload.message" class="confirm-message-block"><dt>{{ t('confirm.message') }}</dt><dd class="confirm-message">{{ event.payload.message }}</dd></div>
      <div><dt>{{ t('confirm.source') }}</dt><dd>{{ event.payload.source === 'auto' ? t('confirm.sourceAuto') : t('confirm.sourceUser') }}</dd></div>
      <div><dt>{{ t('confirm.prompt') }}</dt><dd>{{ event.payload.prompt }}</dd></div>
    </dl>
  </details>
</template>
