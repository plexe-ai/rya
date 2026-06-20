"""Structured JSON logging.

Logs go to stderr as one JSON object per line so a coding agent can parse them
without colliding with ``--json`` payloads on stdout. The authoritative,
queryable record of a run is its trace (persisted on the run); this is the live
stream.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any, Optional


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def structured(level: str, message: str, **fields: Any) -> dict:
    rec = {"ts": _ts(), "level": level, "message": message}
    rec.update(fields)
    return rec


def emit_log(level: str, message: str, *, run_id: Optional[str] = None, stream=None, **fields: Any) -> dict:
    rec = structured(level, message, **fields)
    if run_id:
        rec["runId"] = run_id
    out = stream or sys.stderr
    out.write(json.dumps(rec, default=str) + "\n")
    out.flush()
    return rec
