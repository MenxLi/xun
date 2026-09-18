import { defineStore } from 'pinia'

export type AttachedImage = { file: File; url: string }
export type BrowserState = { path: string; previewPath: string | null }
export type SessionBuffer = {
  input: string
  images: AttachedImage[]
  selectedAgentId: string
  browser: BrowserState
}

function createBuffer(): SessionBuffer {
  return {
    input: '',
    images: [],
    selectedAgentId: '',
    browser: { path: '', previewPath: null },
  }
}

export const useSessionBuffersStore = defineStore('sessionBuffers', {
  state: () => ({
    currentPath: '/',
    buffers: { '/': createBuffer() } as Record<string, SessionBuffer>,
  }),
  getters: {
    current: state => state.buffers[state.currentPath],
  },
  actions: {
    select(path: string) {
      this.buffers[path] ??= createBuffer()
      this.currentPath = path
    },
    discard(path: string) {
      this.buffers[path]?.images.forEach(image => URL.revokeObjectURL(image.url))
      delete this.buffers[path]
    },
    dispose() {
      Object.keys(this.buffers).forEach(path => this.discard(path))
    },
  },
})
