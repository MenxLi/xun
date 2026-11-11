export type OfficeFormat = 'docx' | 'xlsx' | 'pptx'

const mediaTypes: Record<OfficeFormat, string> = {
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
}

export function officeFormat(mediaType: string, path: string): OfficeFormat | null {
  for (const format of ['docx', 'xlsx', 'pptx'] as const) {
    if (mediaType === mediaTypes[format]) return format
  }
  if (mediaType && mediaType !== 'application/octet-stream' && mediaType !== 'application/zip') return null
  const extension = path.match(/\.([a-z0-9]+)$/i)?.[1]?.toLowerCase()
  return extension === 'docx' || extension === 'xlsx' || extension === 'pptx' ? extension : null
}
