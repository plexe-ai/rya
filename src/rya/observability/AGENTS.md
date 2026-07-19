# `rya.observability` - logs, usage, export

Rya owns the durable **journal** (in the store) because replay depends on it;
this module is the observability that falls out of it and the exporters to
best-of-breed tools.

## Files

- `logs.py` - `emit_log` / structured logging (`ctx.logs.*`).
- `usage.py` - `run_usage(run)` computes token + cost totals from the run's
  trace (not a live accumulator, so it's correct across pause/resume replays).
  Cost is reported only when `RYA_PRICE_<MODEL>_{IN,OUT}` env prices are set -
  Rya never hard-codes provider prices.
- `export.py` - `export_run(run)` ships a finished run to a configured backend:
  Langfuse (`LANGFUSE_HOST`), OTLP, or a webhook (`RYA_TRACE_WEBHOOK`).
  Best-effort: never lets an export failure break a run.

## Division of labor

The journal is the source of truth the engine resumes from (mandatory). Langfuse
is where a human looks at traces (Rya exports to it). Ingested external runs
(`POST /runs/ingest`) flow through `export_run` too.
