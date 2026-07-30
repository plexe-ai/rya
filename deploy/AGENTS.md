# `deploy` - production deployment

Infrastructure-as-code for running Rya. See `deploy/aws/README.md` for the full
AWS topology and deploy steps.

## Contents

- `aws/template.yaml` - SAM/CloudFormation for the reference **single-tenant**
  posture: ECS Fargate (ARM64) behind an ALB, RDS Postgres with RLS, Cognito
  identity, ElastiCache, Secrets Manager, a mutator Lambda that is a **501 stub**
  (it revalidates nothing — see `aws/README.md`), CloudWatch logs. Runs with
  `RYA_MULTITENANT=1` + `RYA_JWKS_URL`.
- `aws/Dockerfile.baked` - runtime image that scaffolds the default agent.
- `aws/Dockerfile.project` - runtime image that bakes a SPECIFIC project dir
  (`--build-arg PROJECT=examples/...`), for deploying a real agent.

**`rya publish` does not work against the AWS stack.** The template provisions no
bundle bucket, `TaskRole` is scoped to the files bucket only, and the worker runs a
bare `rya worker` (unpinned), so a publish records a version whose archive the
worker cannot read. The agent reaches AWS by being **baked into the image**; the
upload path is a compose/self-host capability today. `aws/README.md` has the
three-part fix under "Not provisioned".

## Deploy recipe (image update)

1. `docker buildx build --platform linux/arm64 -f deploy/aws/Dockerfile.project
   --build-arg PROJECT=<dir> -t <ecr>/rya-control-plane:<tag> --push .`
2. `aws cloudformation update-stack ... ParameterKey=ContainerImage,ParameterValue=<image>`
   (all other params `UsePreviousValue=true`).
3. Wait for stack + ECS service stable, then verify `/healthz`.

## Notes

- Model keys (e.g. `ANTHROPIC_API_KEY`) are added to the app Secrets Manager
  secret out of band, never in CloudFormation, and referenced via `ValueFrom`.
- Secrets and account IDs must never be committed. HTTPS is fronted by
  CloudFront over the ALB.

## langfuse/

Self-hosted Langfuse v3 (web, worker, Postgres, ClickHouse, Redis, MinIO) via
docker compose, headlessly provisioned with local-dev keys so Rya exports
traces + eval scores with zero UI setup. See `docs/langfuse.md`.
