# `rya.skills` - bundled coding-agent skills

Progressive-disclosure guides installed into a coding agent (Claude Code / Codex
/ Cursor) so it drives Rya correctly without guessing the workflow.

## Files

- `skill_data.py` - `SKILLS` (the skill definitions) and `SKILL_MD` (the markdown
  bodies). `rya skills` installs them.

**Two copies, by hand.** The bodies here are duplicated from `skills/<name>/SKILL.md`
at the repo root. That file is what a human reads; this module is what SHIPS (it is
in the SDK wheel, and `rya skills install` writes from it), so editing only the
root file changes the documentation while every installed agent keeps the old text.
Edit both. `tests/test_skills.py` fails if they diverge.

## Skills

- authoring skill - how to build an agent (manifest, handlers, ctx, evals,
  `deploy --check`).
- ops skill - how to operate a running agent (runs, approvals, kill switches,
  queue, traces) and how to ship one (`deploy --env` from the platform,
  `publish --env` from a client repo, versions/environments).

## Notes

- Keep skills terse and action-oriented; they are consumed by an agent, not read
  linearly by a human. Update them when the CLI/MCP surface changes.
