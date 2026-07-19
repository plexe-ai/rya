# `rya.skills` - bundled coding-agent skills

Progressive-disclosure guides installed into a coding agent (Claude Code / Codex
/ Cursor) so it drives Rya correctly without guessing the workflow.

## Files

- `skill_data.py` - `SKILLS` (the skill definitions) and `SKILL_MD` (the markdown
  bodies). `rya skills` installs them.

## Skills

- authoring skill - how to build an agent (manifest, handlers, ctx, evals,
  `deploy --check`).
- ops skill - how to operate a running agent (runs, approvals, kill switches,
  queue, traces).

## Notes

- Keep skills terse and action-oriented; they are consumed by an agent, not read
  linearly by a human. Update them when the CLI/MCP surface changes.
