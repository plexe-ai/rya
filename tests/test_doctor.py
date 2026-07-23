"""rya doctor - replay-discipline linting."""
from rya.doctor import lint_replay


def test_flags_raw_io_in_handlers_but_not_tools(tmp_path):
    src = tmp_path / "agent.py"
    src.write_text('''
import requests
from rya import define_agent
agent = define_agent()

@agent.tool("ok.tool")
async def ok_tool(input):
    return requests.get("http://x").json()   # tools are leaves - allowed

@agent.on_event
async def main(ctx, event):
    requests.post("http://x", json={})        # NOT allowed - re-executes on replay
    data = open("/tmp/f").read()              # NOT allowed
    return helper()

def helper():
    import subprocess
    return subprocess.run(["ls"])             # reached via handler - flagged
''')
    findings = lint_replay(src)
    calls = {f["call"] for f in findings}
    assert any("requests.post" in c for c in calls)
    assert "open" in calls
    assert any("subprocess.run" in c for c in calls)
    assert not any("requests.get" in c for c in calls)  # the tool is exempt


def test_clean_agent_passes(tmp_path):
    src = tmp_path / "agent.py"
    src.write_text('''
from rya import define_agent
agent = define_agent()

@agent.on_event
async def main(ctx, event):
    out = await ctx.tools.call("x", {})
    await ctx.jobs.schedule("j", {})
    return out
''')
    assert lint_replay(src) == []
