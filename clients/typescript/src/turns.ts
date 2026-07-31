/**
 * Resumable turn streaming — the client half of D6.
 *
 * D6: "all streaming goes through the durable turn buffer." The executor
 * APPENDS frames to a store-backed buffer and the endpoint TAILS it by sequence,
 * which means a dropped socket is not a lost turn — it is a cursor the client
 * still holds. This module is the part that has to believe that: on any
 * interruption it re-opens the tail with `Last-Event-ID` set to the last
 * sequence it actually yielded, and it refuses to emit a frame it has already
 * emitted.
 *
 * Two consequences worth stating, because both were wrong in the first client:
 *
 * - Give up LOUDLY. Returning quietly when reconnects run out makes "the turn
 *   finished" and "we stopped watching" the same event to a caller. It throws
 *   `RyaStreamError`, which carries the cursor to resume from.
 * - `: idle-timeout` is protocol, not failure. The server closes an idle tail on
 *   purpose (`RYA_TURN_STREAM_IDLE_SECONDS`, 60s) so proxies do not; a clean
 *   re-open costs nothing and must not spend the error budget.
 */

import { RyaError, RyaStreamError } from "./errors.js";
import type { Transport } from "./http.js";
import { parseData, readSse } from "./sse.js";
import { TERMINAL_RUN_STATUSES, type TurnFrame } from "./types.js";

export interface StreamOptions {
  /**
   * Resume cursor: the `id` of the last frame you processed. The stream
   * continues strictly AFTER it, so redelivery is impossible.
   */
  lastEventId?: string | number | null;
  /**
   * Tail through an approval pause to the real ending.
   *
   * A `run` frame with `waiting_approval` is a PAUSE marker: approving appends
   * the continuation's frames and a second, terminal `run` frame to the same
   * buffer. Default `false` stops at the pause (the run is yours to resolve);
   * `true` keeps tailing until `completed`/`failed`/`rejected`.
   */
  untilFinal?: boolean;
  /**
   * Budget for consecutive attempts that produce NO frames — drops, connect
   * failures, and idle timeouts on a turn that is not moving. Reset by progress.
   * Default 20, i.e. roughly 20 minutes of silence before giving up.
   */
  maxReconnects?: number;
  /** Base reconnect delay in ms; grows linearly to 5s. Default 300. */
  reconnectDelayMs?: number;
  /** Cancels the stream. The generator returns; nothing is thrown. */
  signal?: AbortSignal;
}

const MAX_BACKOFF_MS = 5_000;

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  if (ms <= 0) return Promise.resolve();
  return new Promise<void>((resolve) => {
    const timer = setTimeout(done, ms);
    function done(): void {
      clearTimeout(timer);
      signal?.removeEventListener("abort", done);
      resolve();
    }
    signal?.addEventListener("abort", done, { once: true });
  });
}

function toFrame(event: string, id: string | null, data: string): TurnFrame {
  const seq = id !== null && /^-?\d+$/.test(id) ? Number(id) : null;
  return { event, id, seq, data: parseData(data) };
}

function isTerminal(frame: TurnFrame, untilFinal: boolean): boolean {
  if (frame.event === "error") return true;
  if (frame.event !== "run") return false;
  if (!untilFinal) return true;
  const status = (frame.data as { status?: string } | null)?.status;
  return TERMINAL_RUN_STATUSES.includes(status as (typeof TERMINAL_RUN_STATUSES)[number]);
}

/** A 4xx is the server saying no; retrying it just burns the budget. */
function isFatal(err: unknown): boolean {
  return err instanceof RyaError && err.httpStatus >= 400 && err.httpStatus < 500;
}

interface Cursor {
  lastEventId: string | null;
  seq: number | null;
}

/**
 * Read one connection to exhaustion, updating `cursor` in place.
 *
 * Yields only frames strictly after the cursor. That check is what makes resume
 * safe against a server (or a proxy cache) that replays the boundary frame: the
 * SSE `id` is the buffer sequence, so "already seen" is decidable, not a guess.
 */
async function* readConnection(
  body: ReadableStream<Uint8Array>,
  cursor: Cursor,
  untilFinal: boolean,
  signal: AbortSignal | undefined,
  state: { progressed: boolean; terminal: boolean }
): AsyncGenerator<TurnFrame, void, void> {
  for await (const ev of readSse(body, { signal })) {
    const frame = toFrame(ev.event, ev.id, ev.data);
    if (frame.seq !== null && cursor.seq !== null && frame.seq <= cursor.seq) continue;
    if (frame.id !== null) cursor.lastEventId = frame.id;
    if (frame.seq !== null) cursor.seq = frame.seq;
    state.progressed = true;
    yield frame;
    if (isTerminal(frame, untilFinal)) {
      state.terminal = true;
      return;
    }
  }
}

/**
 * Tail a durable turn's frames, reconnecting across drops.
 *
 * @throws {RyaStreamError} when the reconnect budget is exhausted — the turn is
 * still running server-side; resume with the error's `lastEventId`.
 */
export async function* streamTurn(
  transport: Transport,
  agentId: string,
  turnId: string,
  opts: StreamOptions = {}
): AsyncGenerator<TurnFrame, void, void> {
  const untilFinal = opts.untilFinal ?? false;
  const maxReconnects = opts.maxReconnects ?? 20;
  const baseDelay = opts.reconnectDelayMs ?? 300;
  const path = `/agents/${encodeURIComponent(agentId)}/turns/${encodeURIComponent(turnId)}/stream`;

  const cursor: Cursor = {
    lastEventId: opts.lastEventId === undefined || opts.lastEventId === null
      ? null
      : String(opts.lastEventId),
    seq: null,
  };
  if (cursor.lastEventId !== null && /^-?\d+$/.test(cursor.lastEventId)) {
    cursor.seq = Number(cursor.lastEventId);
  }

  let failures = 0;
  let lastCause: unknown = null;

  for (;;) {
    if (opts.signal?.aborted) return;

    let handle: Awaited<ReturnType<Transport["stream"]>>;
    try {
      handle = await transport.stream("GET", path, {
        // Both cursors on purpose: `Last-Event-ID` is the SSE-standard resume
        // header the endpoint reads first, `?after=` is the same value as a
        // query param for proxies that strip unknown request headers.
        query: { after: cursor.seq ?? -1 },
        headers: cursor.lastEventId !== null ? { "last-event-id": cursor.lastEventId } : {},
        signal: opts.signal,
      });
    } catch (err) {
      if (opts.signal?.aborted) return;
      if (isFatal(err)) throw err;
      lastCause = err;
      if (++failures > maxReconnects) {
        throw new RyaStreamError(turnId, cursor.lastEventId, failures, err);
      }
      await sleep(Math.min(baseDelay * failures, MAX_BACKOFF_MS), opts.signal);
      continue;
    }

    const state = { progressed: false, terminal: false };
    try {
      yield* readConnection(handle.response.body!, cursor, untilFinal, opts.signal, state);
    } catch (err) {
      if (opts.signal?.aborted) return;
      lastCause = err; // a mid-body drop; the buffer still holds everything
    } finally {
      handle.abort();
    }

    if (state.terminal) return;
    if (opts.signal?.aborted) return;
    // Progress resets the budget: a turn streaming for an hour across a dozen
    // idle timeouts is healthy, not failing.
    failures = state.progressed ? 0 : failures + 1;
    if (failures > maxReconnects) {
      throw new RyaStreamError(turnId, cursor.lastEventId, failures, lastCause);
    }
    await sleep(state.progressed ? 0 : Math.min(baseDelay * failures, MAX_BACKOFF_MS), opts.signal);
  }
}

/**
 * Trigger a run and stream it, then keep streaming it durably.
 *
 * `POST /agents/{id}/events/stream` sends `event: turn {turnId}` first and stops
 * at an approval pause. So this reads that connection, remembers the handle, and
 * on ANY interruption — drop, idle timeout, or the deliberate stop-at-pause —
 * continues on the durable tail from the same cursor. The caller sees one
 * uninterrupted sequence of frames regardless of how many sockets it took.
 */
export async function* streamEvent(
  transport: Transport,
  agentId: string,
  body: { type: string; payload: Record<string, unknown> },
  opts: StreamOptions = {}
): AsyncGenerator<TurnFrame, void, void> {
  const untilFinal = opts.untilFinal ?? false;
  const cursor: Cursor = { lastEventId: null, seq: null };
  let turnId: string | null = null;
  let terminal = false;
  let dropCause: unknown = null;

  const handle = await transport.stream(
    "POST",
    `/agents/${encodeURIComponent(agentId)}/events/stream`,
    { body, signal: opts.signal }
  );
  try {
    for await (const ev of readSse(handle.response.body!, { signal: opts.signal })) {
      const frame = toFrame(ev.event, ev.id, ev.data);
      if (frame.event === "turn") {
        turnId = (frame.data as { turnId?: string } | null)?.turnId ?? null;
      }
      if (frame.seq !== null && cursor.seq !== null && frame.seq <= cursor.seq) continue;
      if (frame.id !== null) cursor.lastEventId = frame.id;
      if (frame.seq !== null) cursor.seq = frame.seq;
      yield frame;
      if (isTerminal(frame, untilFinal)) {
        terminal = true;
        break;
      }
    }
  } catch (err) {
    // A drop here is not fatal — the turn is durable and we hold its cursor, so
    // fall through to the tail below rather than surfacing a transport error.
    if (opts.signal?.aborted) return;
    dropCause = err;
  } finally {
    handle.abort();
  }

  if (terminal || opts.signal?.aborted) return;
  if (turnId === null) {
    // The handle arrives in-band as the first frame, so losing the connection
    // before it means we cannot name the turn we just started. It is still
    // running and still durable — it is only unreachable from here.
    throw new RyaError(
      0,
      {
        code: "E_RUNTIME",
        message: "Event stream ended before the turn handle arrived; cannot resume.",
        hint: "Use startTurn() + streamTurn() when you need the handle before any frames.",
      },
      { codeFromServer: false, raw: dropCause }
    );
  }
  yield* streamTurn(transport, agentId, turnId, { ...opts, lastEventId: cursor.lastEventId });
}
