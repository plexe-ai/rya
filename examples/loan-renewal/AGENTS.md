# Agent handoff - LA renewal agent on Rya

This file is for any coding agent (or engineer) picking this project up in a
new environment. It tells you what exists, how the two repos relate, how to
run and verify everything, what is deployed, what remains, and the mistakes
already made so you do not repeat them.

## What this is

An LA (loan application) renewal agent for a bank credit team. A user says
"Start an LA renewal application for CIF 884411"; the agent resolves the CIF
against the archive, collects the required documents (AECB report, spread,
IDs, reference report), extracts fields from each PDF via Bedrock Claude,
composes a renewal report where every figure carries a `[source: doc.field]`
citation, hard-stops if any figure is unsourced (grounding gate), and writes
back to the LA database only after a human approves. Long documents are
page-split into parallel extraction jobs and merged with per-page provenance;
conflicting values across pages surface as a "DATA CONFLICTS - REVIEW
REQUIRED" section instead of being silently averaged.

It is built on **Rya**, a durable agent runtime: every step is journaled and
replayable, jobs run on a Postgres-backed queue (SKIP LOCKED, leases,
backoff, dead-letter), fan-out/fan-in uses job groups with exactly-once
completion hooks, approvals are pause points that survive restarts, and
multi-tenancy is Postgres RLS per workspace.

## The two repos

| Repo | Contents | Role |
|---|---|---|
| `rya` | Runtime, CLI, providers (Bedrock), deploy tooling, **and this project at `examples/loan-renewal/`** | Canonical dev layout. All work happens here. |
| `la-renewal-agent` | Standalone copy of `examples/loan-renewal/` only | Mirror for importing/tracking the project on its own. |

Rya is **not on PyPI** (the name is taken by an unrelated package). The
project's `pip install .` pulls rya from GitHub; in production the Docker
image bakes runtime + project together, so nothing is installed at runtime.

**The project must live inside the rya checkout** at
`examples/loan-renewal/` for the image build (`rya deploy aws` builds with
`deploy/aws/Dockerfile.project`, which copies both). If you imported the two
repos separately, clone rya and either use the copy already inside it or
replace it with the mirror's content:

```bash
git clone <rya-remote> && git clone <la-renewal-agent-remote>
rsync -a --delete --exclude .git --exclude .rya --exclude __pycache__ \
  la-renewal-agent/ rya/examples/loan-renewal/
```

To push project changes back to the mirror, rsync the other direction and
commit there. Keep both in sync when you change project files.

## Local setup and verification

```bash
cd rya
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[api,postgres,llm,mcp,bedrock]" pypdf

# fast, no AWS needed:
python -m pytest -q                      # core suite (PG tests skip without env)
cd examples/loan-renewal
RYA_FORCE_MOCK=1 rya eval                # 8 behavioural evals, offline
rya dev                                  # validate manifest + inspect

# full suite incl. Postgres RLS / queue / tenancy tests:
docker run -d --name rya-pg -e POSTGRES_PASSWORD=ryamt -p 55441:5432 postgres:16
docker exec rya-pg psql -U postgres -c "CREATE DATABASE ryatest"
RYA_TEST_DATABASE_URL="postgresql://postgres:ryamt@localhost:55441/ryatest" \
  python -m pytest -q                    # expect: 275+ passed
```

Live-model runs need AWS credentials with Bedrock access (see Deploy). The
model is set in `rya.agent.yaml` model routes; the stack currently uses
`us.anthropic.claude-haiku-4-5-20251001-v1:0` inference profiles.

Local app: `rya serve` on :8931 plus a static server for `web/index.html` on
:8932 (the page auto-targets :8931 when served from :8932). The web app is a
single self-contained HTML file - sidebar, hash router, cases/approvals
views, full-screen login gate, presigned S3 uploads for files over 4MB.

## Deploy (one command)

```bash
cd examples/loan-renewal
rya deploy aws            # preflight -> image -> network -> stack -> smoke
rya deploy status
rya deploy destroy        # asks first; deletes the stack AND its data
```

What it provisions: ECS Fargate (arm64, 2 tasks) + ALB, Multi-AZ RDS
Postgres, S3 files bucket with Glacier lifecycle, Secrets Manager, CloudWatch
alarms, task-role IAM for Bedrock/S3. Flags: `--region --stack --count
--no-ha --skip-build`. State lands in `.rya/deploy.json`; the second run is
an update. Full manual runbook (what the command automates): `DEPLOY.md`.

**Do step 0 first in any new account**: enable Anthropic model access in the
Bedrock console and ensure the account has a valid Marketplace payment
method. The preflight makes a real `converse` call and fails fast with this
hint - nothing else works until that call succeeds.

A reference stack (`loan-renewal-live`, us-east-1) runs in the Plexe dev
account; a bank environment should deploy fresh with this command. First run
is ~15 minutes, RDS is the clock. First user to sign up in the app owns the
workspace.

## Hard-won gotchas (do not relearn these)

- **Stack updates preserve secrets and network.** `rya deploy aws` passes
  `UsePreviousValue` for `RyaSecretKey`/`RyaAdminToken` (rotating logs every
  user out) and for VPC/subnet params (changing the RDS subnet set while the
  instance is inside it rolls the whole update back - this happened).
- **CloudFormation failure reasons lie.** "Resource update cancelled" is the
  cascade, not the cause; the CLI's failure translation skips those and digs
  ECS stopped-task reasons. Trust its output over the first red row.
- **Malformed-but-readable PDFs exist.** `split_pdf` falls back to a single
  whole-document chunk when pypdf cannot parse; do not "fix" that fallback.
- **The grounding gate is a feature, not a bug.** If a report is blocked with
  "unsourced figures", the model computed a derived number. The compose
  prompt forbids computing differences/sums/percentages - keep it that way.
- **Chunk extraction schemas use fixed field names per docType**
  (`EXTRACTION_SCHEMAS` in `src/agent.py`). Free-form keys across chunks
  (`annual_revenue` vs `annual_revenue_aed`) silently hide conflicts.
- **Numeric conflict tolerance is 0.5%** (`merge_extractions`), so rounding
  artifacts (0.55 vs 0.551) do not page a human.
- **Bedrock tool names** must match `[a-zA-Z0-9_-]+`; the provider sanitizes,
  but keep manifest tool names clean anyway.
- **The ALB is HTTP.** Front it with CloudFront + ACM before real users; the
  deploy command prints this reminder.

## What remains (see ENTERPRISE.md for the full list)

Open items, priority order: HTTPS via CloudFront + ACM; session-to-JWT
identity bridge so approvals record who acted; private subnets + VPC
endpoints (Bedrock, S3, Secrets Manager); bank IdP SSO; 500-case load test
with honestly reported numbers; CloudWatch dashboard on the existing alarms;
Langfuse in the VPC for prod traces; DR runbook; and the real integration -
swapping the `data/bank_db.json` leaf tools for `url:` HTTP tools against the
bank's archive/LA systems (the pipeline code does not change, only the
manifest tool definitions).

## Working conventions

- No emojis and no em dashes anywhere (code, UI, docs); use " - ".
- Never report tests or evals as passing when they are not; never shop for
  favorable eval numbers.
- The UI is deliberately premium monochrome (Notion/Linear style), one HTML
  file, no framework - keep it that way unless asked.
- `rya doctor` lints handlers for replay discipline (raw IO in journaled
  code); run it after touching `src/agent.py`.

## Key files

```
rya/
  src/rya/deploy_aws.py            rya deploy aws orchestrator
  src/rya/deploy_assets/template.yaml   the CloudFormation stack
  src/rya/documents.py             split_pdf / merge_extractions
  src/rya/runtime/engine.py        journaled execution, jobs, groups, approvals
  deploy/aws/Dockerfile.project    bakes runtime + project into one image
examples/loan-renewal/  (= la-renewal-agent repo)
  rya.agent.yaml                   manifest: model routes, tools, policies
  src/agent.py                     pipeline: intent -> extract fan-out -> compose -> gated write
  web/index.html                   the whole app
  DEPLOY.md                        deploy runbook (one command + manual steps)
  ENTERPRISE.md                    hardening checklist, [x] = done and verified
  rya.evals.yaml                   behavioural evals (run offline with RYA_FORCE_MOCK=1)
```
