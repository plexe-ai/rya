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
}
