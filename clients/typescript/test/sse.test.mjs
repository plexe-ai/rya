// The SSE parser in isolation. It owns the `id:` field, which is the durable
// buffer's sequence number and therefore the only resume cursor a client has —
// mangling one is how a turn gets lost.

import assert from "node:assert/strict";
import test from "node:test";

import { parseData, readSse } from "../dist/index.js";
import { collect } from "./helpers.mjs";

function stream(...chunks) {
  const enc = new TextEncoder();
  return new ReadableStream({
    start(c) {
      for (const chunk of chunks) c.enqueue(enc.encode(chunk));
      c.close();
    },
  });
}

test("parses id, event and data, and defaults a missing event name", async () => {
  const events = await collect(
    readSse(stream("id: 3\nevent: token\ndata: {\"text\":\"hi\"}\n\n", "data: bare\n\n"))
  );
  assert.deepEqual(events, [
    { event: "token", id: "3", data: '{"text":"hi"}' },
    { event: "message", id: null, data: "bare" },
  ]);
});

test("an event split across chunk boundaries is reassembled", async () => {
  const events = await collect(readSse(stream("id: 1\nev", "ent: token\ndata: {\"te", 'xt":"ab"}\n\n')));
  assert.deepEqual(events, [{ event: "token", id: "1", data: '{"text":"ab"}' }]);
});

test("only ONE leading space is stripped, so token whitespace survives", async () => {
  // `trim()` here would silently eat the indentation out of streamed code.
  const [event] = await collect(readSse(stream('data: {"text":"  indented"}\n\n')));
  assert.equal(JSON.parse(event.data).text, "  indented");
});

test("multi-line data joins with a newline", async () => {
  const [event] = await collect(readSse(stream("event: log\ndata: line one\ndata: line two\n\n")));
  assert.equal(event.data, "line one\nline two");
});

test("comment frames are swallowed — keep-alive is not a turn frame", async () => {
  const events = await collect(
    readSse(stream(": keep-alive\n\n", ": idle-timeout\n\n", "id: 0\nevent: run\ndata: {}\n\n"))
  );
  assert.deepEqual(events.map((e) => e.event), ["run"]);
});

test("CRLF line endings parse the same as LF", async () => {
  const [event] = await collect(readSse(stream("id: 7\r\nevent: trace\r\ndata: {}\r\n\r\n")));
  assert.deepEqual(event, { event: "trace", id: "7", data: "{}" });
});

test("a stream that ends without a trailing blank line still delivers the event", async () => {
  const events = await collect(readSse(stream("id: 2\nevent: run\ndata: {}")));
  assert.deepEqual(events, [{ event: "run", id: "2", data: "{}" }]);
});

test("parseData tolerates the empty payload a keep-alive leaves behind", () => {
  assert.equal(parseData(""), null);
  assert.deepEqual(parseData('{"a":1}'), { a: 1 });
  assert.equal(parseData("not json"), "not json");
});
