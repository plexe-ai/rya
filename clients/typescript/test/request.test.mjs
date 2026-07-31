// Request construction: URL, method, body, and above all the auth headers.
// If these are wrong every other test in the suite is testing a request the
// server would reject.

import assert from "node:assert/strict";
import test from "node:test";

import { RyaClient } from "../dist/index.js";
import { jsonResponse, recordingFetch } from "./helpers.mjs";

test("bearer token, JSON body and the default agent path", async () => {
  const fetchImpl = recordingFetch(jsonResponse(200, { runId: "run_1", status: "completed" }));
  const rya = new RyaClient({ baseUrl: "http://x.test/", token: "tok", fetch: fetchImpl });

  const res = await rya.triggerEvent({ email: "ada@example.com" });

  assert.equal(res.runId, "run_1");
  const [call] = fetchImpl.calls;
  assert.equal(call.method, "POST");
  assert.equal(call.url, "http://x.test/agents/_/events"); // trailing slash trimmed
  assert.equal(call.headers.authorization, "Bearer tok");
  assert.equal(call.headers["content-type"], "application/json");
  assert.deepEqual(JSON.parse(call.body), {
    type: "message.received",
    payload: { email: "ada@example.com" },
  });
});

test("no token means no authorization header (open local dev)", async () => {
  const fetchImpl = recordingFetch(jsonResponse(200, { ok: true }));
  await new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl }).health();
  assert.equal(fetchImpl.calls[0].headers.authorization, undefined);
  assert.equal(fetchImpl.calls[0].url, "http://x.test/healthz");
});

test("tokenHeader: x-rya-token sends the raw token, not a Bearer prefix", async () => {
  const fetchImpl = recordingFetch(jsonResponse(200, {}));
  const rya = new RyaClient({
    baseUrl: "http://x.test",
    token: "tok",
    tokenHeader: "x-rya-token",
    fetch: fetchImpl,
  });
  await rya.health();
  assert.equal(fetchImpl.calls[0].headers["x-rya-token"], "tok");
  assert.equal(fetchImpl.calls[0].headers.authorization, undefined);
});

test("userToken rides alongside the API key as X-Rya-User-Token", async () => {
  // Multi-tenant: the API key authenticates the WORKSPACE, the JWT the USER.
  const fetchImpl = recordingFetch(jsonResponse(200, { runs: [] }));
  const rya = new RyaClient({
    baseUrl: "http://x.test",
    token: "rya_sk_abc",
    userToken: "jwt.value",
    fetch: fetchImpl,
  });
  await rya.listRuns();
  assert.equal(fetchImpl.calls[0].headers.authorization, "Bearer rya_sk_abc");
  assert.equal(fetchImpl.calls[0].headers["x-rya-user-token"], "jwt.value");
});

test("query params: undefined is omitted, values are encoded", async () => {
  const fetchImpl = recordingFetch(jsonResponse(200, { approvals: [] }));
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });

  await rya.listApprovals();
  assert.equal(fetchImpl.calls[0].url, "http://x.test/approvals");

  await rya.listApprovals("pending");
  assert.equal(fetchImpl.calls[1].url, "http://x.test/approvals?status=pending");
});

test("usage groupBy maps to the server's snake_case group_by", async () => {
  const fetchImpl = recordingFetch(jsonResponse(200, { usage: {} }));
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });
  await rya.getUsageBy("model", { since: "2026-01-01" });
  assert.deepEqual(fetchImpl.calls[0].query, { since: "2026-01-01", group_by: "model" });
});

test("ids are URL-encoded into the path", async () => {
  const fetchImpl = recordingFetch(jsonResponse(200, { job: { id: "a/b" } }));
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });
  await rya.getQueueJob("a/b");
  assert.equal(fetchImpl.calls[0].path, "/queue/jobs/a%2Fb");
});

test("file upload sends raw bytes with tag.* query params", async () => {
  const fetchImpl = recordingFetch(jsonResponse(200, { ok: true, file: { id: "file_1" } }));
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });
  await rya.uploadFile("policy.pdf", "BYTES", {
    contentType: "application/pdf",
    tags: { kind: "policy" },
    event: false,
  });
  const [call] = fetchImpl.calls;
  assert.deepEqual(call.query, { name: "policy.pdf", "tag.kind": "policy", event: "false" });
  assert.equal(call.headers["content-type"], "application/pdf");
  assert.equal(call.body, "BYTES"); // not JSON-wrapped
});

test("inboundRaw sends the exact bytes it was given, plus the signature", async () => {
  // The server HMACs the RAW body, so re-serializing here would break the check.
  const fetchImpl = recordingFetch(jsonResponse(200, { runId: "run_2", status: "running" }));
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });
  const body = '{"a":1}';
  await rya.inboundRaw(body, { signature: "sha256=deadbeef", eventType: "invoice.paid" });
  const [call] = fetchImpl.calls;
  assert.equal(call.body, body);
  assert.equal(call.headers["x-rya-signature"], "sha256=deadbeef");
  assert.equal(call.headers["x-rya-event-type"], "invoice.paid");
});

test("agentId is configurable and encoded", async () => {
  const fetchImpl = recordingFetch(jsonResponse(200, { turnId: "t1", status: "pending" }));
  const rya = new RyaClient({ baseUrl: "http://x.test", agentId: "billing agent", fetch: fetchImpl });
  await rya.startTurn({ body: "hi" });
  assert.equal(fetchImpl.calls[0].path, "/agents/billing%20agent/turns");
});

test("a caller AbortSignal reaches fetch and its rejection is not rewrapped", async () => {
  const controller = new AbortController();
  const fetchImpl = recordingFetch(async (req) => {
    controller.abort();
    const err = new Error("aborted");
    err.name = "AbortError";
    assert.equal(req.signal.aborted, true, "the linked signal must abort with the caller's");
    throw err;
  });
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });
  await assert.rejects(rya.health({ signal: controller.signal }), (err) => err.name === "AbortError");
});

test("a timeout surfaces as E_TIMEOUT, not as a bare AbortError", async () => {
  const fetchImpl = recordingFetch(
    (req) =>
      new Promise((_resolve, reject) => {
        req.signal.addEventListener("abort", () => {
          const err = new Error("aborted");
          err.name = "AbortError";
          reject(err);
        });
      })
  );
  const rya = new RyaClient({ baseUrl: "http://x.test", timeoutMs: 10, fetch: fetchImpl });
  await assert.rejects(rya.health(), (err) => {
    assert.equal(err.code, "E_TIMEOUT");
    assert.equal(err.retryable, true);
    return true;
  });
});

test("an unreachable server becomes a typed E_RUNTIME, not TypeError", async () => {
  const fetchImpl = recordingFetch(async () => {
    throw new TypeError("fetch failed");
  });
  const rya = new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl });
  await assert.rejects(rya.health(), (err) => {
    assert.equal(err.name, "RyaError");
    assert.equal(err.code, "E_RUNTIME");
    assert.equal(err.httpStatus, 0);
    assert.equal(err.codeFromServer, false);
    return true;
  });
});
