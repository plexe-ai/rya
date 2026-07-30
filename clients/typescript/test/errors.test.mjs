// The error envelope → typed error mapping.
//
// `errors.py`: a failure must carry a *stable* code plus a next action "so a
// coding agent can branch on the failure deterministically instead of scraping
// human prose". These tests pin that the code and hint survive the HTTP hop
// through every envelope shape the API actually produces.

import assert from "node:assert/strict";
import test from "node:test";

import { RyaClient, RyaError } from "../dist/index.js";
import { jsonResponse, recordingFetch, textResponse } from "./helpers.mjs";

function client(response) {
  const fetchImpl = recordingFetch(response);
  return { rya: new RyaClient({ baseUrl: "http://x.test", fetch: fetchImpl }), fetchImpl };
}

test("FastAPI HTTPException(detail={...}) keeps code, message, hint and exit code", async () => {
  const { rya } = client(
    jsonResponse(401, {
      detail: {
        code: "E_UNAUTHORIZED",
        message: "Missing or invalid operator token.",
        hint: "Send 'Authorization: Bearer $RYA_TOKEN' (or X-Rya-Token).",
        exit_code: 5,
      },
    })
  );
  await assert.rejects(rya.health(), (err) => {
    assert.ok(err instanceof RyaError);
    assert.equal(err.code, "E_UNAUTHORIZED");
    assert.equal(err.hint, "Send 'Authorization: Bearer $RYA_TOKEN' (or X-Rya-Token).");
    assert.equal(err.exitCode, 5);
    assert.equal(err.httpStatus, 401);
    assert.equal(err.codeFromServer, true);
    assert.match(err.message, /\[E_UNAUTHORIZED]/);
    assert.match(err.message, /next: Send 'Authorization/);
    return true;
  });
});

test("RyaError.to_dict()'s {ok:false, error:{...}} envelope maps identically", async () => {
  const { rya } = client(
    jsonResponse(409, {
      ok: false,
      error: {
        code: "E_VERSION_IN_USE",
        message: "3 runs are still pinned to ver_a.",
        hint: "Drain them, or retire with force.",
        exit_code: 6,
      },
    })
  );
  await assert.rejects(rya.retireVersion("ver_a"), (err) => {
    assert.equal(err.code, "E_VERSION_IN_USE");
    assert.equal(err.exitCode, 6);
    return true;
  });
});

test("a 404 with only a code and no message still yields that code", async () => {
  // Real shape: `GET /queue/jobs/{id}` raises detail={"code": "E_JOB_NOT_FOUND"}.
  const err = RyaError.from(404, { detail: { code: "E_JOB_NOT_FOUND" } });
  assert.equal(err.code, "E_JOB_NOT_FOUND");
  assert.equal(err.hint, null);
  assert.match(err.message, /HTTP 404/);
});

test("FastAPI's validation array collapses to E_VALIDATION with the field messages", () => {
  const err = RyaError.from(422, {
    detail: [
      { loc: ["query", "channel"], msg: "field required", type: "value_error.missing" },
      { loc: ["query", "externalId"], msg: "field required", type: "value_error.missing" },
    ],
  });
  assert.equal(err.code, "E_VALIDATION");
  assert.equal(err.message, "[E_VALIDATION] field required; field required");
});

test("a bare 500 with no envelope infers E_RUNTIME and admits it inferred", async () => {
  // This happens for real: `POST /agents/{id}/events` does not catch RyaError,
  // so a handler failure escapes as FastAPI's plain-text Internal Server Error.
  const { rya } = client(textResponse(500, "Internal Server Error"));
  await assert.rejects(rya.triggerEvent({ email: "a@b.c" }), (err) => {
    assert.equal(err.code, "E_RUNTIME");
    assert.equal(err.codeFromServer, false);
    assert.equal(err.retryable, true);
    assert.equal(err.body, "Internal Server Error");
    return true;
  });
});

test("an HTML proxy error page does not blow up the JSON parse", async () => {
  const { rya } = client(textResponse(502, "<html><body>502 Bad Gateway</body></html>"));
  await assert.rejects(rya.health(), (err) => {
    assert.equal(err.httpStatus, 502);
    assert.equal(err.code, "E_RUNTIME");
    assert.match(err.message, /502 Bad Gateway/);
    return true;
  });
});

test("inferred codes never guess a specific noun for a bare 404", () => {
  const err = RyaError.from(404, "");
  assert.equal(err.code, "E_NOT_FOUND");
  assert.equal(err.codeFromServer, false);
});

test("401 and 403 infer E_UNAUTHORIZED; 400 infers E_VALIDATION", () => {
  assert.equal(RyaError.from(401, "").code, "E_UNAUTHORIZED");
  assert.equal(RyaError.from(403, "").code, "E_UNAUTHORIZED");
  assert.equal(RyaError.from(400, "").code, "E_VALIDATION");
});

test("retryable is true for 5xx and transport failures, false for 4xx", () => {
  assert.equal(RyaError.from(503, "").retryable, true);
  assert.equal(RyaError.from(409, { detail: { code: "E_QUEUE_CONFLICT" } }).retryable, false);
});

test("404 becomes null only on the lookups where absence is normal", async () => {
  const { rya } = client(jsonResponse(404, { detail: { code: "E_SESSION_NOT_FOUND" } }));
  assert.equal(await rya.findSession("web", "ada"), null);
  assert.equal(await rya.getQueueJob("qj_missing"), null);
});

test("a non-404 on those same lookups still throws", async () => {
  const { rya } = client(jsonResponse(500, { detail: { code: "E_RUNTIME", message: "boom" } }));
  await assert.rejects(rya.getQueueJob("qj_1"), (err) => err.code === "E_RUNTIME");
});
