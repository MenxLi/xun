import type { AgentInfo, CommandInfo, DisplayEvent, FileListing, ModelCapabilities, PendingPrompt, SessionInfo, SessionList, WebConfig } from './types'

const configuredServiceRoot = import.meta.env.VITE_XUN_BASE_PATH as string | undefined
const inferredServiceRoot = location.pathname.match(/^(.*)\/chat(?:\/|$)/)?.[1] ?? ''
const serviceRoot = (configuredServiceRoot ?? inferredServiceRoot).replace(/\/$/, '')
let displayBaseUrl = `${serviceRoot}/session`

export function appUrl(path: string): string {
  return `${displayBaseUrl}${path}`
}

export function configureSession(path: string): void {
  const suffix = path.split('/').filter(Boolean).map(encodeURIComponent).join('/')
  displayBaseUrl = `${serviceRoot}/session${suffix ? `/${suffix}` : ''}`
}

function sessionUrl(path = ''): string {
  const suffix = path.split('/').filter(Boolean).map(encodeURIComponent).join('/')
  return `${serviceRoot}/api/sessions${suffix ? `/${suffix}` : ''}`
}

export function chatUrl(sessionPath: string): string {
  const url = new URL(`${serviceRoot}/chat/`, location.origin)
  url.searchParams.set('session', sessionPath)
  return `${url.pathname}${url.search}`
}

async function fetchOk(url: string, options?: RequestInit): Promise<Response> {
  const response = await fetch(url, options)
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(body?.detail || `Request failed (${response.status})`)
  }
  return response
}

function request<T>(url: string, options?: RequestInit): Promise<T> {
  return fetchOk(url, options).then(response => response.json() as Promise<T>)
}

function query(params: Record<string, string>): string {
  return new URLSearchParams(params).toString()
}

export function formatTokens(tokens: number): string {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(2)}M`
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(1)}K`
  return `${tokens}`
}

export function eventTime(event: { timestamp: number }): string {
  return new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }).format(event.timestamp * 1000)
}

export function fullEventTime(event: { timestamp: number }): string {
  return new Date(event.timestamp * 1000).toLocaleString()
}

export const api = {
  sessions: () => request<SessionList>(sessionUrl()),
  createSession: (name: string) => request<SessionInfo>(sessionUrl(), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name || null }),
  }),
  removeSession: (path: string) => request<{ removed: boolean }>(sessionUrl(path), { method: 'DELETE' }),
  config: () => request<WebConfig>(appUrl('/api/config')),
  events: () => request<DisplayEvent[]>(appUrl('/api/events')),
  prompts: () => request<PendingPrompt[]>(appUrl('/api/prompts')),
  resolvePrompt: (promptId: string, value: string) => request<{ resolved: boolean }>(
    appUrl(`/api/prompts/${encodeURIComponent(promptId)}`),
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: 'choice', prompt_id: promptId, value }),
    },
  ),
  agents: () => request<AgentInfo[]>(appUrl('/api/agents')),
  running: () => request<string[]>(appUrl('/api/running')),
  commands: (agentId: string) => request<CommandInfo[]>(appUrl(`/api/commands/${encodeURIComponent(agentId)}`)),
  capabilities: (agentId: string) => request<ModelCapabilities>(appUrl(`/api/capabilities/${encodeURIComponent(agentId)}`)),
  files: (agentId: string, path = '') =>
    request<FileListing>(appUrl(`/api/files/${encodeURIComponent(agentId)}?${query({ path })}`)),
  contentUrl: (agentId: string, path: string) =>
    appUrl(`/api/files/${encodeURIComponent(agentId)}/content?${query({ path })}`),
  textContent: (agentId: string, path: string) =>
    fetchOk(appUrl(`/api/files/${encodeURIComponent(agentId)}/content?${query({ path })}`)).then(response => response.text()),
  downloadUrl: (agentId: string, path: string) =>
    appUrl(`/api/files/${encodeURIComponent(agentId)}/download?${query({ path })}`),
  archiveUrl: (agentId: string, path: string) =>
    appUrl(`/api/files/${encodeURIComponent(agentId)}/archive?${query({ path })}`),
  upload: (agentId: string, path: string, files: File[]) => {
    const body = new FormData()
    files.forEach(file => body.append('files', file))
    return request<{ uploaded: string[] }>(
      appUrl(`/api/files/${encodeURIComponent(agentId)}/upload?${query({ path })}`),
      { method: 'POST', body },
    )
  },
  remove: (agentId: string, path: string) =>
    request<{ deleted: boolean }>(appUrl(`/api/files/${encodeURIComponent(agentId)}?${query({ path })}`), { method: 'DELETE' }),
}
