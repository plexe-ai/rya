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

```bash
rya dev
rya events send --type message.received --payload '{"email":"c@csa.test","body":"hello"}'
rya events send --type message.received --payload '{"email":"c@csa.test","body":"email the student","sendEmail":true}'
rya approvals    # the outbound email is paused here
```
