/**
 * The typed error surface.
 *
 * `errors.py` states the contract this file mirrors: "every failure surfaced to
 * a coding agent should carry a *stable* string code (`E_*`) plus a process exit
 * code, so [a client] can branch on the failure deterministically instead of
 * scraping human prose." A TS client that threw bare `Error("HTTP 409")` would
 * throw that guarantee away at the boundary, so `RyaError` carries `code`,
 * `hint` and `exitCode` through unchanged.
 */

/**
 * The codes `src/rya/errors.py` declares, kept as a union for autocomplete.
 *
 * `(string & {})` widens it to any string on purpose: the server emits codes
 * that are not in that table (`E_NOT_FOUND` from `GET /files/{id}`,
 * `E_SESSION_NOT_FOUND` from the session routes) and will emit more over time.
 * Pinning this to a closed union would turn a server-side addition into a
 * compile error in every client — the opposite of a stable contract.
 */
export type RyaErrorCode =
  | "E_MANIFEST_NOT_FOUND"
  | "E_MANIFEST_INVALID"
  | "E_ENTRYPOINT_NOT_FOUND"
  | "E_AGENT_NOT_DEFINED"
  | "E_HANDLER_NOT_FOUND"
  | "E_TOOL_NOT_FOUND"
  | "E_TOOL_PERMISSION_DENIED"
  | "E_MODEL_NOT_FOUND"
  | "E_RUN_NOT_FOUND"
  | "E_APPROVAL_NOT_FOUND"
  | "E_APPROVAL_NOT_PENDING"
  | "E_RUN_NOT_PAUSED"
  | "E_JOB_NOT_FOUND"
  | "E_QUEUE_CONFLICT"
  | "E_MODEL_ROUTE_NOT_FOUND"
  | "E_GROUNDING_BLOCKED"
  | "E_VALIDATION"
  | "E_NOT_PRODUCTION_READY"
  | "E_UNAUTHORIZED"
  | "E_BAD_SIGNATURE"
  | "E_NO_CONNECTION"
  | "E_SCOPE_DENIED"
  | "E_NO_IDENTITY"
  | "E_CONNECTION_EXPIRED"
  | "E_TIMEOUT"
  | "E_TOOL_UPSTREAM"
  | "E_TOOL_RECOVERABLE"
  | "E_RUNTIME"
  | "E_JOURNAL_DRIFT"
  | "E_VERSION_NOT_FOUND"
  | "E_VERSION_RETIRED"
  | "E_VERSION_IN_USE"
  | "E_BUNDLE_MISMATCH"
  | "E_BUNDLE_NOT_FOUND"
  | "E_HANDLER_SET_INCOMPLETE"
  | "E_ENVIRONMENT_NOT_FOUND"
  | "E_POLICY_READONLY"
  | "E_PROMOTION_BLOCKED"
  | "E_QUOTA_EXCEEDED"
  | "E_BUNDLE_STORE"
  | (string & {});

/** The `error` object inside Rya's `{ok: false, error: {...}}` envelope. */
export interface RyaErrorBody {
  code: RyaErrorCode;
  message: string;
  hint?: string | null;
  exit_code?: number | null;
}

/** Semantic exit-code buckets, mirroring `errors.py`. */
export const EXIT = {
  OK: 0,
  GENERIC: 1,
  USAGE: 2,
  MANIFEST: 3,
  NOT_FOUND: 4,
  PERMISSION: 5,
  STATE: 6,
  VALIDATION: 7,
} as const;

/**
 * Status → code, used ONLY when the server gave us no envelope at all.
 *
 * That happens for real: `POST /agents/{id}/events` does not wrap
 * `engine.run_event`, so a `RyaError` raised inside a handler escapes FastAPI as
 * a bare 500 `Internal Server Error` with no code. A proxy 502 has no code
 * either. Inferring one keeps `err.code` always present; `codeFromServer` says
 * whether to trust it.
 */
function inferCode(status: number): RyaErrorCode {
  if (status === 401 || status === 403) return "E_UNAUTHORIZED";
  // Deliberately the generic noun: a bare 404 could be a run, job, version or
  // session, and guessing `E_RUN_NOT_FOUND` would send a caller hunting in the
  // wrong place. `E_NOT_FOUND` is what `GET /files/{id}` itself returns.
  if (status === 404) return "E_NOT_FOUND";
  if (status === 400 || status === 413 || status === 422) return "E_VALIDATION";
  if (status === 408 || status === 504) return "E_TIMEOUT";
  return "E_RUNTIME";
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Pull the error object out of whichever envelope this route happens to use. */
function unwrap(payload: unknown): Record<string, unknown> | null {
  if (!isRecord(payload)) return null;
  // FastAPI HTTPException(detail={...}) — the common case on every route.
  if (isRecord(payload["detail"])) return payload["detail"];
  // RyaError.to_dict() and the /mcp middleware: {"ok": false, "error": {...}}.
  if (isRecord(payload["error"])) return payload["error"];
  // Some routes raise with the bare error dict as the body.
  if (typeof payload["code"] === "string") return payload;
  // FastAPI request-validation failures: {"detail": [{loc, msg, type}, ...]}.
  if (Array.isArray(payload["detail"])) {
    const parts = payload["detail"]
      .map((d) => (isRecord(d) && typeof d["msg"] === "string" ? d["msg"] : JSON.stringify(d)))
      .join("; ");
    return { code: "E_VALIDATION", message: parts || "Request validation failed." };
  }
  if (typeof payload["detail"] === "string") {
    return { message: payload["detail"] };
  }
  return null;
}

/**
 * A Rya API failure.
 *
 * Branch on {@link code}, show {@link hint} — that pair is the whole reason the
 * envelope exists. {@link body} keeps the raw payload for anything a future
 * server adds that this class does not model yet.
 */
export class RyaError extends Error {
  readonly name = "RyaError";
  readonly code: RyaErrorCode;
  /** The suggested next action. `errors.py` makes this first-class; so do we. */
  readonly hint: string | null;
  /** The semantic exit code, when the server sent one. See {@link EXIT}. */
  readonly exitCode: number | null;
  readonly httpStatus: number;
  /** False when {@link code} was inferred from the HTTP status, not returned. */
  readonly codeFromServer: boolean;
  /** The raw response payload, parsed if it was JSON, else the response text. */
  readonly body: unknown;

  constructor(
    status: number,
    body: RyaErrorBody | Partial<RyaErrorBody>,
    opts?: { codeFromServer?: boolean; raw?: unknown }
  ) {
    const code = (body.code ?? inferCode(status)) as RyaErrorCode;
    const message = body.message ?? `Rya request failed with HTTP ${status}.`;
    super(`[${code}] ${message}${body.hint ? ` | next: ${body.hint}` : ""}`);
    this.code = code;
    this.hint = body.hint ?? null;
    this.exitCode = body.exit_code ?? null;
    this.httpStatus = status;
    this.codeFromServer = opts?.codeFromServer ?? body.code !== undefined;
    this.body = opts?.raw ?? body;
  }

  /** Build from an HTTP status plus a body of unknown shape. */
  static from(status: number, payload: unknown): RyaError {
    const found = unwrap(payload);
    if (found) {
      return new RyaError(
        status,
        {
          code: typeof found["code"] === "string" ? found["code"] : undefined,
          message: typeof found["message"] === "string" ? found["message"] : undefined,
          hint: typeof found["hint"] === "string" ? found["hint"] : null,
          exit_code: typeof found["exit_code"] === "number" ? found["exit_code"] : null,
        },
        { codeFromServer: typeof found["code"] === "string", raw: payload }
      );
    }
    const text = typeof payload === "string" && payload.trim() ? payload.trim().slice(0, 500) : "";
    return new RyaError(
      status,
      { message: text || `Rya request failed with HTTP ${status}.` },
      { codeFromServer: false, raw: payload }
    );
  }

  /** True for failures worth retrying: transport, timeout, or 5xx. */
  get retryable(): boolean {
    return this.httpStatus >= 500 || this.httpStatus === 0 || this.code === "E_TIMEOUT";
  }
}

/**
 * A resumable stream that gave up.
 *
 * Distinct from `RyaError` because it carries {@link lastEventId}: the turn is
 * durable (D6), so an exhausted reconnect budget is not a lost turn — the caller
 * can resume from that cursor later. Returning quietly instead, as if the stream
 * had ended, would make "the turn finished" and "we stopped watching"
 * indistinguishable.
 */
export class RyaStreamError extends Error {
  readonly name = "RyaStreamError";
  readonly code: RyaErrorCode = "E_TIMEOUT";
  readonly turnId: string;
  /** Pass back as `lastEventId` to `streamTurn` to pick up exactly here. */
  readonly lastEventId: string | null;
  readonly cause: unknown;

  constructor(turnId: string, lastEventId: string | null, attempts: number, cause?: unknown) {
    super(
      `[E_TIMEOUT] Stream for turn '${turnId}' dropped and did not recover after ` +
        `${attempts} reconnect(s) | next: the turn is durable — resume with ` +
        `streamTurn(turnId, { lastEventId: ${JSON.stringify(lastEventId)} })`
    );
    this.turnId = turnId;
    this.lastEventId = lastEventId;
    this.cause = cause;
  }
}
