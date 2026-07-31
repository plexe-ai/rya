/**
 * Compile-time checks on the PUBLIC surface. Never executed — `tsc -p
 * tsconfig.check.json` type-checks it against the emitted `.d.ts`, which is what
 * a consumer actually sees.
 *
 * The point is the assertions no unit test can make: that `isFrame` really
 * narrows, that nothing widened back to `any` on the way out, and that the
 * options bags accept what the docs claim.
 */

import {
  RyaClient,
  RyaError,
  RyaStreamError,
  createQueueWorker,
  isFrame,
  type QueueJob,
  type Run,
  type TurnFrame,
} from "../dist/index.js";

/** Fails to compile if `T` is `any` — the guard this whole file exists for. */
type NotAny<T> = 0 extends 1 & T ? never : T;
function assertNotAny<T>(_value: NotAny<T>): void {}

const rya = new RyaClient({
  baseUrl: "https://rya.example",
  token: "rya_sk_x",
  userToken: "jwt",
  tokenHeader: "authorization",
  timeoutMs: 15_000,
  agentId: "_",
  headers: { "x-trace-id": "abc" },
});

export async function platform(): Promise<void> {
  const summary = await rya.triggerEvent({ email: "ada@example.com" });
  assertNotAny<typeof summary.runId>(summary.runId);
  // @ts-expect-error — `runId` is on the event result; `id` is on the run record.
  void summary.id;

  const run: Run = await rya.getRun(summary.runId);
  assertNotAny<typeof run.trace>(run.trace);
  void run.trace[0]?.kind;

  if (run.status === "waiting_approval") {
    const [pending] = await rya.listApprovals("pending");
    if (pending) {
      const resolved = await rya.approve(pending.id);
      assertNotAny<typeof resolved.runStatus>(resolved.runStatus);
    }
  }

  // @ts-expect-error — approval status is a closed union.
  await rya.listApprovals("maybe");

  const totals = await rya.getUsage({ since: "2026-01-01" });
  const cost: number = totals.costUsd;
  void cost;

  const env = await rya.describeEnvironment("prod");
  const pinned: Record<string, number> = env.pinnedRuns;
  void pinned;
}

export async function streaming(controller: AbortController): Promise<void> {
  const { turnId } = await rya.startTurn({ body: "hi" });

  for await (const frame of rya.streamTurn(turnId, {
    untilFinal: true,
    lastEventId: 12,
    maxReconnects: 5,
    reconnectDelayMs: 100,
    signal: controller.signal,
  })) {
    const f: TurnFrame = frame;
    assertNotAny<typeof f.data>(f.data);

    if (isFrame(f, "token")) {
      const text: string = f.data.text; // narrowed by the guard
      void text;
    }
    if (isFrame(f, "run")) {
      const status: string = f.data.status;
      const tokens: number = f.data.tokens;
      void status;
      void tokens;
    }
    if (isFrame(f, "error")) {
      const code: string = f.data.code;
      void code;
    }
    // Unknown kinds still arrive; their payload must be narrowed, not trusted.
    // @ts-expect-error — `data` is `unknown` until narrowed.
    void f.data.whatever;
  }
}

export function errors(err: unknown): string | null {
  if (err instanceof RyaStreamError) return err.lastEventId; // resume cursor
  if (err instanceof RyaError) {
    if (err.code === "E_VERSION_IN_USE") return err.hint;
    const exit: number | null = err.exitCode;
    void exit;
    void err.retryable;
    // @ts-expect-error — the raw body is `unknown` on purpose.
    void err.body.detail;
    return err.hint;
  }
  return null;
}

export function worker(): void {
  const w = createQueueWorker({
    client: rya, // RyaClient must satisfy QueueWorkerClient structurally
    concurrency: 4,
    leaseSeconds: 90,
    handlers: {
      "render-pdf": async (payload: unknown, signal: AbortSignal, job: QueueJob) => {
        void signal.aborted;
        void job.attempts;
        // @ts-expect-error — a job payload is `unknown`; validate it yourself.
        void payload.docId;
        return { ok: true };
      },
    },
    onError: (error: unknown, job?: QueueJob) => {
      void error;
      void job?.id;
    },
  });
  void w.workerId;
  void w.inFlight();
}
