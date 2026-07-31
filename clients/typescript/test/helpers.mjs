// Test doubles. Nothing here touches the network or a live server: the SDK's
// only ambient dependency is `fetch`, so stubbing that one function is enough to
// exercise request construction, error mapping and stream resume end to end.

const enc = new TextEncoder();

/** A JSON response, as the transport consumes it (`ok`/`status`/`text()`). */
export function jsonResponse(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => (typeof payload === "string" ? payload : JSON.stringify(payload)),
  };
}

/** A response whose body is not JSON — a proxy error page, FastAPI's bare 500. */
export function textResponse(status, text) {
  return { ok: status >= 200 && status < 300, status, text: async () => text };
}

/**
 * An SSE response FACTORY (`recordingFetch` calls functions), so a reconnect
 * gets a fresh, unlocked stream rather than the exhausted one from last time.
 *
 * Chunks are delivered from `pull`, not `start`: a `ReadableStream` discards its
 * queue when the controller errors, so enqueuing everything up front then
 * erroring would deliver nothing at all — which is not what a socket reset
 * looks like. `end: "close"` finishes cleanly (what the server does after
 * `: idle-timeout`); `end: "error"` breaks the socket mid-stream.
 */
export function sseResponse(chunks, end = "close") {
  return () => {
    let i = 0;
    const body = new ReadableStream({
      pull(controller) {
        if (i < chunks.length) {
          controller.enqueue(enc.encode(chunks[i++]));
          return;
        }
        if (end === "error") controller.error(new Error("socket reset"));
        else controller.close();
      },
    });
    return { ok: true, status: 200, body, text: async () => "" };
  };
}

/** Format one durable-turn frame exactly as `api/app.py` writes it. */
export function frame(seq, kind, data) {
  return `id: ${seq}\nevent: ${kind}\ndata: ${JSON.stringify(data)}\n\n`;
}

/**
 * A fetch stub that records every call and replays a scripted list of responses.
 * `responders` may be values or `(request, index) => response` functions.
 */
export function recordingFetch(responders) {
  const calls = [];
  const list = Array.isArray(responders) ? responders : [responders];
  const impl = async (url, init = {}) => {
    const parsed = new URL(url);
    const record = {
      url,
      path: parsed.pathname,
      query: Object.fromEntries(parsed.searchParams),
      method: init.method,
      headers: init.headers ?? {},
      body: init.body,
      signal: init.signal,
    };
    calls.push(record);
    const responder = list[Math.min(calls.length - 1, list.length - 1)];
    return typeof responder === "function" ? responder(record, calls.length - 1) : responder;
  };
  impl.calls = calls;
  return impl;
}

/** Drain an async iterator into an array. */
export async function collect(iter) {
  const out = [];
  for await (const item of iter) out.push(item);
  return out;
}
