import { createI18n } from 'vue-i18n'
import en from './en'
import zh from './zh'

export type Language = 'system' | 'en' | 'zh'
export type Locale = 'en' | 'zh'

export const SUPPORTED_LOCALES: Locale[] = ['en', 'zh']

export function detectLocale(): Locale {
  for (const tag of navigator.languages ?? [navigator.language]) {
    const base = tag.toLowerCase().split('-')[0]
    if (base === 'zh') return 'zh'
    if (base === 'en') return 'en'
  }
  return 'en'
}

export function resolveLocale(language: Language): Locale {
  return language === 'system' ? detectLocale() : language
}

const i18n = createI18n({
  legacy: false,
  locale: detectLocale(),
  fallbackLocale: 'en',
  messages: { en, zh },
})

export default i18n
