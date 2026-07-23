#!/usr/bin/env python
"""Tiny interactive REPL for the csa-counsellor agent.

Not a Rya built-in — the `rya events send` CLI is single-shot. This loops
stdin -> engine.run_event on ONE session (keyed by --email, so learned memory
and the pinned student_state persist across turns) -> prints the reply, and
walks you through any approval-gated write inline.

    python chat.py                    # uses the real model if ANTHROPIC_API_KEY is set
    RYA_FORCE_MOCK=1 python chat.py   # deterministic offline (mock LLM, calls the first tool)
    python chat.py --cams 1472802     # load a student into the session up front

Slash commands (gated writes are payload-driven in this port, mirroring the
counsellor UI's confirm affordance — plain prose won't trigger them):
    /cams <id>          pin/switch the active student for later turns
    /loan <amount>      request a loan write (suspends for approval)
    /email <address>    request an outbound email (suspends for approval)
    /visa <country>     shortcut for a visa-requirements question
    /quit               exit
"""
import argparse
import os
import sys
from pathlib import Path

from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.store import Store

ROOT = Path(__file__).resolve().parent


def _engine():
    manifest = load_manifest(ROOT / "rya.agent.yaml")
    agent = load_agent(manifest, ROOT)
    store = Store(ROOT); store.ensure()
    return Engine(manifest, agent, store, ROOT), store


def _payload(text, email, cams):
    """Turn a line of input into an event payload. Slash commands set the gated
    write flags the event handler dispatches on."""
    p = {"email": email, "body": text}
    if cams:
        p["camsId"] = cams
    if text.startswith("/loan "):
        p["body"] = "start a loan application for this student"
        p["loanApply"] = True
        p["loanAmount"] = int(text.split(None, 1)[1] or 0)
    elif text.startswith("/email "):
        p["body"] = "email the student"
        p["sendEmail"] = True
        p["studentEmail"] = text.split(None, 1)[1].strip()
    elif text.startswith("/visa "):
        p["body"] = f"what are the student-visa requirements for {text.split(None, 1)[1].strip()}?"
    return p


def _show(run, engine, store):
    status = run["status"]
    out = run.get("output") or {}
    if status == "completed":
        print(f"\nagent> {out.get('reply') or '(no text)'}")
        tools = [t for t in (out.get("toolCalls") or []) if t]
        if tools:
            print(f"       [tools: {', '.join(tools)}]")
    elif status == "waiting_approval":
        apr_id = run["pendingApproval"]
        appr = store.get_approval(apr_id)
        action = appr.get("action") or {}
        inp = action.get("input") or {}
        # Show the WRITE awaiting approval — NOT the model's prose. Gated tools are
        # never exposed to the model, so its reply is unrelated to (and can even
        # contradict) the action the counsellor UI queued; the write is dispatched
        # from the turn payload and fires only on your approval.
        fields = ", ".join(f"{k}={v}" for k, v in inp.items()
                           if k != "body" and v not in (None, "", {}, []))
        print(f"\n[approval needed] {appr['title']}")
        print(f"  action: {action.get('tool')}({fields})")
        ans = input("  approve this write? [y/N] ").strip().lower()
        if ans in ("y", "yes"):
            resumed = engine.approve(apr_id)
            result = (store.get_approval(apr_id) or {}).get("actionResult")
            print(f"  [approved → {resumed['status']}] result: {result}")
        else:
            engine.reject(apr_id)
            print("  [rejected — run terminated]")
    elif status == "needs_reconnect":
        print(f"\nagent> [reconnect required] {(run.get('error') or {}).get('message')}")
    else:
        print(f"\nagent> [{status}] {run.get('error')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="you@csa.test", help="Session key (same email = same thread).")
    ap.add_argument("--cams", default=None, help="Pin a student CAMS id for the session.")
    args = ap.parse_args()

    engine, store = _engine()
    mode = "mock LLM" if os.environ.get("RYA_FORCE_MOCK") else "live model"
    print(f"csa-counsellor chat ({mode}). Type a message, /help for commands, /quit to exit.")
    cams = args.cams
    while True:
        try:
            text = input("\nyou > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text in ("/quit", "/exit", ":q"):
            break
        if text == "/help":
            print(__doc__)
            continue
        if text.startswith("/cams "):
            cams = text.split(None, 1)[1].strip()
            print(f"[active student → CAMS {cams}]")
            continue
        run = engine.run_event("message.received", _payload(text, args.email, cams))
        _show(run, engine, store)


if __name__ == "__main__":
    sys.exit(main())
