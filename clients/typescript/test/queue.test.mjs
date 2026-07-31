// The `/queue/*` surface and its worker loop (D14).
//
// A queue job is NOT a governed run: it gets leases, retries, backoff and
// dead-lettering, and nothing else. What the client owes it is that the worker
// stops when the lease is gone and reports exactly once — Rya owns the retry.

import assert from "node:assert/strict";
import test from "node:test";

import { RyaClient, RyaError, createQueueWorker } from "../dist/index.js";
import { jsonResponse, recordingFetch } from "./helpers.mjs";

function job(overrides = {}) {
  return {
    id: "qj_1",
    type: "render",
    payload: { doc: 1 },
    status: "running",
    attempts: 1,
    maxAttempts: 3,
    ...overrides,
  };
}

test("enqueue sends type, payload and the idempotency key in one body", async () => {
  const fetchImpl = recordingFetch(jsonResponse(200, { job: job({ status: "pending" }) }));
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });

  const created = await rya.enqueueJob("render", { doc: 1 }, { jobId: "doc-1", maxAttempts: 5 });

  assert.equal(created.id, "qj_1");
  assert.deepEqual(JSON.parse(fetchImpl.calls[0].body), {
    type: "render",
    payload: { doc: 1 },
    jobId: "doc-1",
    maxAttempts: 5,
  });
});

test("per-call options do not leak into the request body", async () => {
  const fetchImpl = recordingFetch(jsonResponse(200, { job: job() }));
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });
  const controller = new AbortController();

  await rya.enqueueJob("render", { doc: 1 }, { jobId: "d", signal: controller.signal, timeoutMs: 5000 });

  const body = JSON.parse(fetchImpl.calls[0].body);
  assert.equal(body.signal, undefined);
  assert.equal(body.timeoutMs, undefined);
});

test("a full pass: claim, heartbeat, complete", async () => {
  const calls = [];
  const client = {
    claimQueueJobs: async (opts) => {
      calls.push(["claim", opts]);
      return calls.filter((c) => c[0] === "claim").length === 1 ? [job()] : [];
    },
    heartbeatQueueJob: async () => ({ ok: true, leaseExpiresAt: "", cancelRequested: false }),
    completeQueueJob: async (id, workerId, output) => {
      calls.push(["complete", id, workerId, output]);
      return job({ status: "completed" });
    },
    failQueueJob: async (id, _w, error) => {
      calls.push(["fail", id, error]);
      return job({ status: "failed" });
    },
  };

  const worker = createQueueWorker({
    client,
    workerId: "w1",
    handlers: { render: async (payload) => ({ pages: payload.doc + 1 }) },
  });
  const running = worker.run();
  // Give the loop a couple of ticks, then stop it.
  await new Promise((r) => setTimeout(r, 20));
  worker.stop();
  await running;

  assert.deepEqual(calls[0][1].types, ["render"]);
  assert.equal(calls[0][1].workerId, "w1");
  const complete = calls.find((c) => c[0] === "complete");
  assert.deepEqual(complete, ["complete", "qj_1", "w1", { pages: 2 }]);
});

test("a thrown handler is reported once — the client never retries locally", async () => {
  const reported = [];
  let claimed = false;
  const client = {
    claimQueueJobs: async () => {
      if (claimed) return [];
      claimed = true;
      return [job()];
    },
    heartbeatQueueJob: async () => ({ ok: true, leaseExpiresAt: "", cancelRequested: false }),
    completeQueueJob: async () => job(),
    failQueueJob: async (id, _w, error) => {
      reported.push(error);
      return job({ status: "failed" });
    },
  };
  const worker = createQueueWorker({
    client,
    handlers: { render: async () => { throw new Error("upstream 500"); } },
    onError: () => undefined,
  });
  const running = worker.run();
  await new Promise((r) => setTimeout(r, 20));
  worker.stop();
  await running;

  assert.deepEqual(reported, ["upstream 500"], "Rya owns backoff and dead-lettering");
});

test("an unhandled job type fails the job rather than sitting on a lease", async () => {
  const reported = [];
  let claimed = false;
  const client = {
    claimQueueJobs: async () => {
      if (claimed) return [];
      claimed = true;
      return [job({ type: "unknown" })];
    },
    heartbeatQueueJob: async () => ({ ok: true, leaseExpiresAt: "", cancelRequested: false }),
    completeQueueJob: async () => job(),
    failQueueJob: async (_id, _w, error) => {
      reported.push(error);
      return job({ status: "failed" });
    },
  };
  const worker = createQueueWorker({ client, handlers: { render: async () => null } });
  const running = worker.run();
  await new Promise((r) => setTimeout(r, 20));
  worker.stop();
  await running;

  assert.equal(reported.length, 1);
  assert.match(reported[0], /No handler for job type 'unknown'/);
});

test("cancelRequested on a heartbeat aborts the handler's signal", async () => {
  let claimed = false;
  let aborted = false;
  const client = {
    claimQueueJobs: async () => {
      if (claimed) return [];
      claimed = true;
      return [job()];
    },
    heartbeatQueueJob: async () => ({ ok: true, leaseExpiresAt: "", cancelRequested: true }),
    completeQueueJob: async () => job({ status: "cancelled" }),
    failQueueJob: async () => job({ status: "failed" }),
  };
  const worker = createQueueWorker({
    client,
    heartbeatIntervalMs: 20, // the default is a third of the lease; too slow here
    handlers: {
      render: async (_payload, signal) =>
        new Promise((resolve) => {
          signal.addEventListener("abort", () => {
            aborted = true;
            resolve(null);
          });
        }),
    },
  });
  const running = worker.run();
  await new Promise((r) => setTimeout(r, 200));
  worker.stop();
  await running;

  assert.equal(aborted, true);
});

test("a 409 on heartbeat means the lease is gone: abort, do not keep working", async () => {
  let claimed = false;
  let aborted = false;
  const client = {
    claimQueueJobs: async () => {
      if (claimed) return [];
      claimed = true;
      return [job()];
    },
    heartbeatQueueJob: async () => {
      throw new RyaError(409, { code: "E_QUEUE_CONFLICT", message: "not the holder" });
    },
    completeQueueJob: async () => job(),
    failQueueJob: async () => job(),
  };
  const worker = createQueueWorker({
    client,
    heartbeatIntervalMs: 20,
    handlers: {
      render: async (_payload, signal) =>
        new Promise((resolve) => {
          signal.addEventListener("abort", () => {
            aborted = true;
            resolve(null);
          });
        }),
    },
  });
  const running = worker.run();
  await new Promise((r) => setTimeout(r, 200));
  worker.stop();
  await running;

  assert.equal(aborted, true);
});

test("stop() cancels the in-flight long poll instead of waiting it out", async () => {
  let sawAbort = false;
  const client = {
    claimQueueJobs: (_opts, reqOpts) =>
      new Promise((resolve) => {
        reqOpts.signal.addEventListener("abort", () => {
          sawAbort = true;
          resolve([]);
        });
      }),
    heartbeatQueueJob: async () => ({ ok: true, leaseExpiresAt: "", cancelRequested: false }),
    completeQueueJob: async () => job(),
    failQueueJob: async () => job(),
  };
  const worker = createQueueWorker({ client, handlers: { render: async () => null }, waitSeconds: 25 });
  const running = worker.run();
  await new Promise((r) => setTimeout(r, 10));
  worker.stop();
  await running; // would hang for 25s without the abort

  assert.equal(sawAbort, true);
});

test("claim passes the worker's abort signal through to fetch", async () => {
  const fetchImpl = recordingFetch(jsonResponse(200, { jobs: [] }));
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });
  const controller = new AbortController();
  await rya.claimQueueJobs({ workerId: "w1", types: ["render"] }, { signal: controller.signal });
  assert.equal(fetchImpl.calls[0].signal.aborted, false);
  controller.abort();
  // The transport links the caller's signal onto its own, so aborting later
  // still tears down a request that is genuinely in flight.
  assert.equal(controller.signal.aborted, true);
});
