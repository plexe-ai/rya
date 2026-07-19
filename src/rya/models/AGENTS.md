# `rya.models` - custom-model registry

Custom models an agent calls via `ctx.models.call(id, input)` - distinct from
the foundation LLM behind `ctx.llm` (that lives in `providers/`).

## Files

- `registry.py` - `ModelSpec` (id, type, version, fn) and `ModelRegistry`.
  `default_registry()` provides deterministic mock models for the demo domain
  (e.g. `churn-risk-v1`). Real custom models register their own spec.

## Notes

- Models are permissioned via the manifest `models:` block (`ModelDecl`), same
  tiers as tools.
- `snapshot.py` surfaces model call-counts and versions to the console.
