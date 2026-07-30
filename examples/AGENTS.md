# `examples` - reference agents

Runnable agent projects that show how to build on Rya. Each is a normal project
(`rya.agent.yaml` + `src/agent.py`) you can `cd` into and `rya dev`.

## Projects

- `followup_agent/` - the demo vertical slice: event -> memory -> tool ->
  custom model -> scheduled job -> LLM draft -> approval gate -> channel send.
  Exercises every core primitive in one run.
- `csa-counsellor/` - a representation of a real counsellor agent (study-abroad
  domain) on Rya: 28 tools with true governance tiers (hidden -> disabled,
  confirm-gated -> approval_required, student-scoped -> pinned), model routes,
  guard + grounding, evals. Tools are declared for governance/visibility; wire
  each to a live endpoint via its manifest `url:`.
- `loan-renewal/` - a Bedrock-backed document pipeline: intent routing -> CIF
  lookup -> document checklist -> per-PDF extraction jobs -> schema derivation ->
  a cited report gated by the grounding check -> an approval-gated DB write.
- `crizac/` - one durable runtime behind a fleet: each audience (counsellor,
  student, insights, visa/CAS, university, finance) runs as a WORKSPACE on it,
  isolated per agent inside one shared enforcement envelope.

## Notes

- These live IN this repo, so they are developed against the working tree. The
  external-author experience — thin `rya` SDK from a URL, `rya publish` over HTTP
  — lives in the separate `rya-examples` repo. Add an example there when the point
  is "how do I build on Rya from my own repo"; add it here when the point is
  "which primitive does this exercise".
- No manifest may carry an `environment:` key. D11 removed it: one content-hashed
  bundle is promoted between environments, so the loader strips it with a warning
  and `deploy`/`publish` refuse it outright (`E_MANIFEST_ENVIRONMENT`).
- `cd` into any of them and run `rya check --json` — it should report
  `"ready": true` before you touch anything else.
- Deploy a specific example with `deploy/aws/Dockerfile.project`
  (`--build-arg PROJECT=examples/<name>`).
- `csa-counsellor` contains customer-domain naming: keep it out of any public
  (open-sourced) build, or scrub first. This repo is private.
