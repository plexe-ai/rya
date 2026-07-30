// Resumable streaming — the client half of D6.
//
// The load-bearing claim is: a dropped socket must not lose a turn, and must not
// duplicate one either. So these assert on the wire, not on vibes — that the
// reconnect carries `Last-Event-ID` set to the last frame actually yielded, and
// that the frames the caller sees are exactly the buffer's, once each, in order.

import assert from "node:assert/strict";
import test from "node:test";

import { RyaClient, RyaStreamError, isFrame } from "../dist/index.js";
import { collect, frame, jsonResponse, recordingFetch, sseResponse } from "./helpers.mjs";

const RUN_DONE = { id: "run_1", status: "completed", traceLength: 4, tokens: 12 };

test("a clean stream yields every frame and stops at the terminal run", async () => {
  const fetchImpl = recordingFetch(
    sseResponse([
      frame(0, "trace", { kind: "run.started" }),
      frame(1, "token", { text: "he" }),
      frame(2, "token", { text: "llo" }),
      frame(3, "run", RUN_DONE),
    ])
  );
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });

  const frames = await collect(rya.streamTurn("qj_1"));

  assert.deepEqual(
    frames.map((f) => f.event),
    ["trace", "token", "token", "run"]
  );
  assert.deepEqual(
    frames.map((f) => f.seq),
    [0, 1, 2, 3]
  );
  assert.equal(fetchImpl.calls.length, 1);
  assert.equal(fetchImpl.calls[0].path, "/agents/_/turns/qj_1/stream");
  assert.equal(fetchImpl.calls[0].query.after, "-1"); // fresh tail
  assert.equal(fetchImpl.calls[0].headers["last-event-id"], undefined);
  assert.equal(fetchImpl.calls[0].headers.accept, "text/event-stream");
});

test("a mid-stream drop resumes with Last-Event-ID and neither replays nor drops", async () => {
  const fetchImpl = recordingFetch([
    // Socket dies after seq 1.
    sseResponse([frame(0, "trace", { kind: "run.started" }), frame(1, "token", { text: "he" })], "error"),
    sseResponse([frame(2, "token", { text: "llo" }), frame(3, "run", RUN_DONE)]),
  ]);
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });

  const frames = await collect(rya.streamTurn("qj_1", { reconnectDelayMs: 0 }));

  assert.deepEqual(frames.map((f) => f.seq), [0, 1, 2, 3], "every frame exactly once, in order");
  assert.equal(fetchImpl.calls.length, 2);

  const resume = fetchImpl.calls[1];
  assert.equal(resume.headers["last-event-id"], "1", "resumes from the last frame it yielded");
  assert.equal(resume.query.after, "1", "and mirrors it as ?after= for header-stripping proxies");
});

test("a server that replays the boundary frame does not double-deliver it", async () => {
  const fetchImpl = recordingFetch([
    sseResponse([frame(0, "token", { text: "a" }), frame(1, "token", { text: "b" })], "error"),
    // Inclusive resume: seq 1 comes back a second time.
    sseResponse([frame(1, "token", { text: "b" }), frame(2, "run", RUN_DONE)]),
  ]);
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });

  const frames = await collect(rya.streamTurn("qj_1", { reconnectDelayMs: 0 }));

  assert.deepEqual(frames.map((f) => f.seq), [0, 1, 2]);
  assert.deepEqual(
    frames.filter((f) => isFrame(f, "token")).map((f) => f.data.text),
    ["a", "b"]
  );
});

test("an idle-timeout close is protocol, not failure: reconnect from the same cursor", async () => {
  const fetchImpl = recordingFetch([
    sseResponse([frame(0, "token", { text: "a" })]),
    // The server's idle tail: a comment frame and a clean close, no data.
    sseResponse([": idle-timeout\n\n"]),
    sseResponse([frame(1, "run", RUN_DONE)]),
  ]);
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });

  const frames = await collect(rya.streamTurn("qj_1", { reconnectDelayMs: 0 }));

  assert.deepEqual(frames.map((f) => f.seq), [0, 1]);
  assert.equal(fetchImpl.calls[1].headers["last-event-id"], "0");
  assert.equal(fetchImpl.calls[2].headers["last-event-id"], "0", "an empty tail must not move the cursor");
});

test("exhausting the reconnect budget throws with the cursor to resume from", async () => {
  const fetchImpl = recordingFetch(async () => {
    throw new TypeError("fetch failed");
  });
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });

  await assert.rejects(
    collect(rya.streamTurn("qj_1", { lastEventId: 7, maxReconnects: 2, reconnectDelayMs: 0 })),
    (err) => {
      assert.ok(err instanceof RyaStreamError);
      assert.equal(err.turnId, "qj_1");
      assert.equal(err.lastEventId, "7", "the caller can pick up exactly here");
      assert.equal(err.code, "E_TIMEOUT");
      return true;
    }
  );
  assert.equal(fetchImpl.calls.length, 3); // initial + 2 retries
});

test("a 4xx is fatal — retrying a rejection just burns the budget", async () => {
  const fetchImpl = recordingFetch(jsonResponse(401, { detail: { code: "E_UNAUTHORIZED" } }));
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });

  await assert.rejects(collect(rya.streamTurn("qj_1", { reconnectDelayMs: 0 })), (err) => {
    assert.equal(err.code, "E_UNAUTHORIZED");
    return true;
  });
  assert.equal(fetchImpl.calls.length, 1);
});

test("lastEventId seeds the first request, so a process restart resumes cleanly", async () => {
  const fetchImpl = recordingFetch(sseResponse([frame(9, "run", RUN_DONE)]));
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });

  await collect(rya.streamTurn("qj_1", { lastEventId: 8 }));

  assert.equal(fetchImpl.calls[0].headers["last-event-id"], "8");
  assert.equal(fetchImpl.calls[0].query.after, "8");
});

test("the legacy (turnId, afterSeq) positional form still works", async () => {
  const fetchImpl = recordingFetch(sseResponse([frame(6, "run", RUN_DONE)]));
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });
  await collect(rya.streamTurn("qj_1", 5));
  assert.equal(fetchImpl.calls[0].query.after, "5");
});

test("untilFinal tails through an approval pause to the real terminal frame", async () => {
  const pause = { id: "run_1", status: "waiting_approval", pendingApproval: "apr_1", traceLength: 2, tokens: 4 };
  const fetchImpl = recordingFetch([
    sseResponse([frame(0, "trace", { kind: "tool.call" }), frame(1, "run", pause)]),
    sseResponse([
      frame(2, "trace", { kind: "approval.approved" }),
      frame(3, "run", { ...RUN_DONE, status: "completed" }),
    ]),
  ]);
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });

  const frames = await collect(rya.streamTurn("qj_1", { untilFinal: true, reconnectDelayMs: 0 }));

  assert.deepEqual(frames.map((f) => f.seq), [0, 1, 2, 3]);
  assert.equal(frames.at(-1).data.status, "completed");
});

test("without untilFinal, the pause frame ends the stream", async () => {
  const pause = { id: "run_1", status: "waiting_approval", pendingApproval: "apr_1", traceLength: 2, tokens: 4 };
  const fetchImpl = recordingFetch(sseResponse([frame(0, "run", pause), frame(1, "trace", {})]));
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });

  const frames = await collect(rya.streamTurn("qj_1"));

  assert.deepEqual(frames.map((f) => f.seq), [0]);
  assert.equal(fetchImpl.calls.length, 1, "and does not reconnect");
});

test("an error frame is terminal", async () => {
  const fetchImpl = recordingFetch(
    sseResponse([frame(0, "error", { code: "E_TOOL_PERMISSION_DENIED", message: "nope" })])
  );
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });

  const frames = await collect(rya.streamTurn("qj_1", { untilFinal: true }));

  assert.equal(frames.length, 1);
  assert.ok(isFrame(frames[0], "error"));
  assert.equal(frames[0].data.code, "E_TOOL_PERMISSION_DENIED");
});

test("AbortSignal ends the stream without a throw and without reconnecting", async () => {
  const controller = new AbortController();
  const fetchImpl = recordingFetch(
    sseResponse([frame(0, "token", { text: "a" }), frame(1, "token", { text: "b" })], "error")
  );
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });

  const seen = [];
  for await (const f of rya.streamTurn("qj_1", { signal: controller.signal, reconnectDelayMs: 0 })) {
    seen.push(f.seq);
    if (f.seq === 0) controller.abort();
  }

  assert.deepEqual(seen, [0]);
  assert.equal(fetchImpl.calls.length, 1);
});

test("breaking out of the loop closes the socket rather than leaking it", async () => {
  let cancelled = false;
  const body = new ReadableStream({
    start(c) {
      const enc = new TextEncoder();
      c.enqueue(enc.encode(frame(0, "token", { text: "a" })));
    },
    cancel() {
      cancelled = true;
    },
  });
  const fetchImpl = recordingFetch({ ok: true, status: 200, body, text: async () => "" });
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });

  for await (const f of rya.streamTurn("qj_1")) {
    assert.equal(f.seq, 0);
    break;
  }
  assert.equal(cancelled, true);
});

test("streamEvent yields the turn handle first and resumes on the durable tail", async () => {
  const fetchImpl = recordingFetch([
    // POST .../events/stream: the handle arrives in-band, then the socket dies.
    sseResponse(
      [
        `event: turn\ndata: {"turnId":"qj_9"}\n\n`,
        frame(0, "token", { text: "hi" }),
      ],
      "error"
    ),
    // GET .../turns/qj_9/stream: the durable tail picks it up.
    sseResponse([frame(1, "run", RUN_DONE)]),
  ]);
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });

  const frames = await collect(rya.streamEvent({ email: "ada@example.com" }, "message.received", {
    reconnectDelayMs: 0,
  }));

  assert.deepEqual(frames.map((f) => f.event), ["turn", "token", "run"]);
  assert.equal(fetchImpl.calls[0].method, "POST");
  assert.equal(fetchImpl.calls[0].path, "/agents/_/events/stream");
  assert.equal(fetchImpl.calls[1].method, "GET");
  assert.equal(fetchImpl.calls[1].path, "/agents/_/turns/qj_9/stream");
  assert.equal(fetchImpl.calls[1].headers["last-event-id"], "0");
});

test("streamEvent that loses the socket before the handle says so instead of hanging", async () => {
  const fetchImpl = recordingFetch(sseResponse([], "error"));
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });

  await assert.rejects(collect(rya.streamEvent({ email: "ada@example.com" })), (err) => {
    assert.equal(err.code, "E_RUNTIME");
    assert.match(err.message, /before the turn handle arrived/);
    return true;
  });
});
