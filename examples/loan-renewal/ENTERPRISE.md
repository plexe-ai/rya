# Enterprise hardening plan - loan-renewal on Rya

Status: system is live (loan-renewal-live, us-east-1) and correct; this is the
gap list between "working in production" and "enterprise grade", priority
ordered. Each item is scoped and independent.

## P0 - correctness under concurrency (before any multi-worker scale-out)
- [x] Fan-in: platform job groups (schedule_group + exactly-once on_complete)
- [x] Concurrent job execution inside each worker (bounded pool in the serve
      jobs loop) - today jobs run serially per task. ~0.5d
- [x] Idempotent gated write (keyed UPDATE + CIF guard)
- [x] Replay-discipline lint (`rya doctor`): flag raw IO in handlers. ~1d

## P0 - security and identity
- [ ] HTTPS: CloudFront + ACM in front of the ALB; HTTP disabled. ~0.5d
- [ ] Session-to-JWT bridge (X-Rya-User-Token) so every run and approval
      records WHO acted - "approved by sarah@bbg.bank" in the audit trail. ~1d
- [ ] Private subnets for tasks + VPC endpoints (Bedrock, S3, Secrets
      Manager) - document bytes never traverse public internet. ~1d
- [ ] Bank IdP SSO (SAML/OIDC in front of /v1 auth). ~2d, needs bank input

## P1 - documents at enterprise size and retention
- [x] S3 blob backend behind the files API (metadata stays in rya_files;
      bytes in S3; task-role IAM; lifecycle -> Glacier for retention). ~1d
- [x] Presigned direct-to-S3 uploads from the app (large files bypass the
      API task entirely). ~0.5d
- [x] Chunked extraction: page-split PDFs over the Converse ~4.5MB/doc limit
      into per-chunk jobs + a merge step (same queue fan-out pattern). ~0.5d

## P1 - availability and proof
- [x] DesiredCount >= 2 + target-tracking autoscaling; multi-AZ RDS with
      PITR backups. Parameters + cost, ~0.5d
- [ ] Load test: 500 concurrent cases, publish real numbers (no
      results-shopping - whatever they are). ~1d
- [ ] CloudWatch alarms (task health, queue depth, dead-letter count, DB
      connections) + dashboard. ~0.5d

## P2 - operations
- [x] `rya deploy aws` one-command deploy (preflight incl. live Bedrock probe,
      build+push, VPC discovery, create/update stack with secret+network
      preservation, root-cause failure translation, healthz smoke; plus
      `deploy status` / `deploy destroy`). Verified against loan-renewal-live.
- [x] Langfuse in the VPC: `rya deploy aws --langfuse` provisions the
      {stack}-langfuse Fargate stack (web+worker+postgres+clickhouse+redis,
      EFS + S3 via task role) and wires LANGFUSE_* into the app tasks.
      Verified live: 8 eval traces + 28 scores in the in-VPC instance.
- [ ] DR runbook: RDS restore drill, region-loss stance, key rotation. ~1d
- [ ] Real bank integration: archive/LA tools -> `url:` HTTP tools against
      their systems, scoped credentials from connections. Sized with the bank.

Total to enterprise-grade (excl. SSO + bank integration): ~10 engineering days.
