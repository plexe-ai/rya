"""Production-readiness checks — the green checklist a coding agent satisfies.

A coding agent can write a handler; it doesn't inherently *know production*. This
module encodes "what production requires" as machine-checkable rules so the agent
only has to make ``rya deploy --check`` return all-green. Each finding carries a
stable ``code``, a one-line ``message``, and an exact ``fix`` the agent can act on
without a human.

- **blocks** must be fixed before deploy (``ready == len(blocks) == 0``).
- **warnings** are advisory (works, but not production-hardened).
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from .providers import resolve_provider
from .sdk.context import load_env
from .tools.registry import default_registry as _tools


def check_readiness(manifest, store, agent, project_root) -> dict:
    env = load_env(Path(project_root))
    treg = _tools()
    blocks: List[dict] = []
    warnings: List[dict] = []

    def block(code, message, fix, **extra):
        blocks.append({"code": code, "message": message, "fix": fix, **extra})

    def warn(code, message, fix, **extra):
        warnings.append({"code": code, "message": message, "fix": fix, **extra})

    # --- blocks: the agent must not be able to ship these ---------------
    if agent.event_handler() is None:
        block("E_NO_EVENT_HANDLER", "No @agent.on_event handler is registered.",
              "Add an `@agent.on_event` handler in the entrypoint.")

    if manifest.runtime != "python":
        block("E_RUNTIME_UNSUPPORTED", f"Runtime '{manifest.runtime}' is not executable by this build.",
              "Set runtime: python in the manifest.")

    gate_policy = manifest.approvals.default  # default: required_for_external_actions
    for t in manifest.tools:
        spec = treg.get(t.id)
        has_impl = spec is not None or getattr(t, "url", None) or agent.tool_handler(t.id) is not None
        if not has_impl:
            block("E_TOOL_NO_IMPL", f"Tool '{t.id}' is declared but has no implementation.",
                  f"Implement `@agent.tool('{t.id}')`, add a `url:`, or register it.", tool=t.id)

        external = bool(getattr(spec, "external_side_effects", False)) if spec else False
        if external and t.permission.value == "allowed" and gate_policy == "required_for_external_actions":
            block("E_UNGATED_SIDE_EFFECT",
                  f"Tool '{t.id}' has external side effects but permission is 'allowed'.",
                  f"Set permission: approval_required (or read_only) for '{t.id}'.", tool=t.id)

        for secret in (getattr(spec, "required_secrets", []) or []):
            if not env.get(secret):
                block("E_SECRET_UNSET", f"Secret '{secret}' (required by '{t.id}') is not set.",
                      f"rya secrets set {secret}=…", secret=secret, tool=t.id)

    # --- warnings: hardening advice -------------------------------------
    if resolve_provider(manifest.model.provider) == "mock":
        warn("W_LLM_MOCK", "The model provider resolves to the mock LLM.",
             "Set ANTHROPIC_API_KEY / OPENAI_API_KEY, or model.provider, for a real model.")

    if store.describe().get("backend") == "file":
        warn("W_STORE_FILE", "Using the file store — not durable for production.",
             "Set RYA_DATABASE_URL to a Postgres connection string.")

    if not (env.get("RYA_TRACE_WEBHOOK") or env.get("LANGFUSE_HOST")):
        warn("W_NO_TRACE_EXPORT", "Run traces are stored locally only.",
             "Configure LANGFUSE_HOST or RYA_TRACE_WEBHOOK for production observability.")

    if not (Path(project_root) / "rya.evals.yaml").exists() and not (Path(project_root) / "tests").exists():
        warn("W_NO_EVALS", "No eval suite (rya.evals.yaml) or tests/ — no behavioural checks before deploy.",
             "Add rya.evals.yaml cases (run with `rya eval`) or tests/ that assert run outcomes.")

    if not any(k.startswith("RYA_PRICE_") for k in env):
        warn("W_NO_COST_CAP", "No model pricing/cost tracking configured.",
             "Set RYA_PRICE_<MODEL>_IN / _OUT to track and cap cost.")

    return {
        "ready": len(blocks) == 0,
        "blocks": blocks,
        "warnings": warnings,
        "summary": {"blocks": len(blocks), "warnings": len(warnings)},
    }
