"""Support follow-up agent — the first vertical slice end to end.

Flow:
  1. Receive a webhook-style event
  2. Store the event in memory
  3. Call a mock customer lookup tool
  4. Call a mock churn-risk model
  5. Schedule a delayed follow-up job
  6. Draft a message with the LLM
  7. Request human approval before sending  (run PAUSES here)
  8. Resume after approval
  9. Send via mock channel
  10. Emit a complete run trace (automatic)
"""

from rya import define_agent

agent = define_agent()


@agent.on_event
async def handle_event(ctx, event):
    email = event.payload.get("email", "unknown@example.com")
    ctx.logs.info("handling event", type=event.type, email=email)

    # 2. Persist the inbound event.
    await ctx.memory.append("conversations", {"event": event.model_dump()})

    # 3. Look up the customer (allowed tool).
    customer = await ctx.tools.call("crm.lookup", {"email": email})

    # 4. Score churn risk via a custom model.
    risk = await ctx.models.call("churn-risk-v1", {"customer_id": customer["id"]})

    # 5. Schedule a delayed follow-up job for tomorrow.
    await ctx.jobs.schedule(
        "daily_followup", {"customer_id": customer["id"]}, delay_seconds=86400
    )

    if risk["score"] > 0.8:
        # 6. Draft a retention message.
        message = await ctx.llm.respond(
            system="Draft a concise retention follow-up.",
            input={"customer": customer, "risk": risk},
        )

        # 7. Gate the external send behind a human approval (pauses the run).
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

        # 8/9. Resumes here after approval; email already sent as the action.
        await ctx.channels.send(
            "email",
            {"to": customer["email"], "messageId": result["actionResult"]["messageId"]},
        )
        ctx.logs.info("follow-up sent", customer=customer["id"])

    return {"customer": customer["id"], "risk": risk["score"]}


@agent.job("daily_followup")
async def daily_followup(ctx, job):
    """Background job scheduled by the main run; run it with `rya jobs run`."""
    cid = job.payload.get("customer_id")
    ctx.logs.info("daily follow-up job", customer=cid)
    await ctx.channels.send("email", {"customer": cid, "kind": "daily"})
    return {"ok": True, "customer": cid}
