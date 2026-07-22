"""Bedrock provider - Converse API wire shapes, asserted against a fake client.

Everything here runs offline: we monkeypatch ``_bedrock_client`` and capture the
exact kwargs Rya sends, so the tests pin the Converse request format (system,
alternating roles, toolConfig, document blocks) and the response mapping."""

import pytest

import rya.providers.llm as llm
from rya.errors import RyaError


class FakeBedrock:
    """Records converse/converse_stream calls; returns canned Converse shapes."""

    def __init__(self, text="hello from bedrock", tool_use=None):
        self.calls = []
        self.text = text
        self.tool_use = tool_use

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        content = [{"text": self.text}]
        if self.tool_use:
            content.append({"toolUse": self.tool_use})
        return {"output": {"message": {"role": "assistant", "content": content}},
                "usage": {"inputTokens": 11, "outputTokens": 7}}

    def converse_stream(self, **kwargs):
        self.calls.append(kwargs)
        chunks = [{"contentBlockDelta": {"delta": {"text": w}}} for w in ["hel", "lo"]]
        chunks.append({"metadata": {"usage": {"inputTokens": 3, "outputTokens": 2}}})
        return {"stream": iter(chunks)}


@pytest.fixture
def fake(monkeypatch):
    client = FakeBedrock()
    monkeypatch.setattr(llm, "_bedrock_client", lambda: client)
    return client


def test_respond_maps_text_model_usage(fake):
    out = llm.respond(system="be brief", input={"q": "hi"},
                      model_default="us.anthropic.claude-haiku-4-5", provider="bedrock")
    assert out == {"text": "hello from bedrock", "model": "us.anthropic.claude-haiku-4-5",
                   "provider": "bedrock", "usage": {"input": 11, "output": 7}}
    req = fake.calls[0]
    assert req["system"] == [{"text": "be brief"}]
    assert req["messages"][0]["content"][-1]["text"] == '{"q": "hi"}'


def test_placeholder_model_resolves_from_env(fake, monkeypatch):
    monkeypatch.setenv("RYA_BEDROCK_MODEL", "us.anthropic.claude-sonnet-4-6")
    out = llm.respond(system="s", input={}, model_default="mock-llm", provider="bedrock")
    assert out["model"] == "us.anthropic.claude-sonnet-4-6"


def test_auto_resolves_to_bedrock_when_flagged(monkeypatch):
    monkeypatch.setenv("RYA_BEDROCK", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-ignored")
    assert llm.resolve_provider("auto") == "bedrock"


def test_streaming_delivers_tokens_and_usage(fake):
    seen = []
    out = llm.respond(system="s", input={}, model_default="m-x", provider="bedrock",
                      on_token=seen.append)
    assert seen == ["hel", "lo"] and out["text"] == "hello"
    assert out["usage"] == {"input": 3, "output": 2}


def test_documents_become_converse_document_blocks(fake, tmp_path):
    pdf = tmp_path / "aecb report.v2.pdf"
    pdf.write_bytes(b"%PDF-fake")
    llm.respond(system="extract", input={}, model_default="m", provider="bedrock",
                documents=[{"name": "aecb report.v2.pdf", "format": "pdf", "path": str(pdf)}])
    doc = fake.calls[0]["messages"][0]["content"][0]["document"]
    assert doc["format"] == "pdf" and doc["source"]["bytes"] == b"%PDF-fake"
    assert "." not in doc["name"]  # sanitized for Bedrock's name rules


def test_documents_rejected_on_non_bedrock_providers(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    with pytest.raises(RyaError) as e:
        llm.respond(system="s", input={}, provider="anthropic",
                    documents=[{"name": "a", "b64": "aGk="}])
    assert "documents" in str(e.value)


def test_chat_tool_loop_wire_format(monkeypatch):
    # the model sees (and returns) the SANITIZED name; Rya translates it back
    client = FakeBedrock(text="", tool_use={"toolUseId": "t1", "name": "crm_lookup",
                                            "input": {"email": "a@x.co"}})
    monkeypatch.setattr(llm, "_bedrock_client", lambda: client)
    out = llm.chat(
        messages=[
            {"role": "user", "content": {"q": "look up ada"}},
            {"role": "assistant", "content": "checking", "toolCalls": [
                {"id": "t0", "name": "crm.lookup", "input": {"email": "a@x.co"}}]},
            {"role": "tool", "toolUseId": "t0", "content": {"plan": "pro"}},
            {"role": "tool", "toolUseId": "t0b", "content": {"tier": 2}},
        ],
        tools=[{"name": "crm.lookup", "description": "CRM", "input_schema": {"type": "object"}}],
        system="sys", provider="bedrock")
    req = client.calls[0]
    # assistant turn reconstructs its toolUse blocks
    a = req["messages"][1]
    assert a["role"] == "assistant" and any("toolUse" in b for b in a["content"])
    # two tool results MERGED into one user message (Converse alternation rule)
    tr = req["messages"][2]
    assert tr["role"] == "user" and len([b for b in tr["content"] if "toolResult" in b]) == 2
    # outbound name sanitized for Bedrock's [a-zA-Z0-9_-]+ rule...
    assert req["toolConfig"]["tools"][0]["toolSpec"]["name"] == "crm_lookup"
    # ...and the returned call translated back to the real Rya tool id
    assert out["toolCalls"] == [{"id": "t1", "name": "crm.lookup", "input": {"email": "a@x.co"}}]


def test_boto3_missing_is_a_clear_error(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_boto3(name, *a, **k):
        if name == "boto3":
            raise ImportError("no module named boto3")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_boto3)
    with pytest.raises(RyaError) as e:
        llm._bedrock_client()
    assert "rya[bedrock]" in (e.value.hint or "")
