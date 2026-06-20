from rya.errors import RyaError


def test_vertical_slice_pause_approve_resume(engine):
    # Trigger -> pauses for approval.
    run = engine.run_event("message.received", {"email": "ada@example.com"})
    assert run["status"] == "waiting_approval"
    approval_id = run["pendingApproval"]
    assert approval_id

    # Tool + model + memory + job already happened before the pause.
    kinds = [t["kind"] for t in run["trace"]]
    assert "tool.call" in kinds
    assert "model.call" in kinds
    assert "memory.append" in kinds
    assert "job.schedule" in kinds
    assert "approval.requested" in kinds
    # The send has NOT happened yet.
    assert "channel.send" not in kinds

    # Approve -> run resumes and completes; email sent as the action.
    resumed = engine.approve(approval_id)
    assert resumed["status"] == "completed"
    resumed_kinds = [t["kind"] for t in resumed["trace"]]
    assert "approval.approved" in resumed_kinds
    assert "channel.send" in resumed_kinds


def test_replay_is_idempotent(engine):
    """After resume, prior steps must not re-execute (one tool call, not two)."""
    run = engine.run_event("message.received", {"email": "rey@example.com"})
    resumed = engine.approve(run["pendingApproval"])
    tool_calls = [t for t in resumed["trace"] if t["kind"] == "tool.call"]
    assert len(tool_calls) == 1
    model_calls = [t for t in resumed["trace"] if t["kind"] == "model.call"]
    assert len(model_calls) == 1


def test_reject_terminates_run(engine):
    run = engine.run_event("message.received", {"email": "kylo@example.com"})
    rejected = engine.reject(run["pendingApproval"])
    assert rejected["status"] == "rejected"
    assert rejected["error"]["code"] == "E_APPROVAL_REJECTED"


def test_approve_twice_is_rejected(engine):
    run = engine.run_event("message.received", {"email": "finn@example.com"})
    aid = run["pendingApproval"]
    engine.approve(aid)
    try:
        engine.approve(aid)
        assert False, "expected RyaError"
    except RyaError as e:
        assert e.code == "E_APPROVAL_NOT_PENDING"


def test_scheduled_job_runs(engine):
    run = engine.run_event("message.received", {"email": "poe@example.com"})
    engine.approve(run["pendingApproval"])
    jobs = engine.store.list_jobs("pending")
    assert len(jobs) == 1
    job_run = engine.run_job(jobs[0]["id"])
    assert job_run["status"] == "completed"
    assert [t for t in job_run["trace"] if t["kind"] == "channel.send"]


def test_runs_inside_a_running_event_loop(engine):
    """The MCP server and API call the engine from inside a loop; nested
    asyncio.run would fail. This guards that path."""
    import asyncio

    async def driver():
        return engine.run_event("message.received", {"email": "rose@example.com"})

    run = asyncio.run(driver())
    assert run["status"] == "waiting_approval"
    assert run["pendingApproval"]


def test_job_retries_with_backoff(tmp_path):
    """A failing job retries up to maxAttempts, then fails (openclaw pattern)."""
    from rya.manifest import load_manifest
    from rya.runtime import Engine, load_agent
    from rya.store import Store

    (tmp_path / "rya.agent.yaml").write_text("name: t\nruntime: python\nentrypoint: agent.py\n")
    (tmp_path / "agent.py").write_text(
        "from rya import define_agent\n"
        "agent = define_agent()\n"
        "@agent.on_event\n"
        "async def h(ctx, e):\n"
        "    await ctx.jobs.schedule('boom', {}, max_attempts=3)\n"
        "@agent.job('boom')\n"
        "async def boom(ctx, job):\n"
        "    raise RuntimeError('kaboom')\n"
    )
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    engine = Engine(manifest, agent, Store(tmp_path), tmp_path)

    engine.run_event("x", {})
    job_id = engine.store.list_jobs("pending")[0]["id"]

    engine.run_job(job_id)
    j = engine.store.get_job(job_id)
    assert j["attempts"] == 1 and j["status"] == "pending"  # rescheduled
    assert "kaboom" in (j["lastError"] or "")

    engine.run_job(job_id)
    assert engine.store.get_job(job_id)["attempts"] == 2

    engine.run_job(job_id)
    j = engine.store.get_job(job_id)
    assert j["attempts"] == 3 and j["status"] == "failed"  # exhausted


def test_unknown_approval(engine):
    try:
        engine.approve("apr_doesnotexist")
        assert False
    except RyaError as e:
        assert e.code == "E_APPROVAL_NOT_FOUND"
