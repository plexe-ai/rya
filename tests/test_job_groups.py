"""Job groups - fan-out with race-free, exactly-once fan-in."""

import os
import threading

import pytest

from rya.cli import scaffold
from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.store import Store

AGENT = '''
from rya import define_agent
agent = define_agent()

@agent.on_event
async def main(ctx, event):
    return await ctx.jobs.schedule_group(
        [("work", {"n": i}) for i in range(3)],
        on_complete=("done", {"from": "group"}))

@agent.job("work")
async def work(ctx, job):
    if job.payload.get("n") == 99:
        raise RuntimeError("boom")
    return {"n": job.payload["n"]}

@agent.job("done")
async def done(ctx, job):
    return {"fired": True}
'''


def _engine(tmp_path, agent_src=AGENT):
    scaffold.write_project(tmp_path, "grp", template="demo")
    (tmp_path / "src" / "agent.py").write_text(agent_src)
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    store = Store(tmp_path); store.ensure()
    return Engine(manifest, agent, store, tmp_path)


def test_group_fires_on_complete_exactly_once(tmp_path):
    e = _engine(tmp_path)
    e.run_event("message.received", {})
    e.work_once()   # runs the 3 members; last one enqueues on_complete
    e.work_once()   # runs the on_complete job
    jobs = e.store.list_jobs()
    done_jobs = [j for j in jobs if j["handler"] == "done"]
    assert len(done_jobs) == 1 and done_jobs[0]["status"] == "done"


def test_failed_member_blocks_on_complete(tmp_path):
    e = _engine(tmp_path, AGENT.replace('range(3)', '[0, 99]').replace(
        '("work", {"n": i}) for i in ', '("work", {"n": i}) for i in '))
    e.run_event("message.received", {})
    for _ in range(6):  # let retries exhaust (maxAttempts 3, backoff in future)
        for j in e.store.list_jobs():
            if j["status"] == "pending":
                j["runAt"] = "2000-01-01T00:00:00Z"; e.store.save_job(j)
        e.work_once()
    jobs = e.store.list_jobs()
    assert any(j["handler"] == "work" and j["status"] == "failed" for j in jobs)
    assert not any(j["handler"] == "done" for j in jobs)


@pytest.mark.skipif(not os.environ.get("RYA_TEST_DATABASE_URL"),
                    reason="set RYA_TEST_DATABASE_URL for the parallel race test")
def test_parallel_group_completion_fires_once_postgres():
    from rya.store_postgres import PostgresStore
    s = PostgresStore(os.environ["RYA_TEST_DATABASE_URL"]); s.ensure()
    group = s.create_job_group({"handler": "done", "payload": {}}, 16)
    fires = []
    def worker():
        out = s2 = None
        st = PostgresStore(os.environ["RYA_TEST_DATABASE_URL"])
        out = st.group_job_done(group["id"], success=True)
        if out and out.get("fire"):
            fires.append(1)
        st.close()
    threads = [threading.Thread(target=worker) for _ in range(16)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert len(fires) == 1, f"on_complete fired {len(fires)} times"
