/**
 * The transport: URL + auth headers + timeout + error-envelope mapping.
 *
 * Kept separate from `client.ts` because every method in the SDK funnels
 * through exactly two functions here, so "does this send the right header" and
 * "does a 409 become a typed error" are one place each, and testable by
 * swapping `fetch` alone.
 *
 * Uses the global `fetch` and web streams — both are built in from Node 18, so
 * the SDK needs no polyfill and no runtime dependency.
 */

import { RyaError } from "./errors.js";

export type QueryValue = string | number | boolean | undefined | null;

export interface TransportOptions {
  baseUrl: string;
  /**
   * Operator token (`RYA_TOKEN`), a workspace API key (`rya_sk_…`), or a user
   * JWT. Sent as `Authorization: Bearer <token>`; the API also accepts
   * `X-Rya-Token`, which {@link RyaClientOptions.tokenHeader} selects.
   */
  token?: string;
  tokenHeader?: "authorization" | "x-rya-token";
  /**
   * A verified user JWT sent as `X-Rya-User-Token`.
   *
   * Multi-tenant only, and orthogonal to `token`: the API key authenticates the
   * WORKSPACE, this authenticates the USER, and its presence turns on per-user
   * Postgres RLS for the request. Without it a request is workspace-scoped.
   */
  userToken?: string;
  /** Per-request timeout in ms; `0` disables. Default 30s. Never bounds a stream. */
  timeoutMs?: number;
  /** Extra headers on every request (tracing ids, a proxy's auth, …). */
  headers?: Record<string, string>;
  fetch?: typeof fetch;
}

export interface RequestOptions {
  query?: Record<string, QueryValue>;
  /** JSON-serialized into the body. Mutually exclusive with `rawBody`. */
  body?: unknown;
  /** Pre-encoded body (file uploads). Sets no content-type of its own. */
  rawBody?: BodyInit;
  headers?: Record<string, string>;
  /** Caller cancellation. Aborting rejects with the signal's reason, unwrapped. */
  signal?: AbortSignal;
  /** Overrides the transport default for this call. */
  timeoutMs?: number;
}

export function buildQuery(params: Record<string, QueryValue> | undefined): string {
  if (!params) return "";
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    usp.append(k, String(v));
  }
  const s = usp.toString();
  return s ? `?${s}` : "";
}

/**
 * Chain a caller's signal onto our own controller.
 *
 * Hand-rolled rather than `AbortSignal.any`, which only landed in Node 20.3 —
 * and the SDK's floor is Node 18.
 */
function link(controller: AbortController, signal: AbortSignal | undefined): () => void {
  if (!signal) return () => undefined;
  if (signal.aborted) {
    controller.abort(signal.reason);
    return () => undefined;
  }
  const onAbort = () => controller.abort(signal.reason);
  signal.addEventListener("abort", onAbort, { once: true });
  return () => signal.removeEventListener("abort", onAbort);
}

function safeJson(text: string): unknown {
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    // An HTML error page from a proxy, or FastAPI's bare `Internal Server Error`.
    return text;
  }
}

export class Transport {
  readonly baseUrl: string;
  private readonly token?: string;
  private readonly tokenHeader: "authorization" | "x-rya-token";
  private readonly userToken?: string;
  private readonly timeoutMs: number;
  private readonly extraHeaders: Record<string, string>;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: TransportOptions) {
    this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    this.token = opts.token;
    this.tokenHeader = opts.tokenHeader ?? "authorization";
    this.userToken = opts.userToken;
    this.timeoutMs = opts.timeoutMs ?? 30_000;
    this.extraHeaders = opts.headers ?? {};
    const f = opts.fetch ?? globalThis.fetch;
    if (!f) {
      throw new Error(
        "No global fetch. Use Node 18+, or pass { fetch } to the RyaClient constructor."
      );
    }
    // Bind so a bare `globalThis.fetch` reference does not lose its receiver.
    this.fetchImpl = opts.fetch ? f : f.bind(globalThis);
  }

  /** Auth + caller headers. Exposed so streaming builds the same set. */
  authHeaders(extra?: Record<string, string>): Record<string, string> {
    const headers: Record<string, string> = { ...this.extraHeaders };
    if (this.token) {
      headers[this.tokenHeader] =
        this.tokenHeader === "authorization" ? `Bearer ${this.token}` : this.token;
    }
    if (this.userToken) headers["x-rya-user-token"] = this.userToken;
    for (const [k, v] of Object.entries(extra ?? {})) headers[k.toLowerCase()] = v;
    return headers;
  }

  url(path: string, query?: Record<string, QueryValue>): string {
    return `${this.baseUrl}${path}${buildQuery(query)}`;
  }

  /**
   * Fetch with auth, a timeout and caller cancellation, mapping transport
   * failures onto `RyaError` so a caller never has to tell `TypeError:
   * fetch failed` apart from a 500.
   */
  async raw(method: string, path: string, opts: RequestOptions = {}): Promise<Response> {
    const headers = this.authHeaders(opts.headers);
    let body: BodyInit | undefined;
    if (opts.rawBody !== undefined) {
      body = opts.rawBody;
    } else if (opts.body !== undefined) {
      headers["content-type"] = headers["content-type"] ?? "application/json";
      body = JSON.stringify(opts.body);
    }

    const controller = new AbortController();
    const unlink = link(controller, opts.signal);
    const timeoutMs = opts.timeoutMs ?? this.timeoutMs;
    let timedOut = false;
    const timer =
      timeoutMs > 0
        ? setTimeout(() => {
            timedOut = true;
            controller.abort();
          }, timeoutMs)
        : undefined;

    try {
      return await this.fetchImpl(this.url(path, opts.query), {
        method,
        headers,
        body,
        signal: controller.signal,
      });
    } catch (err) {
      // Caller cancellation is not a Rya error — surface the abort untouched so
      // `signal.aborted` checks and `err.name === "AbortError"` still work.
      if (opts.signal?.aborted) throw err;
      if (timedOut) {
        throw new RyaError(0, {
          code: "E_TIMEOUT",
          message: `Request ${method} ${path} exceeded ${timeoutMs}ms.`,
          hint: "Raise timeoutMs, or use startTurn()/streamTurn() for long agent runs.",
        });
      }
      throw new RyaError(
        0,
        {
          code: "E_RUNTIME",
          message: `Could not reach ${this.baseUrl}: ${err instanceof Error ? err.message : String(err)}`,
          hint: "Check baseUrl and that `rya serve` is reachable from here.",
        },
        { codeFromServer: false, raw: err }
      );
    } finally {
      if (timer !== undefined) clearTimeout(timer);
      unlink();
    }
  }

  /** `raw` plus: non-2xx becomes a typed `RyaError`, 2xx is parsed as JSON. */
  async request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
    const res = await this.raw(method, path, opts);
    const text = await res.text();
    const payload = safeJson(text);
    if (!res.ok) throw RyaError.from(res.status, payload);
    return (payload ?? {}) as T;
  }

  /**
   * Open a streaming response.
   *
   * The timeout applies to establishing the response only, then it is dropped:
   * a durable turn legitimately streams for minutes, and a request timeout that
   * kept running would guillotine it mid-token. Cancellation stays available
   * through `opts.signal` for the whole body.
   */
  async stream(
    method: string,
    path: string,
    opts: RequestOptions = {}
  ): Promise<{ response: Response; abort: (reason?: unknown) => void }> {
    const headers = this.authHeaders({ accept: "text/event-stream", ...opts.headers });
    let body: BodyInit | undefined;
    if (opts.body !== undefined) {
      headers["content-type"] = headers["content-type"] ?? "application/json";
      body = JSON.stringify(opts.body);
    }

    const controller = new AbortController();
    const unlink = link(controller, opts.signal);
    const timeoutMs = opts.timeoutMs ?? this.timeoutMs;
    let timedOut = false;
    const timer =
      timeoutMs > 0
        ? setTimeout(() => {
            timedOut = true;
            controller.abort();
          }, timeoutMs)
        : undefined;

    let res: Response;
    try {
      res = await this.fetchImpl(this.url(path, opts.query), {
        method,
        headers,
        body,
        signal: controller.signal,
      });
    } catch (err) {
      unlink();
      if (opts.signal?.aborted) throw err;
      if (timedOut) {
        throw new RyaError(0, {
          code: "E_TIMEOUT",
          message: `Stream ${method} ${path} did not start within ${timeoutMs}ms.`,
        });
      }
      throw new RyaError(
        0,
        {
          code: "E_RUNTIME",
          message: `Could not reach ${this.baseUrl}: ${err instanceof Error ? err.message : String(err)}`,
        },
        { codeFromServer: false, raw: err }
      );
    } finally {
      if (timer !== undefined) clearTimeout(timer);
    }

    if (!res.ok) {
      unlink();
      const text = await res.text().catch(() => "");
      throw RyaError.from(res.status, safeJson(text));
    }
    if (!res.body) {
      unlink();
      throw new RyaError(res.status, {
        code: "E_RUNTIME",
        message: `${method} ${path} returned no readable body.`,
        hint: "A proxy may be buffering SSE; the API sets X-Accel-Buffering: no.",
      });
    }
    return {
      response: res,
      abort: (reason?: unknown) => {
        unlink();
        controller.abort(reason);
      },
    };
  }
}
