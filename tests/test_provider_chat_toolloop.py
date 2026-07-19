"""Multi-tool ctx.llm.run turns must produce provider-valid message sequences.

Regression for a real bug found by the CSA-clone experiment: the assistant's
tool_use / tool_calls blocks were dropped when converting to Anthropic/OpenAI
format, so the second turn 400s ("tool_result has no matching tool_use"). The
mock provider doesn't enforce the protocol, so unit tests missed it - this
test asserts the *converted payload shape* the real providers require."""

from rya.providers.llm import _anthropic_chat, _openai_chat


MESSAGES = [
    {"role": "user", "content": {"q": "find UK data science"}},
    {"role": "assistant", "content": "", "toolCalls": [
        {"id": "toolu_1", "name": "course_catalogue", "input": {"query": "UK data science"}}]},
    {"role": "tool", "name": "course_catalogue", "toolUseId": "toolu_1",
     "content": {"results": []}},
]


def _capture(monkeypatch, module_fn):
    """Run the converter with a stubbed HTTP layer, capturing the payload."""
    captured = {}

    def fake_http(url, headers, payload, timeout=60):
        captured["payload"] = payload
        return {"content": [{"type": "text", "text": "ok"}], "usage": {},
                "choices": [{"message": {"content": "ok"}}]}

    import rya.providers.llm as llm
    monkeypatch.setattr(llm, "_http_json", fake_http)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    module_fn("claude-sonnet-4-6", "sys", MESSAGES, None, None, None)
    return captured["payload"]


def test_anthropic_assistant_tooluse_matches_toolresult(monkeypatch):
    payload = _capture(monkeypatch, _anthropic_chat)
    msgs = payload["messages"]
    # the assistant message carries a tool_use block with the same id the
    # following tool_result references
    assistant = next(m for m in msgs if m["role"] == "assistant")
    use_ids = {b["id"] for b in assistant["content"] if b.get("type") == "tool_use"}
    assert "toolu_1" in use_ids
    result = next(m for m in msgs if m["role"] == "user"
                  and isinstance(m["content"], list)
                  and m["content"][0].get("type") == "tool_result")
    assert result["content"][0]["tool_use_id"] in use_ids


def test_openai_assistant_toolcalls_match_tool_message(monkeypatch):
    payload = _capture(monkeypatch, _openai_chat)
    msgs = payload["messages"]
    assistant = next(m for m in msgs if m["role"] == "assistant")
    call_ids = {c["id"] for c in assistant["tool_calls"]}
    assert "toolu_1" in call_ids
    tool_msg = next(m for m in msgs if m["role"] == "tool")
    assert tool_msg["tool_call_id"] in call_ids
