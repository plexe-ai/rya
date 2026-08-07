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
  /**
   * `null` whenever the control plane holds no loaded agent module — which since
   * D21 is the NORMAL case for a published bundle, because the api never imports
   * one and therefore cannot know what the code registers. Three states, not two:
   * handler present, handler absent, and not introspected. Typing this as
   * non-nullable is what made the Overview crash on every published agent.
   */
  handlers: { event?: boolean } | null
}

/** One entry of the `agents` roster — what a workspace serves, without loading it. */
export interface AgentRef {
  name: string
  source?: string
  versionId?: string
  bundleHash?: string
  environment?: string | null
  declaredBy?: string
  manifestAvailable?: boolean
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

/**
 * The aggregate served by `GET /console` **when an agent is selected**.
 *
 * Every view requires a selected agent, so `agent` is non-nullable here on purpose
 * and the shell narrows to this type before rendering one. The agent-less response
 * is a different shape — see `ConsoleRoster`.
 */
export interface ConsoleState {
  agent: Agent
  agents?: AgentRef[]
  selectedAgent?: string | null
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

/**
 * What `GET /console` returns when NO agent is selected — a real state the route
 * documents in words: "a fresh workspace with nothing published yet still has a
 * dashboard". It arrives in two situations, and the shell tells them apart:
 *
 *  - `agents: []`     nothing published yet
 *  - `agents.length > 1` and nothing chosen — the server only auto-selects when a
 *                     workspace serves exactly one agent
 *
 * None of the agent-scoped fields (`tools`, `stats`, `runs`, …) are present, which
 * is why this is a separate type rather than `ConsoleState` with optional members:
 * a view that touched them would compile and then throw.
 */
export interface ConsoleRoster {
  ok?: boolean
  agent: null
  agents: AgentRef[]
  selectedAgent?: string | null
  viewer?: Viewer
  branding?: Branding | null
  infra?: unknown
}

export type ConsoleResponse = ConsoleState | ConsoleRoster

/** Narrows the poll's payload to the shape every view needs. */
export function hasAgent(r: ConsoleResponse | null | undefined): r is ConsoleState {
  return !!r && r.agent !== null
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
