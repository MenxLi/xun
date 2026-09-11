export interface AgentInfo {
  identifier: string
  name: string
  workdir: string
}

export interface CommandInfo {
  name: string
  description: string
}

export interface ToolInfo {
  name: string
  description: string
  required_capabilities: string[]
}

type EventEnvelope<Name extends string, Payload> = {
  name: Name
  agent: AgentInfo
  timestamp: number
  payload: Payload
}

export type ToolCallDisplayEvent = EventEnvelope<'ToolCallEvent', {
  tool_call_id: string
  tool_name: string
  args: Record<string, unknown>
}>

export type ToolResultDisplayEvent = EventEnvelope<'ToolResultEvent', {
  tool_call_id: string
  result: unknown
}>

export interface ImageDescriptor {
  kind: 'url' | 'base64'
  value: string
}

export type DisplayEvent =
  | EventEnvelope<'AgentBindEvent', Record<string, never>>
  | EventEnvelope<'AgentUnbindEvent', Record<string, never>>
  | EventEnvelope<'ModelWorkingEvent', { model_call_id: string; remaining_iterations?: number | null }>
  | EventEnvelope<'ModelMessageEvent', { model_call_id: string; content: string; reasoning?: string | null; total_tokens: number }>
  | ToolCallDisplayEvent
  | ToolResultDisplayEvent
  | EventEnvelope<'ShowHistoryEvent', { history: Array<{ role: string; content: unknown }> }>
  | EventEnvelope<'ShowHelpEvent', { commands: CommandInfo[] }>
  | EventEnvelope<'ShowToolsEvent', { tools: ToolInfo[] }>
  | EventEnvelope<'UserCommandEvent', { name: string; arguments?: string | null }>
  | EventEnvelope<'UserMessageEvent', { content: string; images: ImageDescriptor[] }>
  | EventEnvelope<'InfoEvent', { message: string }>
  | EventEnvelope<'ConfirmEvent', { prompt: string; choices: string[]; choice: string; source: 'user' | 'auto'; message?: string | null }>
  | EventEnvelope<'WarningEvent', { message: string }>
  | EventEnvelope<'ErrorEvent', { message: string }>

export type ModelMessageDisplayEvent = Extract<DisplayEvent, { name: 'ModelMessageEvent' }>
export type ConfirmDisplayEvent = Extract<DisplayEvent, { name: 'ConfirmEvent' }>

export type ToolItem = { key: string; call: ToolCallDisplayEvent; result?: ToolResultDisplayEvent }

export interface PendingPrompt {
  id: string
  agent_id: string | null
  prompt: string
  choices: string[]
  message?: string
  title?: string
  subtitle?: string
  default?: string
  allow_extra?: boolean
}

export interface FileEntry {
  name: string
  path: string
  kind: 'file' | 'directory'
  size: number | null
  media_type: string | null
}

export interface FileListing {
  path: string
  entries: FileEntry[]
}

export interface WebConfig {
  expose_files: boolean
  sessions_api: string
}

export type SessionStatus = 'idle' | 'running' | 'waiting'

export interface SessionInfo {
  path: string
  name: string
  status: SessionStatus
}

export interface SessionList {
  sessions: SessionInfo[]
  can_manage: boolean
}

export interface ModelCapabilities {
  model: string
  capabilities: Array<'vision'>
}

export type ServerMessage = DisplayEvent
  | { type: 'pending_prompt'; data: PendingPrompt }
  | { type: 'prompt_resolved'; prompt_id: string }
  | { type: 'execution_state'; agent_id: string; running: boolean }

export type ClientMessage =
  | { type: 'message'; agent_id: string; content: string; images: ImageDescriptor[] }
  | { type: 'command'; agent_id: string; name: string; arguments: string | null }
  | { type: 'cancel'; agent_id: string }
