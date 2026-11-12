export function preserveViewportCenter(viewport: HTMLElement, content: HTMLElement): () => void {
  const frame = viewport.getBoundingClientRect()
  const bounds = content.getBoundingClientRect()
  const x = (frame.left + viewport.clientLeft + viewport.clientWidth / 2 - bounds.left) / bounds.width
  const y = (frame.top + viewport.clientTop + viewport.clientHeight / 2 - bounds.top) / bounds.height

  return () => {
    const frame = viewport.getBoundingClientRect()
    const bounds = content.getBoundingClientRect()
    viewport.scrollLeft += bounds.left + bounds.width * x - frame.left - viewport.clientLeft - viewport.clientWidth / 2
    viewport.scrollTop += bounds.top + bounds.height * y - frame.top - viewport.clientTop - viewport.clientHeight / 2
  }
}
