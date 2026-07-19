# `rya.providers` - real external seams

Provider-pluggable seams behind `ctx.llm`, `ctx.channels`, and embeddings.
Deterministic mock by default (offline dev/CI), real when a key is set - same
agent code either way. All outbound HTTP passes through the Action Guard first.

## Files

- `llm.py` - the LLM seam. `resolve_provider()` picks anthropic/openai/mock from
  env. `respond(...)` (single call; `schema=` for structured output; `on_token=`
  for streaming) and `chat(...)` (tool-calling turn for `ctx.llm.run`). Real
  calls use plain `urllib` (no SDK dependency). Pricing/usage flow from the
  returned `usage` block.
- `channels.py` - outbound message delivery (Slack/email/webhook when configured,
  else mock).
- `embeddings.py` - vector embeddings for memory/knowledge search (real or hash-mock).

## Gotchas (bugs live here if tool loops break)

- **Tool-use message conversion**: for `chat`, an assistant message carrying
  `toolCalls` MUST be reconstructed into provider-native `tool_use` (Anthropic) /
  `tool_calls` (OpenAI) blocks so the following `tool_result`/`tool` message has
  a matching call. Dropping this 400s on the second step of any multi-tool loop.
  The mock provider does not enforce this - integration-test real providers.
- Placeholder model names (`mock-llm`, ...) resolve to a real default model when
  a provider is selected, via `RYA_LLM_MODEL` / `RYA_OPENAI_MODEL`.
- Streaming (`on_token`) uses provider SSE; the returned dict shape is identical
  to non-streaming so callers are agnostic.
