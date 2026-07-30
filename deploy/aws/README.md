# Rya on AWS — a deployment you own

Infrastructure-as-Code for a Rya deployment in the customer's own AWS account,
with no shared blast radius. [`template.yaml`](template.yaml) is a
SAM/CloudFormation template that provisions the whole topology.

**Single-tenant stack, multi-tenant runtime** — worth separating, because the two
words point at different boundaries. *One customer* owns this stack, which is why
self-hosting is a residency control: the journal, memory, conversation history and
sealed credentials stay in this account and region (PLATFORM_DESIGN §8). *Inside*
it, `RYA_MULTITENANT=1` turns on workspaces and Postgres RLS, so one stack can
carry many projects with data isolation between them (D13).

D13 is also explicit about the limit: separate processes plus RLS contain a
**buggy** tenant — a runaway loop, a leak, a crash. They do not contain a
**hostile** one, because the worker tasks share a kernel. Node-level isolation is
an accepted residual, not a solved problem.

## What it provisions

| Concern | Resource |
|---|---|
| Identity at the edge | **Cognito** User Pool + App Client (RS256 JWTs, verified via JWKS) |
| Control plane (`api`) | **ECS Fargate** (ARM64) behind an **ALB**, `DesiredCount` tasks. REST/WS/SSE, auth, policy, guard, vault, console, MCP |
| Execution plane (`worker`) | **ECS Fargate**, `WorkerCount` tasks, no ALB target and no inbound port. Runs handler code from the image's **baked** project — the command is a bare `rya worker`, i.e. unpinned, so it does not resolve a deployed version (see below) |
| State | **RDS Postgres** with row-level security; per-user RLS keyed on `app.user_id` |
| Hot reads | **ElastiCache** Serverless (Redis) |
| Secrets | **Secrets Manager** (DB password generated; `RYA_JWT_SECRET` generated; model keys populated post-deploy) — pulled into the task via `ValueFrom`, never committed |
| Logs | CloudWatch Logs |

**Two services, one image.** `api` and `worker` are run modes of the same
container, against the same Postgres, coordinating through the queue table — not
microservices, and there is no service-to-service call to configure
(PLATFORM_DESIGN §2, D4). The split is load-bearing: the api process executes **no
handler code**, which is what makes per-tenant isolation mean something (§11.7).
It also means the worker is not optional — with `WorkerCount=0` the stack accepts
events, enqueues them, and never runs them.

Scale them independently: `DesiredCount` follows request load, `WorkerCount`
follows queue depth. Per-key concurrency caps stop one workspace starving another
(§6), and per-workspace quotas (`rya quotas set`) bound runs, tokens and cost.

The containers run with `RYA_MULTITENANT=1` and `RYA_JWKS_URL` pointing at the
Cognito pool, so the same image you build from the repo `Dockerfile` enforces JWT
identity + per-user RLS in production.

### Not provisioned

| Concern | Status |
|---|---|
| Privileged writes via a **single-purpose mutator Lambda** | **Pattern only — returns 501.** The function is deployed but deliberately unimplemented: verifying an RS256 JWT against Cognito's JWKS needs a crypto library that CloudFormation `InlineCode` cannot carry. It fails closed rather than returning `{"ok": true}` to everything, which is what it used to do. The template comment above `MutatorFunction` specifies what a real implementation must do. **Do not treat this stack as having that control.** |
| A **bundle archive store** for `rya publish` | **Not provisioned — and publishing to this stack misfires silently.** The template creates only `FilesBucket` and sets only `RYA_FILES_S3_BUCKET`, so `bundles.resolve_bundle_store()` finds no `RYA_BUNDLES_S3_BUCKET` and falls back to the **local** arm: the archive lands in `/project/.rya/bundles/…` inside the *api* task's own filesystem. The worker tasks are different containers and cannot read it, and the bytes vanish when the task is replaced. So `POST /agents/{id}/versions` returns `ok: true`, a version is recorded, and a pinned worker later fails `E_BUNDLE_NOT_FOUND`. Three things are missing together and each is silent on its own: (1) an S3 bucket plus `RYA_BUNDLES_S3_BUCKET` on **both** task definitions; (2) that bucket in `TaskRole`, which today is scoped to `${FilesBucket.Arn}/*`; (3) `--env <name>` on the worker command, since a bare `rya worker` runs the baked tree and never resolves a version at all. Until all three land, publish to a compose deployment — `docker-compose.yml` ships MinIO for exactly this — and rebuild the image for AWS. |

## Deploy (two commands)

> Requires AWS credentials with permission to create these resources. This step
> **provisions real, billable infrastructure** — run it deliberately.

```bash
# 1. Build + push the runtime image to ECR, then:
sam build
sam deploy --guided \
  --parameter-overrides \
    ContainerImage=<account>.dkr.ecr.<region>.amazonaws.com/rya:latest \
    VpcId=vpc-… PublicSubnetIds=subnet-…,subnet-… PrivateSubnetIds=subnet-…,subnet-… \
    RyaEnvironment=prod DesiredCount=2 WorkerCount=2
```

After deploy, populate the model API keys in the `<stack>/app` secret, then the
outputs give you the ALB URL, the HTTP API endpoint, and the Cognito pool id.

## Validation (no AWS account needed)

The template is linted clean in CI:

```bash
cfn-lint deploy/aws/template.yaml      # passes with no findings
```

## Status / honest notes

- **Authored + lint-validated locally; NOT deployed here** — actual `sam deploy`
  needs your AWS account and creates billable resources, so it's a deliberate
  operator step, not something this repo does for you.
- **Leave the endpoint overrides unset here.** `RYA_BUNDLES_S3_ENDPOINT` and
  `RYA_FILES_S3_ENDPOINT` exist for S3-compatible stores that are not S3 (MinIO,
  Ceph, R2), and declaring either one *also* forces `addressing_style: "path"` —
  because a MinIO host would otherwise be templated as
  `http://<bucket>.minio:9000` and fail to resolve. On AWS you want boto3's own
  endpoint resolution and virtual-host addressing, so both must stay absent or
  empty in the task definitions. The template sets neither; do not add them.
- **The publish path files no readiness attestation.** The control plane does not
  import bundles (D13), so a version created over HTTP carries `attested: false`.
  If you set a promotion gate requiring readiness, publish records the version and
  refuses the promotion — correct behaviour, not a bug. `rya deploy --env` from a
  box holding the DB and bucket credentials is the path that attests.
- **Hardening TODO** before production: the task definition builds
  `RYA_DATABASE_URL` from a dynamic Secrets Manager reference (the resolved value
  lands in the task env). The vision's "no secrets in env at rest" calls for
  moving the full DSN into a Secrets Manager secret and pulling it via `Secrets:
  ValueFrom`. Also add HTTPS (ACM cert + 443 listener) and CloudFront.
- VPC/subnets are **parameters** (you bring an existing VPC) rather than created
  here, to keep the stack focused on Rya.
- The per-user RLS this relies on is built and **verified on real Postgres**
  (`tests/test_tenancy.py::test_per_user_rls_enforced_by_database`).
