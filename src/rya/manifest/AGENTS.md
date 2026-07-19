# `rya.manifest` - the agent declaration

`rya.agent.yaml` is the contract the runtime enforces. This module is its schema
and loader.

## Files

- `schema.py` - Pydantic models. `Manifest` is the root; key nested types:
  - `ModelBlock` (default/fallback/temperature/max_tokens) + `ModelRoute` under
    `routes:` for per-purpose models (compose/extract/classify).
  - `ToolDecl` - `id`, `permission` (see `Permission`), optional `url:` (HTTP
    tool), `provider`/`scopes` (scoped credentials), `pin` (server-side arg
    pinning), `input_schema` (JSON Schema handed to the model).
  - `Permission` enum: `read_only` | `allowed` | `approval_required` | `disabled`.
  - `MemoryBlock`, `ModelDecl`, `ChannelDecl`, `TriggerDecl` (cron needs a
    schedule), `ApprovalsBlock`, `ObservabilityBlock`.
- `loader.py` - `load_manifest(path)` reads + validates, raising stable
  `E_MANIFEST_*` codes with fix hints.

## Notes

- `Manifest.tool_permission(id)` / `model_permission(id)` are the lookups the
  runtime uses at call time.
- Adding a manifest field: update `schema.py`, then wherever it is consumed
  (usually `sdk/context.py` for tool behavior, `snapshot.py` for the console).
