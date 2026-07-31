// Runtime smoke: drive a live Python `rya serve` with the built TS client.
//
// Complements `test/`: those stub fetch and prove the client's own logic, this
// proves the shapes it was written against are the shapes the server sends.
// Start `rya serve` (or `rya dev`) first, then: npm run build && npm run smoke
import { RyaClient, isFrame } from "./dist/index.js";

const rya = new RyaClient({
  baseUrl: process.env.RYA_URL ?? "http://127.0.0.1:8790",
  token: process.env.RYA_TOKEN,
});

const health = await rya.health();
console.log("health:", JSON.stringify(health));

// ---- the platform surface: trigger, approve, read back --------------------
const run = await rya.triggerEvent({ email: "ada@example.com" });
console.log("triggerEvent:", JSON.stringify(run));

if (run.status === "waiting_approval") {
  const pending = await rya.listApprovals("pending");
  console.log("pending approvals:", pending.length);
  const done = await rya.approve(pending[0].id);
  console.log("approve:", JSON.stringify(done));
}

const finalRun = await rya.getRun(run.runId);
console.log("final status:", finalRun.status, "| trace events:", finalRun.trace.length);

// ---- D6: a durable turn, streamed and resumable ---------------------------
const { turnId } = await rya.startTurn({ email: "ada@example.com", body: "hello" });
console.log("startTurn:", turnId);

let tokenChars = 0;
let lastSeq = null;
for await (const frame of rya.streamTurn(turnId, { untilFinal: true })) {
  if (isFrame(frame, "token")) tokenChars += frame.data.text.length;
  if (isFrame(frame, "run")) console.log("  run frame:", frame.data.status);
  lastSeq = frame.seq ?? lastSeq;
}
console.log("streamed", tokenChars, "token chars; last seq:", lastSeq);

// The buffer is durable, so tailing the same finished turn again replays it
// identically from any cursor — which is what makes a dropped socket recoverable.
const midpoint = Math.max(0, Math.floor((lastSeq ?? 0) / 2));
const resumed = [];
for await (const frame of rya.streamTurn(turnId, { lastEventId: midpoint, untilFinal: true })) {
  resumed.push(frame.seq);
}
console.log(`resumed from seq ${midpoint}:`, resumed.length, "frames; first:", resumed[0]);

// ---- D14: the SDK-free durable job API ------------------------------------
const job = await rya.enqueueJob("smoke.noop", { n: 1 }, { jobId: `smoke-${Date.now()}` });
console.log("enqueued:", job.id, job.status);
const claimed = await rya.claimQueueJobs({ workerId: "smoke", types: ["smoke.noop"], limit: 1 });
console.log("claimed:", claimed.length);
if (claimed.length) {
  const finished = await rya.completeQueueJob(claimed[0].id, "smoke", { ok: true });
  console.log("completed:", finished.status);
}
console.log("queue stats:", JSON.stringify(await rya.queueStats()));
