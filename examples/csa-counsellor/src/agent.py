"""ChatStudyAbroad counsellor agent, represented on Rya.

What runs here is the governance + conversation shell: durable sessions,
memory (facts extracted per turn, like production's Haiku sidecar), model
routes mirroring production model usage, and the runtime-enforced approval
gate on outbound email. The 28 declared tools mirror production's registry
for management (permissions, pins, kill switches) - wiring each to the live
CSA endpoint is the manifest's `url:` field, one tool at a time (Phase 1 of
the migration plan). The handler deliberately calls no unwired tool.
"""

from rya import define_agent

agent = define_agent()

COUNSELLOR_SYSTEM = (
    "You are ChatStudyAbroad's counsellor assistant for study-abroad advisors. "
    "Be concise and honest. Never invent numbers, fees, or eligibility rules - "
    "only cite facts a tool returned. If you don't know, say so and name which "
    "tool would answer it."
)


@agent.on_event
async def handle_event(ctx, event):
    channel = event.payload.get("channel", "web")
    counsellor = event.payload.get("externalId") or event.payload.get("email") or "counsellor"
    body = event.payload.get("body") or "(empty message)"

    session = await ctx.sessions.get_or_create(channel, counsellor, title=counsellor)
    await ctx.sessions.append(session["id"], "user", body)
    await ctx.memory.block_set("persona", COUNSELLOR_SYSTEM)

    reply = await ctx.llm.respond(system=COUNSELLOR_SYSTEM, input={"message": body},
                                  route="compose")
    await ctx.sessions.append(session["id"], "assistant", reply.text)

    # Sidecar fact extraction (production: Haiku post-turn memory extraction).
    facts = await ctx.llm.respond(
        system="Extract at most one durable fact about the student from this exchange. "
               "Reply with the fact as one short sentence, or 'none'.",
        input={"message": body}, route="extract")
    if facts.text.strip().lower() not in ("none", ""):
        await ctx.memory.append("student_facts", {"fact": facts.text.strip(),
                                                  "source": counsellor})

    # Outbound email is approval-gated BY THE RUNTIME (production: preview ->
    # confirm prompt convention). The run pauses durably until a human decides.
    if event.payload.get("sendEmail"):
        await ctx.approvals.request(
            title="Send email to student",
            body=reply.text,
            action={"tool": "send_email",
                    "input": {"to": event.payload.get("studentEmail", "student@example.com"),
                              "subject": "From your counsellor",
                              "body": reply.text}},
        )
        ctx.logs.info("email approved and dispatched")

    return {"session": session["id"], "reply": reply.text}
