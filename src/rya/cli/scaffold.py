"""Project scaffolding for ``rya create`` / ``rya init``.

Generates a project that runs end-to-end immediately: ``rya create x`` then
``cd x`` then ``rya dev`` then ``rya events send`` works with no edits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

MANIFEST_TEMPLATE = """\
name: {name}
runtime: python
entrypoint: src/agent.py
version: 0.1.0
environment: local
owner: you@example.com
instructions: >
  A production-style follow-up agent scaffolded by Rya. It looks up a customer,
  scores churn risk, drafts a retention message, and asks for human approval
  before sending.

model:
  default: mock-llm
  fallback: mock-llm-mini

memory:
  type: managed
  collections:
    - conversations
    - customer_context

tools:
  - id: crm.lookup
    permission: allowed
  - id: calendar.read
    permission: read_only
  - id: email.send
    permission: approval_required

models:
  - id: churn-risk-v1
    type: custom
    permission: allowed

channels:
  - type: webhook
    path: /inbound
  - type: slack
    enabled: false

triggers:
  - id: daily-followups
    type: cron
    schedule: "0 9 * * *"
    handler: daily_followup

approvals:
  default: required_for_external_actions

observability:
  logs: true
  traces: true
  audit: true
"""

AGENT_TEMPLATE = '''\
"""Example follow-up agent.

Demonstrates the full vertical slice: event handling, memory, a tool call, a
model call, a scheduled job, an LLM draft, a human approval gate, and a mock
channel send after approval.
"""

from rya import define_agent

agent = define_agent()


@agent.on_event
async def handle_event(ctx, event):
    email = event.payload.get("email", "unknown@example.com")
    ctx.logs.info("handling event", type=event.type, email=email)

    # 1. Open (or continue) the conversation for this contact, and record the
    #    inbound message. Threaded by the inbound channel + external id so a reply
    #    lands in the same session across restarts — durable conversation handling.
    channel = event.payload.get("channel", "email")
    external_id = event.payload.get("externalId") or email
    session = await ctx.sessions.get_or_create(channel, external_id, title=external_id)
    await ctx.sessions.append(session["id"], "user", event.payload.get("body", "(inbound event)"))
    await ctx.memory.append("conversations", {"event": event.model_dump()})
    # Long-term memory: consolidate what we learn about this contact (deduped),
    # and keep an always-in-context persona block.
    await ctx.memory.block_set("persona", "You are a calm, concise retention agent for Acme.")
    if event.payload.get("body"):
        await ctx.memory.remember(f"{email} said: {event.payload['body']}")

    # 2. Look up the customer (allowed tool).
    customer = await ctx.tools.call("crm.lookup", {"email": email})

    # 3. Score churn risk via a custom model.
    risk = await ctx.models.call("churn-risk-v1", {"customer_id": customer["id"]})

    # 4. Schedule a delayed follow-up job.
    await ctx.jobs.schedule(
        "daily_followup", {"customer_id": customer["id"]}, delay_seconds=86400
    )

    if risk["score"] > 0.8:
        # 5. Draft a message with the LLM.
        message = await ctx.llm.respond(
            system="Draft a concise retention follow-up.",
            input={"customer": customer, "risk": risk},
        )
        # Record the drafted reply on the conversation thread.
        await ctx.sessions.append(session["id"], "assistant", message.text)

        # 6. Request human approval before sending (this PAUSES the run).
        result = await ctx.approvals.request(
            title="Send retention follow-up",
            body=message.text,
            action={
                "tool": "email.send",
                "input": {
                    "to": customer["email"],
                    "subject": "Quick follow-up",
                    "body": message.text,
                },
            },
        )

        # 7. Resumes here after approval; the email was sent as the action.
        await ctx.channels.send(
            "email",
            {"to": customer["email"], "messageId": result["actionResult"]["messageId"]},
        )
        ctx.logs.info("follow-up sent", customer=customer["id"])

    return {"customer": customer["id"], "risk": risk["score"]}


@agent.job("daily_followup")
async def daily_followup(ctx, job):
    """Runs later as a background job (see `rya jobs run`)."""
    ctx.logs.info("daily follow-up job", customer=job.payload.get("customer_id"))
    await ctx.channels.send("email", {"customer": job.payload.get("customer_id"), "kind": "daily"})
    return {"ok": True}
'''

TOOLS_TEMPLATE = '''\
"""Custom tools for this agent.

In the local slice tools are mocked by Rya's built-in registry (crm.lookup,
calendar.read, email.send). Register your own here when wiring real tools.
"""
'''

MODELS_TEMPLATE = '''\
"""Custom models for this agent.

In the local slice models are mocked by Rya's built-in registry
(churn-risk-v1, customer-intent-v2).
"""
'''

TEST_TEMPLATE = '''\
"""Smoke test: an event run reaches the approval pause."""

from pathlib import Path

from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.store import Store


def test_event_pauses_for_approval():
    root = Path(__file__).resolve().parents[1]
    manifest = load_manifest(root / "rya.agent.yaml")
    agent = load_agent(manifest, root)
    engine = Engine(manifest, agent, Store(root), root)

    run = engine.run_event("message.received", {"email": "ada@example.com"})
    assert run["status"] == "waiting_approval"
    assert run["pendingApproval"]
'''

GUARD_TEMPLATE = """\
# Action Guard — egress firewall. Every outbound request the agent makes is
# checked here before it leaves the process. deny beats allow; default applies
# when nothing matches. Edit + save = takes effect immediately.
ssrf: true
default: deny
fail: closed
policy: >
  This agent may call its own backend and approved model providers. It must
  never exfiltrate data to webhooks, pastebins, or chat bridges, and never call
  a payment processor directly.
rules:
  - {action: allow, kind: glob, pattern: "https://api.anthropic.com/*", note: Claude inference}
  - {action: allow, kind: glob, pattern: "https://api.openai.com/*", note: OpenAI inference / embeddings}
  - {action: deny, kind: glob, pattern: "https://webhook.site/*", note: request-capture / exfiltration host}
  - {action: deny, kind: glob, pattern: "https://hooks.slack.com/*", note: no outbound chat webhooks}
  - {action: deny, kind: glob, pattern: "https://api.stripe.com/*", note: no direct card charges}
"""

ENV_TEMPLATE = """\
# Secrets are read from here by ctx.secrets.get(NAME). Never commit real values.
CRM_API_KEY=dev-crm-key
EMAIL_API_KEY=dev-email-key
"""

PYPROJECT_TEMPLATE = """\
[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["rya"]
"""

README_TEMPLATE = """\
# {name}

A Rya agent project.

```bash
rya dev                                   # validate + inspect the agent
rya events send --type message.received \\
  --payload '{{"email":"ada@example.com"}}'  # trigger a run (pauses for approval)
rya approvals list                        # see the pending approval
rya approvals approve <approval_id>       # resume the run; email is "sent"
rya runs list                             # see the run
rya runs trace <run_id>                   # full trace
```

Add `--json` to any command for machine-readable output.
"""


EVALS_TEMPLATE = """\
# Eval suite — run with `rya eval`. Each case fires a real event and scores the
# resulting run. `rya eval` exits non-zero if any case fails, so you can gate a
# deploy on agent BEHAVIOUR, not just readiness.
evals:
  - id: high_risk_pauses_for_approval
    trigger:
      type: message.received
      payload: { email: "risk@acme.io", body: "thinking about cancelling" }
    expect:
      status: waiting_approval          # high churn risk → human gate
      tools_called: [crm.lookup]        # it looked the customer up
      approval_requested: true          # and paused for approval
      trace_lacks: run.failed           # nothing errored

  - id: handles_event_without_error
    trigger:
      type: message.received
      payload: { email: "ada@example.com", body: "quick question" }
    expect:
      no_failure: true
      max_tokens: 100000                # cost guardrail
"""


def scaffold_files(name: str) -> Dict[str, str]:
    return {
        "rya.agent.yaml": MANIFEST_TEMPLATE.format(name=name),
        "rya.guard.yaml": GUARD_TEMPLATE,
        "rya.evals.yaml": EVALS_TEMPLATE,
        "pyproject.toml": PYPROJECT_TEMPLATE.format(name=name),
        ".env.example": ENV_TEMPLATE,
        ".env": ENV_TEMPLATE,
        "README.md": README_TEMPLATE.format(name=name),
        "src/agent.py": AGENT_TEMPLATE,
        "src/tools.py": TOOLS_TEMPLATE,
        "src/models.py": MODELS_TEMPLATE,
        "tests/test_agent.py": TEST_TEMPLATE,
    }


def write_project(target: Path, name: str, *, overwrite: bool = False) -> list[str]:
    written = []
    for rel, content in scaffold_files(name).items():
        path = target / rel
        if path.exists() and not overwrite:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        written.append(rel)
    return written
