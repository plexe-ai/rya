/**
 * `@plexe/rya` — the TypeScript client SDK for the Rya platform.
 *
 * Rya's runtime is Python. PLATFORM_DESIGN §3 draws the boundary at "platform
 * code vs. client code", and §2 states the client contract: **a client repo
 * needs `rya-sdk` and a deploy token. It never imports the runtime, never runs a
 * server, and never knows which deployment it is running in.** This package is
 * the TypeScript side of exactly that contract — an HTTP client, not a second
 * runtime. There is no `defineAgent`, no `ctx`, no tool registry here, because
 * governed execution (permissions, the journal, the guard verdict, approvals)
 * lives on the server and a TS reimplementation would be a second thing to keep
 * honest.
 *
 * What it does cover:
 *
 * - **The agent platform** — trigger events, read runs and traces, resolve
 *   approvals, read sessions, upload files, inspect usage, versions and
 *   environments.
 * - **Durable streaming (D6)** — every stream is a tail over the durable turn
 *   buffer, resumed by `Last-Event-ID`, so a dropped socket is a cursor rather
 *   than a lost turn.
 * - **The durable job API (D14)** — `/queue/*` is deliberately SDK-free so
 *   foreign code can drive it, and TypeScript DAG workers are the consumer the
 *   design names. `createQueueWorker` is the claim/heartbeat/report loop.
 *
 * ```ts
 * import { RyaClient, isFrame } from "@plexe/rya";
 *
 * const rya = new RyaClient({ baseUrl: "http://localhost:8787", token: process.env.RYA_TOKEN });
 * const { turnId } = await rya.startTurn({ email: "ada@example.com", body: "refund please" });
 *
 * for await (const frame of rya.streamTurn(turnId, { untilFinal: true })) {
 *   if (isFrame(frame, "token")) process.stdout.write(frame.data.text);
 *   if (isFrame(frame, "run") && frame.data.status === "waiting_approval") {
 *     const [pending] = await rya.listApprovals("pending");
 *     if (pending) await rya.approve(pending.id); // continuation lands on this same stream
 *   }
 * }
 * ```
 *
 * Zero runtime dependencies: global `fetch` and web streams, both built in from
 * Node 18. It runs unchanged in the browser, a worker, or an edge runtime.
 */

export { RyaError, RyaStreamError, EXIT } from "./errors.js";
export type { RyaErrorBody, RyaErrorCode } from "./errors.js";

export { RyaClient } from "./client.js";
export type { AgentManifest, CallOptions, RyaClientOptions, ToolPermissionChange } from "./client.js";

export type { RequestOptions, TransportOptions } from "./http.js";
export { Transport, buildQuery } from "./http.js";

export { readSse, parseData } from "./sse.js";
export type { SseEvent } from "./sse.js";

export { streamEvent, streamTurn } from "./turns.js";
export type { StreamOptions } from "./turns.js";

export { createQueueWorker } from "./queue.js";
export type {
  QueueJobHandler,
  QueueWorker,
  QueueWorkerClient,
  QueueWorkerOptions,
} from "./queue.js";

export { isFrame, TERMINAL_RUN_STATUSES } from "./types.js";
export type {
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
  Identity,
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
  TurnFrameData,
  TurnHandle,
  TurnRunSummary,
  UsageBuckets,
  UsageTotals,
  Version,
  VersionState,
  Worker,
} from "./types.js";

import type { TurnFrame } from "./types.js";

/**
 * @deprecated Use {@link TurnFrame}. Kept so existing imports keep compiling;
 * the only change is `data: any` → `data: unknown`, which is the point — a
 * frame payload has to be narrowed (see `isFrame`) rather than trusted.
 */
export type StreamFrame = TurnFrame;
