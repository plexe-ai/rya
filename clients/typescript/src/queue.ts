/**
 * The `/queue/*` worker loop — D14's second product surface.
 *
 * D14: "two named product surfaces: the agent platform and the durable job API.
 * `/queue/*` stays SDK-free for foreign code. **A queue job is not a governed
 * run**." A queue job gets leases, retries, backoff, dead-lettering and crash
 * reclaim. It does NOT get permission resolution, a guard verdict, a journal or
 * an approval gate — so nothing in this file pretends otherwise, and a handler
 * here is a plain async function, not an agent.
 *
 * The design cites TypeScript DAG workers as the motivating consumer, which is
 * exactly what this loop is for: Rya owns durability, your process owns
 * execution, and the boundary between them is four HTTP calls.
 */

import { RyaError } from "./errors.js";
import type { ClaimOptions, HeartbeatResult, QueueJob } from "./types.js";

/**
 * The subset of `RyaClient` a worker uses.
 *
 * Structural on purpose: a test double is a plain object, and this module never
 * has to import the client (which imports this one).
 */
export interface QueueWorkerClient {
  claimQueueJobs(opts: ClaimOptions, reqOpts?: { signal?: AbortSignal }): Promise<QueueJob[]>;
  heartbeatQueueJob(jobId: string, workerId: string, extendSeconds?: number): Promise<HeartbeatResult>;
  completeQueueJob(jobId: string, workerId: string, output?: unknown): Promise<QueueJob>;
  failQueueJob(jobId: string, workerId: string, error: string): Promise<QueueJob>;
}

/**
 * Executes one job.
 *
 * `signal` aborts when the job is cancelled or the lease is lost — stop work
 * promptly, because after a lost lease another worker already owns the job and
 * your result will be refused with `E_QUEUE_CONFLICT`.
 */
export type QueueJobHandler = (
  payload: unknown,
  signal: AbortSignal,
  job: QueueJob
) => Promise<unknown>;

export interface QueueWorkerOptions {
  client: QueueWorkerClient;
  /** Keyed by job `type`; the worker claims exactly these types. */
  handlers: Record<string, QueueJobHandler>;
  workerId?: string;
  /** Max jobs executing concurrently in this process (default 5). */
  concurrency?: number;
  /** Lease length; heartbeats extend it by the same amount (default 60s). */
  leaseSeconds?: number;
  /** Server-side long-poll window per claim, capped at 25s by the API (default 10s). */
  waitSeconds?: number;
  /**
   * Pause after a claim that returned nothing (default: 0 when `waitSeconds` is
   * set, else 250ms).
   *
   * Not cosmetic. The server's long poll is what paces this loop; if it answers
   * instantly — `waitSeconds: 0`, or a store that returns straight away — an
   * unpaused loop spins on microtasks, and a promise loop that never reaches a
   * macrotask starves every timer in the process, including its own heartbeats.
   */
  idleDelayMs?: number;
  /** Backoff after a failed claim call, in ms (default 5000). */
  claimErrorDelayMs?: number;
  /** Heartbeat period in ms. Defaults to a third of the lease. */
  heartbeatIntervalMs?: number;
  onError?: (error: unknown, job?: QueueJob) => void;
}

export interface QueueWorker {
  workerId: string;
  /** Resolves when `stop()` is called and in-flight jobs have been reported. */
  run(): Promise<void>;
  /** Stops claiming and cancels the pending long-poll; in-flight jobs finish. */
  stop(): void;
  inFlight(): number;
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
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

/**
 * Claim → heartbeat → execute → report.
 *
 * Rya owns retries, backoff and dead-lettering, so a thrown handler error is
 * reported ONCE and never retried locally: a client-side retry would consume
 * the attempt budget invisibly and defeat the server's backoff.
 */
export function createQueueWorker(options: QueueWorkerOptions): QueueWorker {
  const { client, handlers } = options;
  const workerId = options.workerId ?? `worker-${Math.random().toString(36).slice(2, 10)}`;
  const concurrency = Math.max(1, options.concurrency ?? 5);
  const leaseSeconds = Math.max(10, options.leaseSeconds ?? 60);
  const waitSeconds = options.waitSeconds ?? 10;
  const idleDelayMs = options.idleDelayMs ?? (waitSeconds > 0 ? 0 : 250);
  const claimErrorDelayMs = options.claimErrorDelayMs ?? 5_000;
  const types = Object.keys(handlers);
  const onError = options.onError ?? ((): void => undefined);

  let running = false;
  let stopper = new AbortController();
  const inFlight = new Set<Promise<void>>();

  /** Extend the lease on a schedule, and abort the handler when we lose it. */
  function startHeartbeat(job: QueueJob, controller: AbortController): () => void {
    // A third of the lease: two consecutive missed beats still leave the lease
    // alive, so a single blip does not hand the job to another worker.
    const every = options.heartbeatIntervalMs ?? Math.max(1_000, (leaseSeconds * 1000) / 3);
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const beat = async (): Promise<void> => {
      try {
        const hb = await client.heartbeatQueueJob(job.id, workerId, leaseSeconds);
        if (hb.cancelRequested) controller.abort(new Error(`Job ${job.id} cancelled`));
      } catch (err) {
        // 409 = E_QUEUE_CONFLICT: the lease expired and someone else holds the
        // job. Anything else is transient and the lease still covers us.
        if (err instanceof RyaError && err.httpStatus === 409) {
          controller.abort(new Error(`Job ${job.id} lease lost`));
          return;
        }
      }
      // Re-arm only after the call settles, so a slow API cannot pile up beats.
      if (!stopped) timer = setTimeout(() => void beat(), every);
    };
    timer = setTimeout(() => void beat(), every);
    return () => {
      stopped = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }

  async function execute(job: QueueJob): Promise<void> {
    const handler = handlers[job.type];
    if (!handler) {
      // Reported rather than swallowed: an unclaimable type must show up as a
      // failing job, not as a silently leased one that expires 60s later.
      await client.failQueueJob(job.id, workerId, `No handler for job type '${job.type}'`)
        .catch((err: unknown) => onError(err, job));
      return;
    }
    const controller = new AbortController();
    const stopBeat = startHeartbeat(job, controller);
    try {
      const output = await handler(job.payload, controller.signal, job);
      await client.completeQueueJob(job.id, workerId, output ?? null);
    } catch (err) {
      onError(err, job);
      try {
        await client.failQueueJob(job.id, workerId, err instanceof Error ? err.message : String(err));
      } catch (reportErr) {
        onError(reportErr, job);
      }
    } finally {
      stopBeat();
    }
  }

  return {
    workerId,
    stop(): void {
      running = false;
      stopper.abort(); // cut the in-flight long-poll so stop() is prompt
    },
    inFlight: () => inFlight.size,
    async run(): Promise<void> {
      running = true;
      stopper = new AbortController();
      while (running) {
        const capacity = concurrency - inFlight.size;
        if (capacity <= 0) {
          await Promise.race(inFlight);
          continue;
        }
        let jobs: QueueJob[] = [];
        try {
          jobs = await client.claimQueueJobs(
            { workerId, types, limit: capacity, leaseSeconds, waitSeconds },
            { signal: stopper.signal }
          );
        } catch (err) {
          if (!running) break;
          onError(err);
          await sleep(claimErrorDelayMs, stopper.signal);
          continue;
        }
        if (jobs.length === 0) {
          await sleep(idleDelayMs, stopper.signal);
          continue;
        }
        for (const job of jobs) {
          const p = execute(job).finally(() => inFlight.delete(p));
          inFlight.add(p);
        }
      }
      // Never abandon a leased job: report what is in flight before returning.
      await Promise.allSettled(inFlight);
    },
  };
}
