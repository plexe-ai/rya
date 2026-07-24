# Langfuse - traces and eval scores

Rya owns the durable journal (replay depends on it) but does not try to be an
observability product. Point it at a Langfuse - self-hosted or cloud - and every
run and every eval score lands there automatically. Rya also *reads* from
Langfuse: `rya eval --langfuse-dataset` pulls a dataset's items and runs the
agent over each (see [Run a Langfuse dataset](#run-a-langfuse-dataset)).

## What gets exported

- **Every finished run** becomes a Langfuse **trace**: LLM steps are
  **generations** (model, input/output, token usage), tool calls are **spans**
  (input, output, permission), everything else (approvals, guard checks,
  memory writes) are **events**. Exported by the engine at terminal status;
  ingested external runs (`POST /runs/ingest`) flow through too.
- **Every eval case** attaches **scores** to its run's trace:
  - `eval:<case-id>` - the case verdict (BOOLEAN)
  - `<case-id>:<check>` - each individual check (BOOLEAN), e.g.
    `high_risk_pauses_for_approval:approval_requested`
  - metric checks export their real value (NUMERIC), e.g. a DeepEval
    faithfulness of `0.83`
  - a case that ends paused (`waiting_approval`) is still exported so its
    scores have a trace to attach to.

Everything is best-effort: an export failure never breaks a run or an eval.

## Configure

Three env vars (in the project `.env` or the process environment):

```bash
LANGFUSE_HOST=http://localhost:3300
LANGFUSE_PUBLIC_KEY=pk-lf-rya-local
LANGFUSE_SECRET_KEY=sk-lf-rya-local
```

That's it. `rya dev`, `rya serve`, and `rya eval` all pick them up.

## Self-host it

```bash
cd deploy/langfuse && docker compose up -d
# UI:    http://localhost:3300   (rya@local.dev / ryalangfuse)
# keys:  pk-lf-rya-local / sk-lf-rya-local (provisioned headlessly on first boot)
```

The compose file runs the full Langfuse v3 stack (web, worker, Postgres,
ClickHouse, Redis, MinIO) with local-dev credentials baked in - override every
secret via env before exposing it anywhere. The web port binds to 127.0.0.1
only.

## Run a Langfuse dataset

Have an eval dataset in Langfuse (Datasets — items with an `input` and optional
`expectedOutput`)? Run the agent over every item and record the results as a
Langfuse **dataset run**:

```bash
rya eval --langfuse-dataset <dataset-name>
```

Each item fires a real engine run (exactly like a local eval case), the run's
trace is exported, and the trace is linked to its dataset item as a
`dataset-run-item` — so the whole run shows up under **Datasets → your dataset →
Runs** in the Langfuse UI, one row per item, each opening the trace that produced
it. Uses the same three `LANGFUSE_*` env vars; it errors if they're unset.

**How an item's `input` maps to an event** (`type`, `payload`):

| Item `input` | Fired as |
|---|---|
| dict with `type` / `payload` keys | those, verbatim |
| any other dict | the `payload` (type = `--trigger-type`, default `message.received`) |
| a string | `{"body": <string>}` |

`--payload-defaults '<json>'` is merged *under* every item's payload, so a
dataset that only carries `body` still satisfies a handler that needs a fixed
field — the item always wins on conflict:

```bash
rya eval --langfuse-dataset csa-golden \
  --payload-defaults '{"email":"counsellor@csa.test"}' \
  --run-name nightly-2026-07-23 --json
```

**Scoring.** An item is "run and linked" by default (counts as a pass; its trace
+ any metric scores still land in Langfuse). To assert behaviour per item, add a
Rya `expect` block under the item's **metadata** (`metadata.expect`) — the same
scorers as local evals (`status`, `tools_called`, `no_failure`, `deepeval`, …)
run and attach as scores to the trace. Any failing item exits non-zero (5), so
you can gate a deploy on a Langfuse dataset just like the local suite.

## Deep evals

Behavioural checks (status, tools called, approvals, cost caps) are native to
`rya eval`. LLM-output-quality metrics are delegated to
[DeepEval](https://github.com/confident-ai/deepeval):

```bash
pip install 'rya[deepeval]'
```

```yaml
# rya.evals.yaml
evals:
  - id: refund_reply_grounded
    trigger: { type: message.received, payload: { email: ada@acme.io } }
    expect:
      status: waiting_approval
      deepeval: { metric: faithfulness, threshold: 0.7 }
```

Metrics: `faithfulness`, `answer_relevancy`, `hallucination`, `bias`,
`toxicity`, `contextual_relevancy` / `_precision` / `_recall`. The metric's own
judge uses your Anthropic or OpenAI key (`RYA_DEEPEVAL_MODEL` to pin one).
Retrieval context defaults to the run's tool results, so faithfulness scores
"did the reply stick to what the tools returned" with zero extra config. The
numeric score is exported to Langfuse per case, so you can chart eval quality
over time next to the traces that produced it.

Offline behaviour is honest but non-blocking: with no provider key the judge
and DeepEval checks are SKIPPED (reported as such), never silently failed.

## Also speaks OTLP

`RYA_OTLP_ENDPOINT` exports the same runs as OpenTelemetry GenAI spans (Arize
Phoenix, Grafana Tempo, Datadog...), and `RYA_TRACE_WEBHOOK` POSTs run
summaries anywhere. All three can be enabled at once.
