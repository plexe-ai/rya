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

## Notes

- Deploy a specific example with `deploy/aws/Dockerfile.project`
  (`--build-arg PROJECT=examples/<name>`).
- `csa-counsellor` contains customer-domain naming: keep it out of any public
  (open-sourced) build, or scrub first. This repo is private.
