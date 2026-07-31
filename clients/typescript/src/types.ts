/**
 * Wire types, mirrored from what `src/rya/api/app.py` actually returns.
 *
 * Every shape here was read off a route handler or the store record it hands
 * back — none are aspirational. Records the server may extend keep an index
 * signature or optional fields rather than a closed shape, because a client that
 * fails to compile when the platform adds a column is not a stable contract.
 */

import type { RyaErrorBody } from "./errors.js";

// ---- runs ------------------------------------------------------------------

export type RunStatus =
  | "running"
  | "waiting_approval"
  | "completed"
  | "failed"
  | "rejected"
  | "needs_reconnect";

/** A verified caller identity (`auth.py`), echoed by the event routes. */
export interface Identity {
  sub: string;
  email?: string | null;
  [key: string]: unknown;
}

/** `POST /agents/{id}/events` and `POST /inbound`. Note `runId`, not `id`. */
export interface RunSummary {
  runId: string;
  status: RunStatus;
  pendingApproval?: string | null;
  identity?: Identity | null;
}

/** One journaled step. `kind` is open (`tool.call`, `llm.respond`, `log`, …). */
export interface TraceEvent {
  seq: number;
  ts: string;
  kind: string;
  label: string;
  data: Record<string, unknown>;
}

/**
 * A run record, as `GET /runs/{id}` returns it (the raw store document).
 *
 * `versionId`/`bundleHash` are the code identity a replay is checked against
 * (D12); `agentVersion` is the author-typed manifest label and nothing should
 * branch on it.
 */
export interface Run {
  id: string;
  agent: string;
  agentVersion: string;
  versionId?: string | null;
  bundleHash?: string | null;
  sdkVersion?: string | null;
  environment?: string | null;
  status: RunStatus;
  trigger: string;
  trace: TraceEvent[];
  event?: { type?: string; payload?: Record<string, unknown> } | null;
  pendingApproval?: string | null;
  error?: RyaErrorBody | null;
  parentRunId?: string | null;
  turnId?: string | null;
  createdAt?: string;
  updatedAt?: string;
  [key: string]: unknown;
}

/** The payload of the terminal `run` frame (`turns.py:_summary`) — `id`, not `runId`. */
export interface TurnRunSummary {
  id: string;
  status: RunStatus;
  trigger?: string | null;
  pendingApproval?: string | null;
  error?: RyaErrorBody | null;
  traceLength: number;
  tokens: number;
  costUsd?: number | null;
}

/** Statuses at which a run is done. Mirrors `turns.TERMINAL_RUN_STATUSES`. */
export const TERMINAL_RUN_STATUSES = ["completed", "failed", "rejected"] as const;

// ---- approvals -------------------------------------------------------------

export type ApprovalStatus = "pending" | "approved" | "rejected";

export interface Approval {
  id: string;
  runId: string;
  status: ApprovalStatus;
  title: string;
  body: string;
  action?: Record<string, unknown>;
  createdAt?: string;
  resolvedAt?: string | null;
  actionResult?: unknown;
}

/** `POST /approvals/{id}/approve|reject`. `turnId` is set for turn-bound runs. */
export interface ApprovalResolution {
  approvalId: string;
  runStatus: RunStatus;
  turnId?: string | null;
}

// ---- durable turns (D6) ----------------------------------------------------

/** `POST /agents/{id}/turns`. The turn is a leased queue job, hence `status`. */
export interface TurnHandle {
  turnId: string;
  status: QueueJobStatus;
}

/**
 * One frame off a durable turn stream.
 *
 * `data` is `unknown` rather than a per-kind union because frame kinds are
 * ADDITIVE — `ui` was added after `token`/`trace`/`message`/`run`, and a closed
 * union would have made that a compile error in every deployed client. Narrow
 * with {@link isFrame}, which restores full typing per kind.
 */
export interface TurnFrame {
  /** The SSE `event:` field — the store's frame `kind`. */
  event: string;
  /** The SSE `id:` field, i.e. the buffer sequence, as a string. */
  id: string | null;
  /** {@link id} parsed, or `null` for frames the server sends without one. */
  seq: number | null;
  data: unknown;
}

/** Payload shapes for the frame kinds the runtime emits today. */
export interface TurnFrameData {
  /** First frame of `streamEvent`: the handle to resume with. */
  turn: { turnId: string };
  /** A streamed LLM chunk. Not journaled, so a replay never re-streams it. */
  token: { text: string };
  trace: TraceEvent;
  message: SessionMessage;
  /** Generative-UI frame; shape is agent-defined. */
  ui: Record<string, unknown>;
  /** Appended when a crashed turn is reclaimed and re-run. */
  restart: { attempt: number };
  /** `waiting_approval` here is a PAUSE marker, not the end of the turn. */
  run: TurnRunSummary;
  error: RyaErrorBody;
}

/** Narrow a frame to a known kind: `if (isFrame(f, "token")) f.data.text`. */
export function isFrame<K extends keyof TurnFrameData>(
  frame: TurnFrame,
  kind: K
): frame is TurnFrame & { event: K; data: TurnFrameData[K] } {
  return frame.event === kind;
}

// ---- sessions --------------------------------------------------------------

export interface Session {
  id: string;
  agent: string;
  channel: string;
  externalId: string;
  title?: string | null;
  createdAt?: string;
  lastMessageAt?: string | null;
  [key: string]: unknown;
}

export interface SessionMessage {
  id: string;
  seq: number;
  role: "user" | "assistant" | "system" | (string & {});
  content: string;
  ts: string;
  [key: string]: unknown;
}

// ---- files -----------------------------------------------------------------

export interface FileMeta {
  id: string;
  name: string;
  contentType: string;
  size: number;
  sha256: string;
  tags: Record<string, string>;
  createdAt: string;
  storage?: "s3";
}

/** `POST /files`. `runId` is absent when uploaded with `event: false`. */
export interface FileUploadResult {
  ok: boolean;
  file: FileMeta;
  runId?: string;
  runStatus?: RunStatus;
}

export interface PresignedUpload {
  ok: boolean;
  fileId: string;
  /** PUT the bytes here directly, then call `confirmFile(fileId)`. */
  uploadUrl: string;
}

// ---- queue (D14: SDK-free durable jobs) ------------------------------------

export type QueueJobStatus = "pending" | "running" | "completed" | "failed" | "cancelled";

export interface QueueJob {
  id: string;
  type: string;
  payload: unknown;
  status: QueueJobStatus;
  attempts: number;
  maxAttempts: number;
  priority: number;
  runAt: string;
  seq: number;
  tags: string[];
  metadata: Record<string, unknown>;
  concurrencyKey: string | null;
  concurrencyLimit: number | null;
  retryDelaySeconds: number | null;
  workerId: string | null;
  leaseExpiresAt: string | null;
  /** Set by a graceful cancel; the worker sees it on its next heartbeat. */
  cancelRequested: boolean;
  /** True once the attempt budget is exhausted. */
  deadLetter: boolean;
  output: unknown;
  error: string | null;
  lastError: string | null;
  createdAt: string;
  startedAt: string | null;
  completedAt: string | null;
}

export interface EnqueueJobOptions {
  /** Doubles as the idempotency key: re-enqueueing an id returns the existing job. */
  jobId?: string;
  maxAttempts?: number;
  delaySeconds?: number;
  priority?: number;
  tags?: string[];
  metadata?: Record<string, unknown>;
  concurrencyKey?: string;
  concurrencyLimit?: number;
  retryDelaySeconds?: number;
}

export interface ClaimOptions {
  workerId: string;
  types?: string[];
  limit?: number;
  leaseSeconds?: number;
  /** Server-side long poll, capped at 25s by the API. */
  waitSeconds?: number;
}

export interface HeartbeatResult {
  ok: boolean;
  leaseExpiresAt: string;
  /** Stop and report when true — someone cancelled the job. */
  cancelRequested: boolean;
}

export interface CancelResult {
  ok: boolean;
  found: boolean;
  jobId: string;
  status?: QueueJobStatus;
  cancelRequested?: boolean;
}

export interface QueueStats {
  counts: Record<string, number>;
}

// ---- deployments: versions, environments, workers (D11, D12, §6) -----------

export type VersionState = "active" | "retired";

export interface Version {
  id: string;
  agent: string;
  /** The content hash. THIS is the code identity, not `manifestVersion`. */
  bundleHash: string;
  state: VersionState;
  sdkVersion?: string | null;
  entrypoint?: string | null;
  lockfile?: string | null;
  sizeBytes?: number;
  fileCount?: number;
  manifestVersion?: string | null;
  createdBy?: string | null;
  /** Provenance slot: git sha, CI run url, who built it. */
  metadata?: Record<string, unknown>;
  createdAt?: string;
  retiredAt?: string | null;
}

export interface Environment {
  name: string;
  agent: string;
  currentVersionId: string | null;
  history: Array<{ versionId: string; replacedAt: string; actor?: string | null }>;
  actor?: string | null;
  createdAt?: string;
  updatedAt?: string;
}

/** `GET /agents/{id}/environments/{env}` — pointer plus rollout drift. */
export interface EnvironmentStatus {
  name: string;
  agent: string;
  currentVersionId: string | null;
  currentVersion: Version | null;
  updatedAt?: string;
  actor?: string | null;
  historyDepth: number;
  /** versionId → count of non-terminal runs still pinned to an OLDER version. */
  pinnedRuns: Record<string, number>;
}

export interface EnvironmentHistoryEntry {
  versionId: string;
  bundleHash?: string | null;
  manifestVersion?: string | null;
  state?: VersionState;
  current: boolean;
  at?: string | null;
  actor?: string | null;
  version: Version | null;
}

export interface Worker {
  id: string;
  status: "alive" | (string & {});
  agent?: string;
  versionId?: string | null;
  bundleHash?: string | null;
  handlers?: string[];
  startedAt?: string;
  lastHeartbeatAt?: string;
  [key: string]: unknown;
}

// ---- usage (D10: the durable meter, not run traces) ------------------------

export interface UsageTotals {
  inputTokens: number;
  outputTokens: number;
  costUsd: number;
  calls: number;
}

/** With `groupBy`, `GET /usage` returns a bucket per distinct field value. */
export type UsageBuckets = Record<string, UsageTotals>;

// ---- governance: tools, guard ----------------------------------------------

export type ToolPermission = "auto" | "approval" | "never" | (string & {});

export interface ToolInfo {
  id: string;
  /** As declared in the manifest. */
  permission: ToolPermission;
  /** What the runtime will actually enforce, after any kill switch. */
  effectivePermission: ToolPermission;
  override: { permission: ToolPermission; ts: string; reason?: string | null } | null;
}

/** The audit record of a privileged policy write (guard or kill switch). */
export interface PolicyLogEntry {
  key: string;
  value: unknown;
  version: number;
  actor: string | null;
  previous: unknown;
  changedAt: string;
}

export interface GuardState {
  policy: Record<string, unknown>;
  tests: unknown;
  exists: boolean;
  [key: string]: unknown;
}

// ---- misc ------------------------------------------------------------------

export interface Health {
  ok: boolean;
  agent: string;
  authEnabled: boolean;
  multiTenant: boolean;
  /** The live store backend — how a deploy confirms it is on Postgres. */
  store: string;
}

/** `GET /v1/info` — discovery: how to reach this (possibly hosted) instance. */
export interface ServiceInfo {
  service: string;
  version: string;
  agent: string;
  multiTenant: boolean;
  authRequired: boolean;
  remoteMcp: string | null;
  api: string;
  console: string;
  webhook: string;
  websocket: string;
  provisionProjects: boolean;
}

export interface Connection {
  id: string;
  provider: string;
  owner?: string | null;
  scopes: string[];
  label: string;
  status: string;
  secretSet: boolean;
  encrypted: boolean;
  createdAt?: string;
}

export interface ModelInfo {
  id: string;
  type: string;
  permission: string;
}

export interface KnowledgeHit {
  text: string;
  source?: string | null;
  docId?: string | null;
  _score: number;
}
