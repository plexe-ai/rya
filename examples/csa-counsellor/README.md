# csa-counsellor

ChatStudyAbroad's counsellor agent represented on Rya: the production agent's
real 28-tool registry with its TRUE governance tiers (hidden -> disabled,
confirm-gated -> approval_required, student-scoped -> pinned camsId), model
routes mirroring production model usage, memory collections, and the egress
guard - all visible and manageable in the Rya console (tools, kill switches,
memory, guard, approvals, runs, durable turns).

Tools are declared for governance; each gets wired to the live CSA endpoint by
adding its `url:` (Phase 1 of the migration plan). The handler runs the real
conversation shell (sessions, routed models, fact extraction, the runtime
approval gate on outbound email) and calls no unwired tool.

> **This is a governance showcase, not a mirror of the production agent** — and
> the two have since diverged. As of 2026-07-29 this example declares 28 tools
> with **0 `url:` and 4 `pin:`**, and its `src/agent.py` is 60 lines with **no
> `@agent.tool` handlers**. The real agent (`chatstudyabroad/rya-agent`) is 3,040
> lines with **8 `url:`, 9 `pin:`**, 26 handlers, and a live Crizac client. Phase 1
> has partly happened there; this snapshot predates it. Treat the tool *registry
> and permission tiers* here as representative and the *wiring* as illustrative.

```bash
rya dev
rya events send --type message.received --payload '{"email":"c@csa.test","body":"hello"}'
rya events send --type message.received --payload '{"email":"c@csa.test","body":"email the student","sendEmail":true}'
rya approvals    # the outbound email is paused here
```
