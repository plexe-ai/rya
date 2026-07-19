# `rya.approvals` - the pause/resume signal

Human-in-the-loop is core infrastructure, not an add-on: a run literally pauses
its coroutine and persists, then resumes in a separate process by replay.

## What's here

- `PausedForApproval` - raised by `ctx.approvals.request(...)`; the engine
  catches it, sets the run to `waiting_approval`, and persists the journal.
- `ApprovalRejected` - raised on replay when the approval was rejected; the
  engine maps it to a `rejected` run.

## Flow

`ctx.approvals.request(title, body, action)` records a pending approval + a
journal entry, then raises `PausedForApproval`. `engine.approve(id)` executes
the gated `action` (through the real tool/channel seam), marks the journal entry
approved, and re-runs the handler - memoized steps skip, the code after the
approval runs for real. See `runtime/engine.py` (`approve`/`reject`) and
`turns.py` (`resolve_on_stream`) for the streaming continuation.
