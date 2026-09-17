import { defineStore } from 'pinia'
import type { Language } from '../i18n'

export type Theme = 'system' | 'light' | 'dark'

export const useSettingsStore = defineStore('settings', {
  state: () => ({
    theme: 'system' as Theme,
    language: 'system' as Language,
    markdown: true,
    sessionsOpen: true,
    filesOpen: true,
    filesWidth: 310,
    previewHeight: 260,
  }),
  persist: true,
})
