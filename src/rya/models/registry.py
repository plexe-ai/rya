"""Mock model registry + gateway.

Models are callable, permissioned, versioned, and observable inside agent
workflows. In the local slice every model is a deterministic mock; the registry
records latency/version metadata so the run trace looks like a real gateway.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional


@dataclass
class ModelSpec:
    id: str
    type: str  # llm | custom | external
    version: str
    fn: Callable[[dict], dict]
    description: str = ""
    # Fixed mock latency (ms) so traces are reproducible.
    mock_latency_ms: int = 40

    def public(self, permission: str) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "version": self.version,
            "permission": permission,
            "description": self.description,
        }


def _churn_risk(inp: dict) -> dict:
    # Deterministic high-risk score so the example approval path always triggers.
    return {"score": 0.86, "label": "high", "factors": ["low_logins", "ticket_spike"]}


def _intent(inp: dict) -> dict:
    text = (inp.get("text") or inp.get("message") or "").lower()
    intent = "cancel" if "cancel" in text else "support"
    return {"intent": intent, "confidence": 0.91}


def default_registry() -> "ModelRegistry":
    reg = ModelRegistry()
    reg.register(ModelSpec(
        id="churn-risk-v1",
        type="custom",
        version="1.0.0",
        fn=_churn_risk,
        description="Customer churn risk classifier (mock).",
        mock_latency_ms=55,
    ))
    reg.register(ModelSpec(
        id="customer-intent-v2",
        type="custom",
        version="2.0.0",
        fn=_intent,
        description="Customer message intent classifier (mock).",
        mock_latency_ms=30,
    ))
    return reg


class ModelRegistry:
    def __init__(self) -> None:
        self._models: Dict[str, ModelSpec] = {}

    def register(self, spec: ModelSpec) -> None:
        self._models[spec.id] = spec

    def get(self, model_id: str) -> Optional[ModelSpec]:
        return self._models.get(model_id)

    def all(self) -> List[ModelSpec]:
        return list(self._models.values())
