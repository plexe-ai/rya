"""Channel provider seam for ``ctx.channels.send`` — real delivery, mock fallback.

openclaw only mocks channels (a local "outbox"), so there's no implementation to
copy — but the same factory shape as the LLM seam applies. Real delivery is
opt-in via env, so local/CI stays offline and deterministic:

- ``slack``  → ``SLACK_WEBHOOK_URL``  (Slack incoming webhook)
- ``email``  → ``RESEND_API_KEY``     (Resend HTTP API; from = ``RYA_EMAIL_FROM``)
- any type   → ``RYA_CHANNEL_<TYPE>_URL`` (generic webhook POST)
- otherwise  → mock (records the send into the run trace)

Plain stdlib HTTP, no SDK dependency. The send is journaled by the runtime.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional

from ..errors import RyaError


def active_channel_provider(channel: str, env: Optional[dict] = None) -> str:
    env = env or os.environ
    if channel == "slack" and env.get("SLACK_WEBHOOK_URL"):
        return "slack"
    if channel == "email" and env.get("RESEND_API_KEY"):
        return "resend"
    if env.get(f"RYA_CHANNEL_{channel.upper()}_URL"):
        return "webhook"
    return "mock"


def _post(url: str, headers: dict, payload: dict, timeout: int = 30):
    from ..guard import check_egress
    check_egress(url, "POST")  # Action Guard egress check
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"content-type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode(errors="replace")
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                pass
            return resp.status, body
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RyaError("E_RUNTIME", f"channel '{url}' send HTTP {e.code}: {detail}",
                       hint="Check the channel credentials / webhook URL.")
    except urllib.error.URLError as e:
        raise RyaError("E_RUNTIME", f"channel send failed: {e.reason}",
                       hint="Check network egress to the channel provider.")


def _text_of(message: dict) -> str:
    if isinstance(message, dict):
        return message.get("text") or message.get("body") or json.dumps(message, default=str)
    return str(message)


def send(channel: str, message: dict, env: Optional[dict] = None) -> dict:
    """Deliver ``message`` on ``channel``. Returns a structured delivery receipt."""
    env = env or os.environ
    provider = active_channel_provider(channel, env)

    if provider == "slack":
        status, body = _post(env["SLACK_WEBHOOK_URL"], {}, {"text": _text_of(message)})
        return {"delivered": True, "channel": "slack", "provider": "slack", "status": status,
                "messageId": "slack_" + str(abs(hash(str(message))) % 1000000)}

    if provider == "resend":
        if not message.get("to"):
            raise RyaError("E_VALIDATION", "email send requires a 'to' field.",
                           hint="Call ctx.channels.send('email', {'to':…, 'subject':…, 'body':…}).")
        payload = {
            "from": env.get("RYA_EMAIL_FROM", "onboarding@resend.dev"),
            "to": message["to"],
            "subject": message.get("subject", "(no subject)"),
            "text": message.get("body") or message.get("text", ""),
        }
        _, body = _post("https://api.resend.com/emails",
                        {"Authorization": f"Bearer {env['RESEND_API_KEY']}"}, payload)
        rid = body.get("id") if isinstance(body, dict) else None
        return {"delivered": True, "channel": "email", "provider": "resend",
                "id": rid, "messageId": rid}

    if provider == "webhook":
        url = env[f"RYA_CHANNEL_{channel.upper()}_URL"]
        status, _ = _post(url, {}, {"channel": channel, "message": message})
        return {"delivered": True, "channel": channel, "provider": "webhook", "status": status,
                "messageId": "wh_" + str(abs(hash(str(message))) % 1000000)}

    # mock — record the intent into the trace (offline / default).
    return {"delivered": True, "channel": channel, "provider": "mock",
            "messageId": "msg_" + str(abs(hash(str(message))) % 1000000), "message": message}
