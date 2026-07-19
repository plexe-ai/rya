# `rya.tools` - tool registry + built-ins

## Files

- `registry.py` - `ToolSpec` (id, description, fn, side-effect flag, required
  secrets, `input_schema`, `mock` flag) and `ToolRegistry`. `default_registry()`
  wires the demo-domain mocks (`crm.lookup`, `calendar.read`, `email.send`) AND
  the real built-ins.
- `builtins.py` - real, general-purpose tools that do actual IO: `web.fetch`
  (guarded HTTP GET, HTML stripped) and `http.request` (any method). Both call
  `guard.check_egress` before a byte leaves the process.

## How tools resolve at call time (see sdk/context.py `_Tools.call`)

1. `@agent.tool` handler in the agent's code (real, project-defined).
2. HTTP tool: manifest `url:` - POSTs input as JSON, scoped credential injected.
3. Mock registry fallback (this module's `default_registry`).

## Notes

- `ToolSpec.mock=True` marks deterministic placeholders; `snapshot.py` surfaces
  this as `mockImpl` so the console badges demo data (real only if a project
  handler or `url:` overrides the mock).
- Permission is resolved from the manifest (+ runtime kill switches), never
  hard-coded here.
- The `default` scaffold template declares NO mock domain tools - only
  `web.fetch` + a project `@agent.tool`. The mock trio is the `demo` template.
