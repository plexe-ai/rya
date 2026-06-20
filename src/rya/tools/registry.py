"""Mock tool registry.

Each tool is explicit: id, description, schemas, side-effect flag, required
secrets. In the local slice the implementations are deterministic mocks so runs
are reproducible. Permission is resolved from the manifest at call time (see
``RuntimeContext._resolve_tool_permission``), not hard-coded here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass
class ToolSpec:
    id: str
    name: str
    description: str
    fn: Callable[[dict], dict]
    external_side_effects: bool = False
    required_secrets: List[str] = field(default_factory=list)
    input_schema: Optional[dict] = None
    output_schema: Optional[dict] = None
    mock: bool = False   # deterministic placeholder — not real IO (flagged by readiness)

    def public(self, permission: str) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "permission": permission,
            "externalSideEffects": self.external_side_effects,
            "requiredSecrets": self.required_secrets,
        }


def _crm_lookup(inp: dict) -> dict:
    email = inp.get("email", "unknown@example.com")
    cid = "cus_" + str(abs(hash(email)) % 100000)
    return {
        "id": cid,
        "email": email,
        "name": email.split("@")[0].replace(".", " ").title(),
        "plan": "pro",
        "mrr": 199,
    }


def _calendar_read(inp: dict) -> dict:
    return {"events": [], "busy": False}


def _email_send(inp: dict) -> dict:
    return {
        "delivered": True,
        "channel": "email",
        "to": inp.get("to"),
        "subject": inp.get("subject"),
        "messageId": "msg_" + str(abs(hash(str(inp))) % 1000000),
    }


def default_registry() -> "ToolRegistry":
    reg = ToolRegistry()
    reg.register(ToolSpec(
        id="crm.lookup",
        name="CRM Lookup",
        description="Look up a customer record by email.",
        fn=_crm_lookup,
        external_side_effects=False,
        required_secrets=["CRM_API_KEY"],
        mock=True,
    ))
    reg.register(ToolSpec(
        id="calendar.read",
        name="Calendar Read",
        description="Read calendar availability (read only).",
        fn=_calendar_read,
        external_side_effects=False,
        mock=True,
    ))
    reg.register(ToolSpec(
        id="email.send",
        name="Email Send",
        description="Send an email. External side effect — gate behind approval.",
        fn=_email_send,
        external_side_effects=True,
        required_secrets=["EMAIL_API_KEY"],
        mock=True,
    ))
    # --- real, general-purpose built-ins (actual IO, Action-Guard governed) ---
    from .builtins import http_request, web_fetch
    reg.register(ToolSpec(
        id="web.fetch",
        name="Web Fetch",
        description="Fetch a URL and return its readable text content (HTML stripped).",
        fn=web_fetch,
        external_side_effects=False,  # read-only GET
        input_schema={"type": "object", "required": ["url"], "properties": {
            "url": {"type": "string", "description": "The URL to fetch."},
            "maxChars": {"type": "integer", "description": "Max characters of text to return."}}},
    ))
    reg.register(ToolSpec(
        id="http.request",
        name="HTTP Request",
        description="Make an HTTP request (GET/POST/...). May have side effects — gate writes behind approval.",
        fn=http_request,
        external_side_effects=True,  # can POST/mutate; an agent should gate or scope it
        input_schema={"type": "object", "required": ["url"], "properties": {
            "url": {"type": "string"}, "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
            "headers": {"type": "object"}, "body": {}}},
    ))
    return reg


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.id] = spec

    def get(self, tool_id: str) -> Optional[ToolSpec]:
        return self._tools.get(tool_id)

    def all(self) -> List[ToolSpec]:
        return list(self._tools.values())
