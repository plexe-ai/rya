# `deploy` - production deployment

Infrastructure-as-code for running Rya. See `deploy/aws/README.md` for the full
AWS topology and deploy steps.

## Contents

- `aws/template.yaml` - SAM/CloudFormation for the reference **single-tenant**
  posture: ECS Fargate (ARM64) behind an ALB, RDS Postgres with RLS, Cognito
  identity, ElastiCache, Secrets Manager, a JWT-revalidating mutator Lambda,
  CloudWatch logs. Runs with `RYA_MULTITENANT=1` + `RYA_JWKS_URL`.
- `aws/Dockerfile.baked` - runtime image that scaffolds the default agent.
- `aws/Dockerfile.project` - runtime image that bakes a SPECIFIC project dir
  (`--build-arg PROJECT=examples/...`), for deploying a real agent.

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
