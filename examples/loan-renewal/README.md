# loan-renewal - the BBG credit-renewal pipeline on Rya

"Start an LA renewal application for CIF 884411" to a cited, human-approved
report written back to the LA database. Eight steps, all durable:

intent (Bedrock haiku route) -> archive CIF resolution -> document checklist
(AECB, spread, IDs, reference report; uploads resume the case via
`file.uploaded`) -> one retryable extraction job per PDF (Converse document
blocks) -> reference-report schema derivation -> deterministic field filter ->
composed report with `[source: doc.field]` citations, hard-stopped by the
grounding gate if any figure is unsourced -> approval-gated `la.update_record`.

## Run it

```bash
export AWS_REGION=us-east-1            # any Bedrock-enabled identity
rya dev                                # validate + inspect
RYA_FORCE_MOCK=1 rya eval              # offline behavioural evals (8 cases)
rya eval                               # same evals live against Bedrock
```

Bank systems are leaf tools over `data/bank_db.json` (writes go to a runtime
copy). Swap any tool for the real archive/LA system by giving it a `url:` in
the manifest - the pipeline code does not change.

Models are Bedrock inference profiles in `rya.agent.yaml` model routes; pick
per-purpose models (cheap intent, strong compose) by editing two lines.
