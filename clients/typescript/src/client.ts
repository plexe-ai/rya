/**
 * `RyaClient` — the whole HTTP surface a client repo is allowed to touch.
 *
 * PLATFORM_DESIGN §2: "a client repo needs `rya-sdk` and a deploy token. It
 * never imports the runtime, never runs a server, and never knows which
 * deployment it is running in." So this client sends events, reads runs and
 * traces, resolves approvals, drives the durable job API, and inspects
 * deployment state. It does not define agents, does not hand you a `ctx`, and
 * does not execute a governed run — §3's boundary puts all of that in Python,
 * and a TypeScript port of it would be a second runtime to keep honest.
 *
 * Every method below corresponds to a route that exists in `src/rya/api/app.py`.
 */

import { RyaError } from "./errors.js";
import { Transport, type TransportOptions } from "./http.js";
import { streamEvent as streamEventFrames, streamTurn as streamTurnFrames, type StreamOptions } from "./turns.js";
import type {
  Approval,
  ApprovalResolution,
  ApprovalStatus,
  ClaimOptions,
  Connection,
  EnqueueJobOptions,
  Environment,
  EnvironmentHistoryEntry,
  EnvironmentStatus,
  FileMeta,
  FileUploadResult,
  GuardState,
  Health,
  HeartbeatResult,
  KnowledgeHit,
  ModelInfo,
  PolicyLogEntry,
  PresignedUpload,
  QueueJob,
  QueueJobStatus,
  QueueStats,
  Run,
  RunStatus,
  RunSummary,
  ServiceInfo,
  Session,
  SessionMessage,
  ToolInfo,
  ToolPermission,
  TraceEvent,
  TurnFrame,
  TurnHandle,
  UsageBuckets,
  UsageTotals,
  Version,
  VersionState,
  Worker,
} from "./types.js";

/** Per-call cancellation and timeout, accepted by every method. */
export interface CallOptions {
  signal?: AbortSignal;
  /** Overrides the client default for this call; `0` disables the timeout. */
  timeoutMs?: number;
}

export interface RyaClientOptions extends TransportOptions {
  /**
   * The agent path segment. Every `/agents/{agent_id}/…` route currently
   * IGNORES this value — one deployment serves one agent (D11) — so the default
   * `_` is correct today and this exists so a future multi-agent deployment does
   * not need a new client.
   */
  agentId?: string;
}

/** The manifest as `GET /agents/{id}` returns it: a `rya.agent.yaml` model dump. */
export interface AgentManifest {
  name: string;
  version?: string;
  owner?: string | null;
  description?: string | null;
  [key: string]: unknown;
}

export class RyaClient {
  private readonly http: Transport;
  private readonly agentId: string;

  constructor(opts: RyaClientOptions) {
    this.http = new Transport(opts);
    this.agentId = opts.agentId ?? "_";
  }

  /** The configured base URL, with any trailing slash removed. */
  get baseUrl(): string {
    return this.http.baseUrl;
  }

  private agentPath(suffix = ""): string {
    return `/agents/${encodeURIComponent(this.agentId)}${suffix}`;
  }

  // ---- service ------------------------------------------------------------

  health(opts?: CallOptions): Promise<Health> {
    return this.http.request<Health>("GET", "/healthz", opts);
  }

  /** Discovery: API, console, webhook, websocket and remote-MCP URLs. */
  info(opts?: CallOptions): Promise<ServiceInfo> {
    return this.http.request<ServiceInfo>("GET", "/v1/info", opts);
  }

  /** The deployed agent's manifest. */
  getAgent(opts?: CallOptions): Promise<AgentManifest> {
    return this.http.request<AgentManifest>("GET", this.agentPath(), opts);
  }

  // ---- events and runs ----------------------------------------------------

  /** Trigger a run by posting an event, and wait for it to settle. */
  triggerEvent(
    payload: Record<string, unknown>,
    type = "message.received",
    opts?: CallOptions
  ): Promise<RunSummary> {
    return this.sendEvent({ type, payload }, opts);
  }

  /** `triggerEvent` with the full body, including the `source` label. */
  sendEvent(
    event: { type?: string; payload: Record<string, unknown>; source?: string },
    opts?: CallOptions
  ): Promise<RunSummary> {
    return this.http.request<RunSummary>("POST", this.agentPath("/events"), {
      ...opts,
      body: { type: event.type ?? "message.received", payload: event.payload, source: event.source },
    });
  }

  /**
   * Post to the webhook trigger.
   *
   * Unsigned. When the deployment sets `RYA_WEBHOOK_SECRET` the request must
   * carry `X-Rya-Signature: sha256=HMAC_SHA256(body, secret)` computed over the
   * EXACT bytes sent — use {@link inboundRaw}, which lets you serialize and sign
   * the same string. The SDK does not compute the HMAC itself: that would mean a
   * crypto dependency (or `node:crypto`, which does not exist in a browser) for
   * a case only server-side callers hit.
   */
  inbound(payload: Record<string, unknown>, opts?: CallOptions): Promise<RunSummary> {
    return this.http.request<RunSummary>("POST", "/inbound", { ...opts, body: payload });
  }

  /** {@link inbound} over a pre-serialized body, so you can sign those bytes. */
  inboundRaw(
    body: string,
    init?: { signature?: string; eventType?: string } & CallOptions
  ): Promise<RunSummary> {
    const headers: Record<string, string> = { "content-type": "application/json" };
    if (init?.signature) headers["x-rya-signature"] = init.signature;
    if (init?.eventType) headers["x-rya-event-type"] = init.eventType;
    return this.http.request<RunSummary>("POST", "/inbound", {
      signal: init?.signal,
      timeoutMs: init?.timeoutMs,
      rawBody: body,
      headers,
    });
  }

  getRun(runId: string, opts?: CallOptions): Promise<Run> {
    return this.http.request<Run>("GET", `/runs/${encodeURIComponent(runId)}`, opts);
  }

  getTrace(runId: string, opts?: CallOptions): Promise<{ runId: string; trace: TraceEvent[] }> {
    return this.http.request("GET", `/runs/${encodeURIComponent(runId)}/trace`, opts);
  }

  listRuns(opts?: CallOptions): Promise<Run[]> {
    return this.http
      .request<{ runs: Run[] }>("GET", this.agentPath("/runs"), opts)
      .then((r) => r.runs);
  }

  /**
   * File a run that executed OUTSIDE Rya so its trace sits next to native ones.
   *
   * The single-pane step of a sidecar migration, and legitimately a TS surface:
   * the caller mapping its events onto Rya's trace vocabulary is exactly the
   * foreign agent loop this exists for. It is NOT a governed run — nothing was
   * authorized, journaled or guarded — and the caller must scrub PII first.
   */
  ingestRun(
    body: {
      status: RunStatus;
      trace: Array<{ kind: string; label?: string; ts?: string; data?: Record<string, unknown> }>;
      trigger?: string;
      event?: Record<string, unknown>;
      error?: Record<string, unknown>;
      createdAt?: string;
      source?: string;
      agentVersion?: string;
    },
    opts?: CallOptions
  ): Promise<{ ok: boolean; runId: string; events: number }> {
    return this.http.request("POST", "/runs/ingest", { ...opts, body });
  }

  // ---- approvals ----------------------------------------------------------

  listApprovals(status?: ApprovalStatus, opts?: CallOptions): Promise<Approval[]> {
    return this.http
      .request<{ approvals: Approval[] }>("GET", "/approvals", { ...opts, query: { status } })
      .then((r) => r.approvals);
  }

  /**
   * Approve a paused run.
   *
   * For a turn-bound run the continuation streams onto the ORIGINAL turn's
   * buffer, which is why the response carries `turnId`: keep tailing that turn
   * (or use `untilFinal: true`) rather than starting a new stream.
   */
  approve(approvalId: string, opts?: CallOptions): Promise<ApprovalResolution> {
    return this.http.request("POST", `/approvals/${encodeURIComponent(approvalId)}/approve`, opts);
  }

  reject(approvalId: string, opts?: CallOptions): Promise<ApprovalResolution> {
    return this.http.request("POST", `/approvals/${encodeURIComponent(approvalId)}/reject`, opts);
  }

  // ---- durable turns and streaming (D6) -----------------------------------

  /**
   * Start a durable chat turn.
   *
   * The turn is a leased, reclaimable queue job, so it survives the executor
   * crashing, and its frames land in a store-backed buffer, so the stream
   * survives the socket dropping. Prefer this over {@link streamEvent} when you
   * need the resume handle BEFORE any frames arrive.
   */
  startTurn(
    payload: Record<string, unknown>,
    type = "message.received",
    opts?: CallOptions
  ): Promise<TurnHandle> {
    return this.http.request<TurnHandle>("POST", this.agentPath("/turns"), {
      ...opts,
      body: { type, payload },
    });
  }

  /**
   * Tail a durable turn, resuming across dropped connections.
   *
   * Reconnects send `Last-Event-ID` with the sequence of the last frame you
   * actually received, so nothing is replayed and nothing is skipped. Ends on
   * the terminal `run`/`error` frame; throws `RyaStreamError` (carrying the
   * cursor) if the connection cannot be re-established.
   *
   * ```ts
   * for await (const f of rya.streamTurn(turnId, { untilFinal: true })) {
   *   if (isFrame(f, "token")) process.stdout.write(f.data.text);
   * }
   * ```
   *
   * The legacy `(turnId, afterSeq, opts)` form is still accepted.
   */
  streamTurn(turnId: string, opts?: StreamOptions): AsyncGenerator<TurnFrame, void, void>;
  streamTurn(
    turnId: string,
    afterSeq: number,
    opts?: StreamOptions
  ): AsyncGenerator<TurnFrame, void, void>;
  streamTurn(
    turnId: string,
    second?: number | StreamOptions,
    third?: StreamOptions
  ): AsyncGenerator<TurnFrame, void, void> {
    const opts: StreamOptions =
      typeof second === "number" ? { lastEventId: second, ...third } : (second ?? {});
    return streamTurnFrames(this.http, this.agentId, turnId, opts);
  }

  /**
   * Trigger a run and stream it in one call.
   *
   * Yields `turn` (the resume handle) first, then `token` / `trace` / `message` /
   * `ui` frames, then a terminal `run` or `error`. If the connection drops — or
   * the server closes at an approval pause with `untilFinal: true` — it
   * transparently continues on the durable tail from the same cursor.
   */
  streamEvent(
    payload: Record<string, unknown>,
    type = "message.received",
    opts?: StreamOptions
  ): AsyncGenerator<TurnFrame, void, void> {
    return streamEventFrames(this.http, this.agentId, { type, payload }, opts ?? {});
  }

  /**
   * Run any pending or lease-expired turns for this workspace.
   *
   * The durability backstop. `rya serve` sweeps on its own in single-tenant
   * mode; a hosted multi-tenant deployment executes no handler code in the API
   * process, so this is the hook for an external (e.g. TypeScript) cron.
   */
  reclaimTurns(opts?: CallOptions): Promise<{ reclaimed: string[]; count: number }> {
    return this.http.request("POST", this.agentPath("/turns/reclaim"), opts);
  }

  // ---- sessions: the durable transcript -----------------------------------

  listSessions(opts?: CallOptions): Promise<Session[]> {
    return this.http
      .request<{ sessions: Session[] }>("GET", "/sessions", opts)
      .then((r) => r.sessions);
  }

  /** Resolve a session by identity — how a chat UI finds its thread after reload. */
  findSession(channel: string, externalId: string, opts?: CallOptions): Promise<Session | null> {
    return this.http
      .request<Session>("GET", "/sessions/find", { ...opts, query: { channel, externalId } })
      .catch(notFoundToNull);
  }

  getSession(sessionId: string, opts?: CallOptions): Promise<Session | null> {
    return this.http
      .request<Session>("GET", `/sessions/${encodeURIComponent(sessionId)}`, opts)
      .catch(notFoundToNull);
  }

  /** The stored transcript, oldest first. Render it; never replay a typewriter. */
  listMessages(sessionId: string, limit?: number, opts?: CallOptions): Promise<SessionMessage[]> {
    return this.http
      .request<{ messages: SessionMessage[] }>(
        "GET",
        `/sessions/${encodeURIComponent(sessionId)}/messages`,
        { ...opts, query: { limit } }
      )
      .then((r) => r.messages);
  }

  // ---- files --------------------------------------------------------------

  /**
   * Upload bytes and (by default) fire a `file.uploaded` event at the agent, so
   * a workflow waiting on a document resumes.
   */
  uploadFile(
    name: string,
    data: BodyInit,
    init?: {
      contentType?: string;
      /** Becomes the file's tags, sent as repeated `tag.<key>` query params. */
      tags?: Record<string, string>;
      /** `false` stores the file without notifying the agent. */
      event?: boolean;
    } & CallOptions
  ): Promise<FileUploadResult> {
    const query: Record<string, string> = { name };
    for (const [k, v] of Object.entries(init?.tags ?? {})) query[`tag.${k}`] = v;
    if (init?.event === false) query["event"] = "false";
    return this.http.request<FileUploadResult>("POST", "/files", {
      signal: init?.signal,
      timeoutMs: init?.timeoutMs,
      query,
      rawBody: data,
      headers: { "content-type": init?.contentType ?? "application/octet-stream" },
    });
  }

  /** Large files: register metadata, PUT the bytes straight to S3, then confirm. */
  presignFile(
    init: { name: string; contentType?: string; tags?: Record<string, string> },
    opts?: CallOptions
  ): Promise<PresignedUpload> {
    return this.http.request<PresignedUpload>("POST", "/files/presign", { ...opts, body: init });
  }

  /** After a presigned PUT: verify the object landed and fire `file.uploaded`. */
  confirmFile(
    fileId: string,
    opts?: CallOptions
  ): Promise<{ ok: boolean; runId: string; runStatus: RunStatus; size: number }> {
    return this.http.request("POST", `/files/${encodeURIComponent(fileId)}/confirm`, opts);
  }

  listFiles(opts?: CallOptions): Promise<FileMeta[]> {
    return this.http.request<{ files: FileMeta[] }>("GET", "/files", opts).then((r) => r.files);
  }

  getFile(fileId: string, opts?: CallOptions): Promise<FileMeta | null> {
    return this.http
      .request<FileMeta>("GET", `/files/${encodeURIComponent(fileId)}`, opts)
      .catch(notFoundToNull);
  }

  // ---- knowledge ----------------------------------------------------------

  knowledge(opts?: CallOptions): Promise<{ documents: unknown[]; chunks: number }> {
    return this.http.request("GET", "/knowledge", opts);
  }

  searchKnowledge(query: string, limit = 5, opts?: CallOptions): Promise<KnowledgeHit[]> {
    return this.http
      .request<{ query: string; hits: KnowledgeHit[] }>("POST", "/knowledge/search", {
        ...opts,
        body: { query, limit },
      })
      .then((r) => r.hits);
  }

  // ---- governance: tools, guard -------------------------------------------

  /** Declared vs. effective permission per tool — the kill-switch view. */
  listTools(opts?: CallOptions): Promise<ToolInfo[]> {
    return this.http.request<{ tools: ToolInfo[] }>("GET", "/tools", opts).then((r) => r.tools);
  }

  /**
   * Kill switch: override a tool's permission NOW, without a redeploy.
   *
   * Written to privileged policy state (§11.2), so the change is versioned and
   * attributed — and the bundle whose tool is being killed cannot write it back.
   */
  setToolPermission(
    toolId: string,
    permission: ToolPermission,
    reason?: string,
    opts?: CallOptions
  ): Promise<ToolPermissionChange> {
    return this.http.request("PUT", `/tools/${encodeURIComponent(toolId)}/permission`, {
      ...opts,
      body: { permission, reason },
    });
  }

  /** Drop the override and fall back to the manifest's declared permission. */
  clearToolPermission(
    toolId: string,
    reason?: string,
    opts?: CallOptions
  ): Promise<ToolPermissionChange> {
    return this.http.request("PUT", `/tools/${encodeURIComponent(toolId)}/permission`, {
      ...opts,
      body: { clear: true, reason },
    });
  }

  /** Who changed which kill switch, when, and what it was before. */
  toolLog(limit = 50, opts?: CallOptions): Promise<PolicyLogEntry[]> {
    return this.http
      .request<{ entries: PolicyLogEntry[] }>("GET", "/tools/log", { ...opts, query: { limit } })
      .then((r) => r.entries);
  }

  /** The egress policy and its test scores. */
  getGuard(opts?: CallOptions): Promise<GuardState> {
    return this.http.request<GuardState>("GET", "/guard", opts);
  }

  putGuard(
    policy: Record<string, unknown>,
    opts?: CallOptions
  ): Promise<{ ok: boolean; tests: unknown; version?: number; record?: PolicyLogEntry }> {
    return this.http.request("PUT", "/guard", { ...opts, body: { policy } });
  }

  testGuard(opts?: CallOptions): Promise<unknown> {
    return this.http.request("POST", "/guard/test", opts);
  }

  /** The policy audit trail: every change, its diff, and who made it. */
  guardLog(limit = 50, opts?: CallOptions): Promise<PolicyLogEntry[]> {
    return this.http
      .request<{ entries: PolicyLogEntry[] }>("GET", "/guard/log", { ...opts, query: { limit } })
      .then((r) => r.entries);
  }

  // ---- usage (D10) ---------------------------------------------------------

  /**
   * Billable facts from the durable meter — NOT summed from run traces, which
   * is why a replayed run does not double-bill.
   */
  getUsage(
    range?: { since?: string; until?: string } & CallOptions
  ): Promise<UsageTotals> {
    return this.http
      .request<{ usage: UsageTotals }>("GET", "/usage", {
        signal: range?.signal,
        timeoutMs: range?.timeoutMs,
        query: { since: range?.since, until: range?.until },
      })
      .then((r) => r.usage);
  }

  /** {@link getUsage} bucketed by a meter field: `model`, `agent`, `agentVersion`, `kind`. */
  getUsageBy(
    groupBy: "model" | "agent" | "agentVersion" | "kind" | (string & {}),
    range?: { since?: string; until?: string } & CallOptions
  ): Promise<UsageBuckets> {
    return this.http
      .request<{ usage: UsageBuckets }>("GET", "/usage", {
        signal: range?.signal,
        timeoutMs: range?.timeoutMs,
        query: { since: range?.since, until: range?.until, group_by: groupBy },
      })
      .then((r) => r.usage);
  }

  // ---- deployments: versions, environments, workers (D11, D12, §9) --------

  listVersions(state?: VersionState, opts?: CallOptions): Promise<Version[]> {
    return this.http
      .request<{ versions: Version[] }>("GET", this.agentPath("/versions"), {
        ...opts,
        query: { state },
      })
      .then((r) => r.versions);
  }

  getVersion(versionId: string, opts?: CallOptions): Promise<Version> {
    return this.http.request<Version>("GET", `/versions/${encodeURIComponent(versionId)}`, opts);
  }

  /** Why a retire was refused: the non-terminal runs still pinned to a version. */
  pinnedRuns(versionId: string, opts?: CallOptions): Promise<{ runs: Run[]; count: number }> {
    return this.http.request("GET", `/versions/${encodeURIComponent(versionId)}/pinned-runs`, opts);
  }

  /**
   * Retire a version: no new runs, no promotion, artifact still retained.
   *
   * Fails closed with `E_VERSION_IN_USE` while it is an environment's pointer or
   * any live run is pinned to it. `force` is the operator override and is not
   * free — a pinned run that later resumes may fail closed on a missing artifact.
   */
  retireVersion(versionId: string, force = false, opts?: CallOptions): Promise<Version> {
    return this.http.request<Version>("POST", `/versions/${encodeURIComponent(versionId)}/retire`, {
      ...opts,
      body: { force },
    });
  }

  listEnvironments(opts?: CallOptions): Promise<Environment[]> {
    return this.http
      .request<{ environments: Environment[] }>("GET", this.agentPath("/environments"), opts)
      .then((r) => r.environments);
  }

  describeEnvironment(environment: string, opts?: CallOptions): Promise<EnvironmentStatus> {
    return this.http.request<EnvironmentStatus>(
      "GET",
      this.agentPath(`/environments/${encodeURIComponent(environment)}`),
      opts
    );
  }

  /**
   * Point an environment at a version. Atomic (§9): new runs go to the new
   * version, in-flight runs finish on the one they pinned.
   */
  promote(environment: string, versionId: string, opts?: CallOptions): Promise<Environment> {
    return this.http.request<Environment>(
      "POST",
      this.agentPath(`/environments/${encodeURIComponent(environment)}/promote`),
      { ...opts, body: { versionId } }
    );
  }

  /** Rollback is a pointer flip. Defaults to the previous version in history. */
  rollback(environment: string, versionId?: string, opts?: CallOptions): Promise<Environment> {
    return this.http.request<Environment>(
      "POST",
      this.agentPath(`/environments/${encodeURIComponent(environment)}/rollback`),
      { ...opts, body: versionId ? { versionId } : {} }
    );
  }

  environmentHistory(
    environment: string,
    opts?: CallOptions
  ): Promise<EnvironmentHistoryEntry[]> {
    return this.http
      .request<{ history: EnvironmentHistoryEntry[] }>(
        "GET",
        this.agentPath(`/environments/${encodeURIComponent(environment)}/history`),
        opts
      )
      .then((r) => r.history);
  }

  /** Which execution-plane processes are live, on which version (§6). */
  listWorkers(
    filter?: { status?: string; versionId?: string } & CallOptions
  ): Promise<Worker[]> {
    return this.http
      .request<{ workers: Worker[] }>("GET", "/workers", {
        signal: filter?.signal,
        timeoutMs: filter?.timeoutMs,
        query: { status: filter?.status ?? "alive", version_id: filter?.versionId },
      })
      .then((r) => r.workers);
  }

  // ---- reference lists ----------------------------------------------------

  /** Connection metadata only — the store never returns secret values. */
  listConnections(opts?: CallOptions): Promise<Connection[]> {
    return this.http
      .request<{ connections: Connection[] }>("GET", "/connections", opts)
      .then((r) => r.connections);
  }

  listModels(opts?: CallOptions): Promise<ModelInfo[]> {
    return this.http.request<{ models: ModelInfo[] }>("GET", "/models", opts).then((r) => r.models);
  }

  listChannels(opts?: CallOptions): Promise<Array<Record<string, unknown>>> {
    return this.http
      .request<{ channels: Array<Record<string, unknown>> }>("GET", "/channels", opts)
      .then((r) => r.channels);
  }

  // ---- queue: durable jobs run by YOUR workers (D14) ----------------------

  /** Enqueue one job. `jobId` is an idempotency key: re-enqueueing returns it. */
  enqueueJob(
    type: string,
    payload: unknown,
    init?: EnqueueJobOptions & CallOptions
  ): Promise<QueueJob> {
    const { signal, timeoutMs, ...job } = init ?? {};
    return this.http
      .request<{ job: QueueJob }>("POST", "/queue/jobs", {
        signal,
        timeoutMs,
        body: { type, payload, ...job },
      })
      .then((r) => r.job);
  }

  /** Enqueue many; dispatch preserves input order. Returns the ids, in order. */
  enqueueJobBatch(
    type: string,
    items: Array<{ payload: unknown } & EnqueueJobOptions>,
    opts?: CallOptions
  ): Promise<string[]> {
    return this.http
      .request<{ ids: string[] }>("POST", "/queue/jobs/batch", { ...opts, body: { type, items } })
      .then((r) => r.ids);
  }

  /** {@link enqueueJobBatch}, returning the full records instead of ids. */
  enqueueJobs(
    type: string,
    items: Array<{ payload: unknown } & EnqueueJobOptions>,
    opts?: CallOptions
  ): Promise<QueueJob[]> {
    return this.http
      .request<{ jobs: QueueJob[] }>("POST", "/queue/jobs/batch", { ...opts, body: { type, items } })
      .then((r) => r.jobs);
  }

  /** `null` rather than a throw for an unknown id — polling stale state is normal. */
  getQueueJob(jobId: string, opts?: CallOptions): Promise<QueueJob | null> {
    return this.http
      .request<{ job: QueueJob }>("GET", `/queue/jobs/${encodeURIComponent(jobId)}`, opts)
      .then((r) => r.job)
      .catch(notFoundToNull);
  }

  listQueueJobs(
    filter?: { status?: QueueJobStatus; type?: string } & CallOptions
  ): Promise<QueueJob[]> {
    return this.http
      .request<{ jobs: QueueJob[] }>("GET", "/queue/jobs", {
        signal: filter?.signal,
        timeoutMs: filter?.timeoutMs,
        query: { status: filter?.status, type: filter?.type },
      })
      .then((r) => r.jobs);
  }

  /**
   * Cancel a job. Pending cancels immediately; a running job gets
   * `cancelRequested` and stops when its worker next heartbeats, unless `force`.
   * Unknown ids resolve quietly (`found: false`).
   */
  cancelQueueJob(
    jobId: string,
    force = false,
    opts?: CallOptions
  ): Promise<{ ok: boolean; found: boolean; jobId: string; status?: QueueJobStatus; cancelRequested?: boolean }> {
    return this.http.request("POST", `/queue/jobs/${encodeURIComponent(jobId)}/cancel`, {
      ...opts,
      body: { force },
    });
  }

  /** Requeue a dead-lettered or cancelled job with a fresh attempt budget. */
  retryQueueJob(jobId: string, opts?: CallOptions): Promise<QueueJob> {
    return this.http
      .request<{ job: QueueJob }>("POST", `/queue/jobs/${encodeURIComponent(jobId)}/retry`, opts)
      .then((r) => r.job);
  }

  queueStats(opts?: CallOptions): Promise<QueueStats> {
    return this.http.request<QueueStats>("GET", "/queue/stats", opts);
  }

  /** Worker-side: claim due jobs with a lease. `waitSeconds` long-polls. */
  claimQueueJobs(claim: ClaimOptions, opts?: CallOptions): Promise<QueueJob[]> {
    return this.http
      .request<{ jobs: QueueJob[] }>("POST", "/queue/claim", { ...opts, body: claim })
      .then((r) => r.jobs);
  }

  /** Worker-side: extend the lease. A 409 means the lease was lost — stop. */
  heartbeatQueueJob(
    jobId: string,
    workerId: string,
    extendSeconds = 60,
    opts?: CallOptions
  ): Promise<HeartbeatResult> {
    return this.http.request("POST", `/queue/jobs/${encodeURIComponent(jobId)}/heartbeat`, {
      ...opts,
      body: { workerId, extendSeconds },
    });
  }

  completeQueueJob(
    jobId: string,
    workerId: string,
    output?: unknown,
    opts?: CallOptions
  ): Promise<QueueJob> {
    return this.http
      .request<{ job: QueueJob }>("POST", `/queue/jobs/${encodeURIComponent(jobId)}/complete`, {
        ...opts,
        body: { workerId, output: output ?? null },
      })
      .then((r) => r.job);
  }

  failQueueJob(
    jobId: string,
    workerId: string,
    error: string,
    opts?: CallOptions
  ): Promise<QueueJob> {
    return this.http
      .request<{ job: QueueJob }>("POST", `/queue/jobs/${encodeURIComponent(jobId)}/fail`, {
        ...opts,
        body: { workerId, error },
      })
      .then((r) => r.job);
  }
}

/** `PUT /tools/{id}/permission` — the kill-switch audit record. */
export interface ToolPermissionChange {
  ok: boolean;
  tool: string;
  permission: ToolPermission;
  previous: ToolPermission;
  cleared: boolean;
  reason?: string | null;
  version?: number;
  actor?: string | null;
  ts: string;
}

/** 404 → `null`. Any other failure still throws, so a typo is not silence. */
function notFoundToNull(err: unknown): null {
  if (err instanceof RyaError && err.httpStatus === 404) return null;
  throw err;
}
