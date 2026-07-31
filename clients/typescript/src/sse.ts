/**
 * A spec-shaped Server-Sent Events parser.
 *
 * Split out as a pure function over a byte stream because it is the piece that
 * has to be right for D6 to hold: the `id:` field IS the durable buffer's
 * sequence number, and dropping or mangling one loses the client's only resume
 * cursor. Being a plain generator, it is testable against a synthetic stream
 * with no server and no network.
 */

/** A decoded SSE event, before the payload is interpreted. */
export interface SseEvent {
  /** The `event:` field, defaulting to `"message"` per the SSE spec. */
  event: string;
  /** The `id:` field if present, else `null`. */
  id: string | null;
  /** `data:` lines joined with `\n`, un-parsed. */
  data: string;
}

/**
 * Decode an SSE byte stream into events.
 *
 * Comment frames (`: keep-alive`, `: idle-timeout`) are swallowed: the API sends
 * them to defeat proxy idle timeouts and to signal "reconnect now", neither of
 * which is a turn frame. The stream simply ending after `: idle-timeout` is the
 * caller's cue to resume — see `turns.ts`.
 */
export async function* readSse(
  body: ReadableStream<Uint8Array>,
  opts?: { signal?: AbortSignal }
): AsyncGenerator<SseEvent, void, void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let event = "";
  let id: string | null = null;
  let data: string[] = [];
  let sawField = false;

  const flush = (): SseEvent | null => {
    if (!sawField) return null;
    const out: SseEvent = { event: event || "message", id, data: data.join("\n") };
    event = "";
    id = null;
    data = [];
    sawField = false;
    return out;
  };

  /** Consume one complete line; returns an event when the line dispatched one. */
  const feed = (raw: string): SseEvent | null => {
    const line = raw.replace(/\r$/, "");
    if (line === "") return flush();
    if (line.startsWith(":")) return null; // comment / keep-alive

    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    // Exactly ONE leading space is stripped, per the SSE spec — `trim()` would
    // eat significant whitespace out of a streamed token chunk.
    let text = colon === -1 ? "" : line.slice(colon + 1);
    if (text.startsWith(" ")) text = text.slice(1);

    if (field === "event") {
      event = text;
      sawField = true;
    } else if (field === "data") {
      data.push(text);
      sawField = true;
    } else if (field === "id") {
      // The spec says ignore an id containing a NUL; Rya sends a decimal seq.
      if (!text.includes(" ")) id = text;
      sawField = true;
    }
    // `retry:` and unknown fields are ignored — reconnect timing is ours.
    return null;
  };

  try {
    for (;;) {
      if (opts?.signal?.aborted) return;
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let nl: number;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl);
        buffer = buffer.slice(nl + 1);
        const decoded = feed(line);
        if (decoded) yield decoded;
      }
    }
    // A truncated stream can leave a complete event with no terminating blank
    // line. Emitting it beats dropping a frame the buffer already committed —
    // and the seq dedupe in `turns.ts` makes an over-eager tail harmless.
    if (buffer) feed(buffer);
    const tail = flush();
    if (tail) yield tail;
  } finally {
    // Releasing matters on an early `break`/`return` from the consumer: without
    // it the socket stays open until GC and a reconnect races the old one.
    await reader.cancel().catch(() => undefined);
  }
}

/** Parse an event's `data` as JSON, tolerating the empty payloads keep-alives leave. */
export function parseData(data: string): unknown {
  if (!data) return null;
  try {
    return JSON.parse(data) as unknown;
  } catch {
    return data;
  }
}
