// Shapes returned by `GET /console` (see `rya/snapshot.py: build_console`) and the
// per-view endpoints. These are hand-written rather than generated: the console is
// the only consumer, and a mismatch here should surface as a type error at build
// time instead of `undefined` in a table cell at 3am.
//
// Everything the server may legitimately omit is optional. `build_console` fills
// most fields unconditionally, but a fresh install has empty collections
// everywhere (see console/AGENTS.md: "a fresh install has no versions, no
// environments and no workers, and all three are ordinary states").

export type Permission = 'allowed' | 'read_only' | 'approval_required' | 'disabled'

export type RunStatus =
  | 'completed'
  | 'waiting_approval'
  | 'failed'
  | 'rejected'
  | 'running'
  | (string & {})

export interface Agent {
  name: string
  version: string
  environment: string
  status: string
  runtime: string
  handlers: { event?: boolean }
}

export interface Runtime {
  store: string
  llmProvider: string
  multiTenant: boolean
}

export interface Stats {
  runs: number
  byStatus: Record<string, number>
  approvalsPending: number
  inputTokens: number
  outputTokens: number
  costUsd?: number | null
  jobsPending: number
  sessions?: number
  messages?: number
}

export interface Tool {
  id: string
  permission: Permission
  provider?: string | null
  description?: string
}

export interface Model {
  id: string
  type: string
  permission: Permission
  version?: string | null
  calls: number
}

export interface Channel {
  type: string
  path?: string | null
  enabled: boolean
}

export interface Run {
  id: string
  status: RunStatus
  trigger: string
  tokens?: number
  costUsd?: number | null
  createdAt?: string
  pendingApproval?: string | null
  error?: string | null
  traceLength?: number
}

export interface Approval {
  id: string
  title: string
  runId: string
  body?: string
  action?: { tool?: string; input?: Record<string, unknown> } | null
}

export interface TraceEvent {
  kind: string
  label?: string
  ts?: string
}

export interface Memory {
  blocks?: { name: string; chars: number; limit: number; updatedAt?: string }[]
  facts?: number
  collections: { name: string; count: number }[]
}

export interface Knowledge {
  documents: { id: string; title?: string; chunks?: number }[]
  chunks?: number
}

export interface Viewer {
  workspace?: string
  workspaceId?: string
  mode?: 'multi-tenant' | 'single-tenant' | string
  user?: string | null
}

export interface Branding {
  name: string
  tagline?: string
  logo?: string | null
}

export interface Trigger {
  id: string
  type: string
  schedule?: string
}

export interface Governance {
  enforcement: {
    egressGuard: boolean
    groundingGate: boolean
    approverIdentity: boolean
    perUserIdentity: boolean
    multiTenantRls: boolean
    secretsSealed: boolean
  }
  policy: {
    hash: string
    toolsGated: number
    toolsDenied: number
    pinnedArgTools: number
    egressRules: number
    egressDefault?: string | null
  }
  switches?: {
    active: { tool: string; permission: Permission; ts?: string; version: number }[]
    history: {
      ts?: string
      tool: string
      permission: Permission
      previous?: Permission
      cleared?: boolean
      reason?: string
    }[]
  }
  violations?: { ts?: string; kind: string; runId?: string; detail?: string }[]
}

/** The aggregate served by `GET /console`. */
export interface ConsoleState {
  agent: Agent
  runtime: Runtime
  stats: Stats
  tools: Tool[]
  models: Model[]
  channels: Channel[]
  runs: Run[]
  approvals: Approval[]
  memory: Memory
  knowledge?: Knowledge
  connections?: { id: string; provider: string; scopes?: string[] }[]
  secrets: string[]
  triggers: Trigger[]
  governance?: Governance
  manifestYaml?: string
  branding?: Branding | null
  viewer?: Viewer
  infra?: unknown
}

/** `GET /v1/info` — drives which auth tabs are offered. */
export interface RuntimeInfo {
  multiTenant?: boolean
  agent?: string
  version?: string
}

export interface Workspace {
  id: string
  name: string
  role?: string
}

export interface QueueCounts {
  pending?: number
  running?: number
  completed?: number
  failed?: number
  cancelled?: number
}
