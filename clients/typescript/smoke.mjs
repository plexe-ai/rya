// Runtime smoke: drive a live Python `rya serve` with the built TS client.
import { RyaClient } from "./dist/index.js";

const rya = new RyaClient({ baseUrl: process.env.RYA_URL ?? "http://127.0.0.1:8790" });

const health = await rya.health();
console.log("health:", JSON.stringify(health));

const run = await rya.triggerEvent({ email: "ada@example.com" });
console.log("triggerEvent:", JSON.stringify(run));

if (run.status === "waiting_approval") {
  const pending = await rya.listApprovals("pending");
  console.log("pending approvals:", pending.length);
  const done = await rya.approve(pending[0].id);
  console.log("approve:", JSON.stringify(done));
}

const finalRun = await rya.getRun(run.runId);
console.log("final status:", finalRun.status);
