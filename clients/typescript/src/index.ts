/**
 * Rya TypeScript client.
 *
 * A typed client for the Rya runtime HTTP API ({@link https://github.com/plexe-ai/rya}).
 * The Rya runtime itself is Python; this is the client a TS/JS app uses to trigger
 * agent runs, resolve approvals, and read traces. Uses the global `fetch` (Node 18+).
 *
 * ```ts
 * const rya = new RyaClient({ baseUrl: "http://localhost:8787", token: process.env.RYA_TOKEN });
 * const run = await rya.triggerEvent({ email: "ada@example.com" });
 * if (run.status === "waiting_approval") {
 *   const approvals = await rya.listApprovals("pending");
 *   await rya.approve(approvals[0]!.id);
 * }
 * ```
 */

export type RunStatus =
  | "running"
  | "waiting_approval"
  | "completed"
  | "failed"
  | "rejected";

export interface RunSummary {
  runId: string;
  status: RunStatus;
  pendingApproval?: string | null;
  identity?: { sub: string; email?: string | null } | null;
}

export interface TraceEvent {
  seq: number;
  ts: string;
  kind: string;
  label: string;
  data: Record<string, unknown>;
}

export interface Run {
  id: string;
  agent: string;
  agentVersion: string;
  status: RunStatus;
  trigger: string;
  trace: TraceEvent[];
  error?: { code: string; message: string } | null;
}

export interface Approval {
  id: string;
  status: "pending" | "approved" | "rejected";
  title: string;
  body: string;
  runId: string;
}

export interface RyaErrorBody {
  code: string;
  message: string;
  hint?: string | null;
  exit_code?: number;
}

export class RyaError extends Error {
  readonly code: string;
  readonly hint?: string | null;
  readonly httpStatus: number;
  constructor(status: number, body: RyaErrorBody) {
    super(`[${body.code}] ${body.message}`);
    this.name = "RyaError";
    this.code = body.code;
    this.hint = body.hint ?? null;
    this.httpStatus = status;
  }
}

export interface RyaClientOptions {
  baseUrl: string;
  /** Operator token (RYA_TOKEN), an API key (rya_sk_…), or a user JWT. */
  token?: string;
  /** Per-request timeout in ms (default 30s). */
  timeoutMs?: number;
  fetch?: typeof fetch;
}

export class RyaClient {
  private readonly baseUrl: string;
  private readonly token?: string;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: RyaClientOptions) {
    this.baseUrl = opts.baseUrl.replace(/\/$/, "");
    this.token = opts.token;
    this.timeoutMs = opts.timeoutMs ?? 30_000;
    const f = opts.fetch ?? globalThis.fetch;
    if (!f) throw new Error("No fetch available; pass options.fetch or use Node 18+.");
    this.fetchImpl = f;
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const headers: Record<string, string> = { "content-type": "application/json" };
    if (this.token) headers["authorization"] = `Bearer ${this.token}`;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
    try {
      const res = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: ctrl.signal,
      });
      const text = await res.text();
      const json = text ? JSON.parse(text) : {};
      if (!res.ok) {
        const detail = (json.detail ?? json.error ?? json) as RyaErrorBody;
        throw new RyaError(res.status, detail);
      }
      return json as T;
    } finally {
      clearTimeout(timer);
    }
  }

  /** Trigger a run by posting an event. */
  triggerEvent(payload: Record<string, unknown>, type = "message.received"): Promise<RunSummary> {
    return this.request<RunSummary>("POST", `/agents/_/events`, { type, payload });
  }

  /** Fire a (signature-verified) inbound webhook payload. */
  inbound(payload: Record<string, unknown>): Promise<RunSummary> {
    return this.request<RunSummary>("POST", `/inbound`, payload);
  }

  getRun(runId: string): Promise<Run> {
    return this.request<Run>("GET", `/runs/${runId}`);
  }

  getTrace(runId: string): Promise<{ runId: string; trace: TraceEvent[] }> {
    return this.request("GET", `/runs/${runId}/trace`);
  }

  listRuns(): Promise<{ runs: Run[] }> {
    return this.request("GET", `/agents/_/runs`);
  }

  listApprovals(status?: Approval["status"]): Promise<Approval[]> {
    const q = status ? `?status=${status}` : "";
    return this.request<{ approvals: Approval[] }>("GET", `/approvals${q}`).then((r) => r.approvals);
  }

  approve(approvalId: string): Promise<{ approvalId: string; runStatus: RunStatus }> {
    return this.request("POST", `/approvals/${approvalId}/approve`);
  }

  reject(approvalId: string): Promise<{ approvalId: string; runStatus: RunStatus }> {
    return this.request("POST", `/approvals/${approvalId}/reject`);
  }

  health(): Promise<{ ok: boolean; agent: string; store: string; multiTenant: boolean }> {
    return this.request("GET", `/healthz`);
  }

  // ---- streaming: trigger a run and consume it as Server-Sent Events -------

  /**
   * Trigger a run and stream it: yields `{event, data}` frames in arrival
   * order - `token` (LLM chunks), `trace` (journaled steps), `message`
   * (session replies), then ALWAYS a terminal `run` (or `error`) frame.
   */
  async *streamEvent(
    payload: Record<string, unknown>,
    type = "message.received"
  ): AsyncGenerator<StreamFrame> {
    const headers: Record<string, string> = {
      "content-type": "application/json",
      accept: "text/event-stream",
    };
    if (this.token) headers["authorization"] = `Bearer ${this.token}`;
    const res = await this.fetchImpl(`${this.baseUrl}/agents/_/events/stream`, {
      method: "POST",
      headers,
      body: JSON.stringify({ type, payload }),
    });
    if (!res.ok || !res.body) {
      const text = await res.text();
      const json = text ? JSON.parse(text) : {};
      throw new RyaError(res.status, (json.detail ?? json.error ?? json) as RyaErrorBody);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let event: string | null = null;
    let data: string[] = [];
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let nl: number;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl).replace(/\r$/, "");
        buffer = buffer.slice(nl + 1);
        if (line.startsWith(":")) continue; // keep-alive comment
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trim());
        else if (line === "" && event) {
          yield { event, data: data.length ? JSON.parse(data.join("\n")) : null };
          if (event === "run" || event === "error") return;
          event = null;
          data = [];
        }
      }
    }
  }

  // ---- durable chat turns --------------------------------------------------

  /** Start a durable chat turn. The turn survives a server crash (it's a leased,
   * reclaimable job) and its stream survives a dropped connection. */
  startTurn(
    payload: Record<string, unknown>,
    type = "message.received"
  ): Promise<{ turnId: string; status: string }> {
    return this.request("POST", `/agents/_/turns`, { type, payload });
  }

  /**
   * Stream a durable turn, resuming across dropped connections: yields frames
   * from `afterSeq`, and on a network drop it reconnects from the last seq it
   * saw (the durable buffer means nothing is lost). Ends on `run`/`error`.
   *
   * With `untilFinal: true`, a `run` frame with status `waiting_approval` is a
   * pause marker, not the end: the stream keeps tailing (reconnecting through
   * idle timeouts) until the approval resolves and the continuation's frames +
   * the real terminal frame (completed/failed/rejected) arrive.
   */
  async *streamTurn(
    turnId: string,
    afterSeq = -1,
    opts?: { maxReconnects?: number; untilFinal?: boolean }
  ): AsyncGenerator<StreamFrame> {
    let cursor = afterSeq;
    let reconnects = 0;
    const untilFinal = opts?.untilFinal ?? false;
    const maxReconnects = opts?.maxReconnects ?? (untilFinal ? Number.POSITIVE_INFINITY : 20);
    const isFinal = (event: string, data: any) =>
      event === "error" ||
      (event === "run" &&
        (!untilFinal || ["completed", "failed", "rejected"].includes(data?.status)));
    while (true) {
      const headers: Record<string, string> = { accept: "text/event-stream" };
      if (this.token) headers["authorization"] = `Bearer ${this.token}`;
      let res: Response;
      try {
        res = await this.fetchImpl(
          `${this.baseUrl}/agents/_/turns/${encodeURIComponent(turnId)}/stream?after=${cursor}`,
          { method: "GET", headers }
        );
      } catch {
        if (reconnects++ >= maxReconnects) return;
        await new Promise((r) => setTimeout(r, 500));
        continue;
      }
      if (!res.ok || !res.body) {
        const text = await res.text();
        throw new RyaError(res.status, JSON.parse(text || "{}") as RyaErrorBody);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let event: string | null = null;
      let id: number | null = null;
      let data: string[] = [];
      let terminated = false;
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let nl: number;
          while ((nl = buffer.indexOf("\n")) >= 0) {
            const line = buffer.slice(0, nl).replace(/\r$/, "");
            buffer = buffer.slice(nl + 1);
            if (line.startsWith(":")) continue; // keep-alive / idle comment
            if (line.startsWith("id:")) id = Number(line.slice(3).trim());
            else if (line.startsWith("event:")) event = line.slice(6).trim();
            else if (line.startsWith("data:")) data.push(line.slice(5).trim());
            else if (line === "" && event) {
              if (id !== null) cursor = id;
              const parsed = data.length ? JSON.parse(data.join("\n")) : null;
              yield { event, data: parsed };
              if (isFinal(event, parsed)) {
                terminated = true;
                break;
              }
              event = null;
              id = null;
              data = [];
            }
          }
          if (terminated) break;
        }
      } catch {
        // connection dropped mid-stream: reconnect from the last seq we saw
      }
      if (terminated) return;
      if (reconnects++ >= maxReconnects) return;
      await new Promise((r) => setTimeout(r, 300));
    }
  }

  // ---- queue: durable jobs executed by YOUR workers (any language) ---------

  /** Enqueue a durable job for an external worker. `jobId` is an idempotency key. */
  enqueueJob(type: string, payload: unknown, opts?: EnqueueJobOptions): Promise<QueueJob> {
    return this.request<{ job: QueueJob }>("POST", `/queue/jobs`, {
      type,
      payload,
      ...opts,
    }).then((r) => r.job);
  }

  /** Enqueue many jobs; dispatch preserves input order. Returns ids in order. */
  enqueueJobBatch(
    type: string,
    items: Array<{ payload: unknown } & EnqueueJobOptions>
  ): Promise<string[]> {
    return this.request<{ ids: string[] }>("POST", `/queue/jobs/batch`, { type, items }).then(
      (r) => r.ids
    );
  }

  getQueueJob(jobId: string): Promise<QueueJob | null> {
    return this.request<{ job: QueueJob }>("GET", `/queue/jobs/${encodeURIComponent(jobId)}`)
      .then((r) => r.job)
      .catch((e) => {
        if (e instanceof RyaError && e.httpStatus === 404) return null;
        throw e;
      });
  }

  /** Graceful cancel: pending cancels now; running workers see it on heartbeat. */
  cancelQueueJob(jobId: string, force = false): Promise<{ ok: boolean; found: boolean }> {
    return this.request("POST", `/queue/jobs/${encodeURIComponent(jobId)}/cancel`, { force });
  }

  queueStats(): Promise<{ counts: Record<string, number> }> {
    return this.request("GET", `/queue/stats`);
  }

  /** Worker-side: claim due jobs with a lease. */
  claimQueueJobs(opts: ClaimOptions): Promise<QueueJob[]> {
    return this.request<{ jobs: QueueJob[] }>("POST", `/queue/claim`, opts).then((r) => r.jobs);
  }

  /** Worker-side: extend the lease; the response carries `cancelRequested`. */
  heartbeatQueueJob(
    jobId: string,
    workerId: string,
    extendSeconds = 60
  ): Promise<{ ok: boolean; cancelRequested: boolean }> {
    return this.request("POST", `/queue/jobs/${encodeURIComponent(jobId)}/heartbeat`, {
      workerId,
      extendSeconds,
    });
  }

  completeQueueJob(jobId: string, workerId: string, output?: unknown): Promise<QueueJob> {
    return this.request<{ job: QueueJob }>(
      "POST",
      `/queue/jobs/${encodeURIComponent(jobId)}/complete`,
      { workerId, output: output ?? null }
    ).then((r) => r.job);
  }

  failQueueJob(jobId: string, workerId: string, error: string): Promise<QueueJob> {
    return this.request<{ job: QueueJob }>(
      "POST",
      `/queue/jobs/${encodeURIComponent(jobId)}/fail`,
      { workerId, error }
    ).then((r) => r.job);
  }
}

export interface StreamFrame {
  event: string; // token | trace | message | run | error
  data: any;
}

// ---- queue types -------------------------------------------------------------

export type QueueJobStatus = "pending" | "running" | "completed" | "failed" | "cancelled";

export interface QueueJob {
  id: string;
  type: string;
  payload: unknown;
  status: QueueJobStatus;
  attempts: number;
  maxAttempts: number;
  cancelRequested?: boolean;
  deadLetter?: boolean;
  output?: unknown;
  error?: string | null;
  lastError?: string | null;
  createdAt?: string;
  startedAt?: string | null;
  completedAt?: string | null;
  metadata?: Record<string, unknown>;
  tags?: string[];
}

export interface EnqueueJobOptions {
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
  waitSeconds?: number;
}

// ---- queue worker --------------------------------------------------------------

export type QueueJobHandler = (payload: unknown, signal: AbortSignal) => Promise<unknown>;

export interface QueueWorkerOptions {
  client: RyaClient;
  handlers: Record<string, QueueJobHandler>;
  workerId?: string;
  /** Max jobs executing concurrently in this process (default 5). */
  concurrency?: number;
  /** Lease length; heartbeats extend it by the same amount (default 60s). */
  leaseSeconds?: number;
  /** Server-side long-poll window per claim request (default 10s). */
  waitSeconds?: number;
  onError?: (error: unknown, job?: QueueJob) => void;
}

export interface QueueWorker {
  workerId: string;
  /** Resolves when stop() is called and in-flight jobs have been reported. */
  run(): Promise<void>;
  stop(): void;
  inFlight(): number;
}

/**
 * The execution loop for Rya's external-worker queue: claims due jobs,
 * heartbeats while they run (a cancelRequested or lost lease aborts the
 * handler's AbortSignal), and reports complete/fail. Rya owns retries,
 * backoff, and dead-lettering - a thrown handler error is reported once.
 */
export function createQueueWorker(options: QueueWorkerOptions): QueueWorker {
  const { client, handlers } = options;
  const workerId = options.workerId ?? `worker-${Math.random().toString(36).slice(2, 10)}`;
  const concurrency = Math.max(1, options.concurrency ?? 5);
  const leaseSeconds = Math.max(10, options.leaseSeconds ?? 60);
  const waitSeconds = options.waitSeconds ?? 10;
  const types = Object.keys(handlers);
  const onError = options.onError ?? (() => undefined);

  let running = false;
  const inFlight = new Set<Promise<void>>();

  async function execute(job: QueueJob): Promise<void> {
    const handler = handlers[job.type];
    if (!handler) {
      await client.failQueueJob(job.id, workerId, `No handler for job type '${job.type}'`);
      return;
    }
    const controller = new AbortController();
    const beat = setInterval(async () => {
      try {
        const hb = await client.heartbeatQueueJob(job.id, workerId, leaseSeconds);
        if (hb.cancelRequested) controller.abort("Cancelled");
      } catch (e) {
        if (e instanceof RyaError && e.httpStatus === 409) controller.abort("Lease lost");
        // other heartbeat errors are non-fatal; the lease covers us until the next beat
      }
    }, Math.max(1000, (leaseSeconds * 1000) / 3));
    try {
      const output = await handler(job.payload, controller.signal);
      await client.completeQueueJob(job.id, workerId, output ?? null);
    } catch (e) {
      onError(e, job);
      try {
        await client.failQueueJob(job.id, workerId, e instanceof Error ? e.message : String(e));
      } catch (reportErr) {
        onError(reportErr, job);
      }
    } finally {
      clearInterval(beat);
    }
  }

  return {
    workerId,
    stop: () => {
      running = false;
    },
    inFlight: () => inFlight.size,
    async run() {
      running = true;
      while (running) {
        const capacity = concurrency - inFlight.size;
        if (capacity <= 0) {
          await Promise.race(inFlight);
          continue;
        }
        let jobs: QueueJob[] = [];
        try {
          jobs = await client.claimQueueJobs({
            workerId,
            types,
            limit: capacity,
            leaseSeconds,
            waitSeconds,
          });
        } catch (e) {
          onError(e);
          await new Promise((r) => setTimeout(r, 5000));
          continue;
        }
        for (const job of jobs) {
          const p = execute(job).finally(() => inFlight.delete(p));
          inFlight.add(p);
        }
      }
      await Promise.allSettled(inFlight);
    },
  };
}
