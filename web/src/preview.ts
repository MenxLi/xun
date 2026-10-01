export type PreviewKind = 'image' | 'text' | 'pdf' | 'unsupported'
export type RenderKind = 'markdown' | 'html'

// Structured text formats previewed as text despite not being text/*;
// mirrors TEXT_MEDIA_TYPES in src/xun/displays/web_file.py.
const TEXT_LIKE = new Set(['application/json', 'application/toml', 'application/xml', 'application/yaml'])

// The single classification point for listing icons and the preview pane.
// New formats: extend this plus the renderer in FilePreview.vue.
export function previewKind(mediaType: string | null): PreviewKind {
  if (!mediaType) return 'unsupported'
  if (mediaType.startsWith('image/')) return 'image'
  if (mediaType.startsWith('text/') || TEXT_LIKE.has(mediaType)) return 'text'
  if (mediaType === 'application/pdf') return 'pdf'
  return 'unsupported'
}

// Formats that can switch between source and rendered view.
export function renderKind(mediaType: string | null, path: string): RenderKind | null {
  const extension = path.split('.').pop()?.toLowerCase() ?? ''
  if (mediaType === 'text/markdown' || mediaType === 'text/x-markdown' || extension === 'md' || extension === 'markdown') return 'markdown'
  if (mediaType === 'text/html' || extension === 'html' || extension === 'htm') return 'html'
  return null
}
