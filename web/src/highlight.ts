// Central highlight.js setup: register languages and maps here; theme colors in style.css.
import hljs from 'highlight.js/lib/core'
import bash from 'highlight.js/lib/languages/bash'
import css from 'highlight.js/lib/languages/css'
import diff from 'highlight.js/lib/languages/diff'
import dockerfile from 'highlight.js/lib/languages/dockerfile'
import go from 'highlight.js/lib/languages/go'
import ini from 'highlight.js/lib/languages/ini'
import java from 'highlight.js/lib/languages/java'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import markdown from 'highlight.js/lib/languages/markdown'
import python from 'highlight.js/lib/languages/python'
import rust from 'highlight.js/lib/languages/rust'
import sql from 'highlight.js/lib/languages/sql'
import typescript from 'highlight.js/lib/languages/typescript'
import xml from 'highlight.js/lib/languages/xml'
import yaml from 'highlight.js/lib/languages/yaml'

const languages: Record<string, typeof javascript> = {
  bash, css, diff, dockerfile, go, ini, java, javascript, json, markdown,
  python, rust, sql, typescript, xml, yaml,
}
for (const [name, language] of Object.entries(languages)) hljs.registerLanguage(name, language)

const FILE_NAMES: Record<string, string> = {
  dockerfile: 'dockerfile',
  makefile: 'bash',
}

// Language -> file extensions.
const EXTENSIONS: Record<string, string[]> = {
  bash: ['sh', 'zsh', 'env', 'profile'],
  css: ['css', 'scss'],
  diff: ['patch', 'diff'],
  dockerfile: ['dockerfile'],
  go: ['go'],
  ini: ['toml', 'ini', 'cfg', 'conf', 'editorconfig'],
  java: ['java', 'kt', 'kts'],
  javascript: ['js', 'jsx', 'mjs', 'cjs'],
  json: ['json', 'jsonc', 'jsonl'],
  markdown: ['md', 'markdown'],
  python: ['py', 'pyi', 'ipynb'],
  rust: ['rs'],
  sql: ['sql'],
  typescript: ['ts', 'tsx', 'mts', 'cts', 'vue'],
  xml: ['html', 'htm', 'xml', 'svg'],
  yaml: ['yml', 'yaml'],
}

// Highlighted HTML safe for v-html, or null when the language has no grammar.
export function highlightLanguage(name: string, code: string): string | null {
  if (!hljs.getLanguage(name)) return null
  return hljs.highlight(code, { language: name, ignoreIllegals: true }).value
}

function languageForPath(path: string): string {
  const name = path.split('/').pop()!.toLowerCase()
  if (FILE_NAMES[name]) return FILE_NAMES[name]
  const extension = name.includes('.') ? name.split('.').pop()! : ''
  return Object.keys(EXTENSIONS).find(key => EXTENSIONS[key].includes(extension)) ?? ''
}

export function highlightFile(path: string, code: string): string | null {
  return highlightLanguage(languageForPath(path), code)
}
