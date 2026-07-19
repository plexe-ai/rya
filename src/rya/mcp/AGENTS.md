# `rya.mcp` - MCP server for coding agents

Rya is coding-agent-first: Claude Code / Codex / Cursor drive the whole control
plane over MCP. This module exposes Rya's operations as MCP tools.

## Files

- `ops.py` - plain, testable functions (no MCP dependency): create/validate/
  trigger/approve/provision/connect, etc. These are the real logic.
- `server.py` - wraps `ops` as FastMCP tools. `mounted_app()` returns an ASGI app
  that `api/app.py` mounts at `/mcp` so `rya serve` is one origin for API +
  console + MCP. `rya mcp` runs it over stdio for local coding agents.

## Notes

- Test against `ops` (fast, no MCP runtime); `server` is a thin adapter.
- Requires the `[mcp]` extra (FastMCP); `api` degrades gracefully if absent.
- Remote MCP is privileged: when `RYA_TOKEN` is set it is required on `/mcp`.
