import type { FileInfo, PathInfo } from './types'

export type PreviewKind = 'image' | 'text' | 'pdf' | 'unsupported'
export type RenderKind = 'markdown' | 'html'

export function asFileInfo(info: PathInfo): FileInfo | null {
  return info.kind === 'file' ? (info as FileInfo) : null
}

export function previewKind(entry: FileInfo): PreviewKind {
  if (entry.media_type.startsWith('image/')) return 'image'
  if (entry.is_text) return 'text'
  if (entry.media_type === 'application/pdf') return 'pdf'
  return 'unsupported'
}

export function renderKind(mediaType: string | null, path: string): RenderKind | null {
  const extension = path.split('.').pop()?.toLowerCase() ?? ''
  if (mediaType === 'text/markdown' || mediaType === 'text/x-markdown' || extension === 'md' || extension === 'markdown') return 'markdown'
  if (mediaType === 'text/html' || extension === 'html' || extension === 'htm') return 'html'
  return null
}
