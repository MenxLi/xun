export type PreviewKind = 'image' | 'text' | 'unsupported'

// Structured text formats previewed as text despite not being text/*;
// mirrors TEXT_MEDIA_TYPES in src/xun/displays/web_file.py.
const TEXT_LIKE = new Set(['application/json', 'application/toml', 'application/xml', 'application/yaml'])

// The single classification point for listing icons and the preview pane.
// New formats: extend this plus the renderer in FilePreview.vue.
export function previewKind(mediaType: string | null): PreviewKind {
  if (!mediaType) return 'unsupported'
  if (mediaType.startsWith('image/')) return 'image'
  if (mediaType.startsWith('text/') || TEXT_LIKE.has(mediaType)) return 'text'
  return 'unsupported'
}
