# Rya on AWS — single-tenant deployment

This is the Infrastructure-as-Code for the vision's reference posture: a
**single-tenant** Rya deployment in the customer's own AWS account, with no
shared blast radius. [`template.yaml`](template.yaml) is a SAM/CloudFormation
template that provisions the whole topology.

## What it provisions

| Concern | Resource |
|---|---|
| Identity at the edge | **Cognito** User Pool + App Client (RS256 JWTs, verified via JWKS) |
| Runtime (control + data plane) | **ECS Fargate** (ARM64) behind an **ALB**, `DesiredCount` tasks, scales horizontally |
| State | **RDS Postgres** with row-level security; per-user RLS keyed on `app.user_id` |
| Hot reads | **ElastiCache** Serverless (Redis) |
| Secrets | **Secrets Manager** (DB password generated; `RYA_JWT_SECRET` generated; model keys populated post-deploy) — pulled into the task via `ValueFrom`, never committed |
| Privileged writes | **single-purpose mutator Lambda** behind an HTTP API with a Cognito JWT authorizer — re-validates the caller's JWT before any DB write |
| Logs | CloudWatch Logs |

The runtime container runs with `RYA_MULTITENANT=1` and `RYA_JWKS_URL` pointing at
the Cognito pool, so the same image you build from the repo `Dockerfile` enforces
JWT identity + per-user RLS in production.

## Deploy (two commands)

> Requires AWS credentials with permission to create these resources. This step
> **provisions real, billable infrastructure** — run it deliberately.

```bash
# 1. Build + push the runtime image to ECR, then:
sam build
sam deploy --guided \
  --parameter-overrides \
    ContainerImage=<account>.dkr.ecr.<region>.amazonaws.com/rya:latest \
    VpcId=vpc-… PublicSubnetIds=subnet-…,subnet-… PrivateSubnetIds=subnet-…,subnet-…
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
- **Hardening TODO** before production: the task definition builds
  `RYA_DATABASE_URL` from a dynamic Secrets Manager reference (the resolved value
  lands in the task env). The vision's "no secrets in env at rest" calls for
  moving the full DSN into a Secrets Manager secret and pulling it via `Secrets:
  ValueFrom`. Also add HTTPS (ACM cert + 443 listener) and CloudFront.
- VPC/subnets are **parameters** (you bring an existing VPC) rather than created
  here, to keep the stack focused on Rya.
- The per-user RLS this relies on is built and **verified on real Postgres**
  (`tests/test_tenancy.py::test_per_user_rls_enforced_by_database`).
