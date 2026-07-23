# Deploying to a fresh AWS account

## The one command

```bash
cd examples/loan-renewal
rya deploy aws                 # preflight -> image -> network -> stack -> smoke
rya deploy status              # stack + URL
rya deploy destroy             # tear it all down (asks first)
```

`rya deploy aws` runs everything in the manual runbook below: verifies AWS
credentials, docker buildx, and a real Bedrock model call (the expensive
failure, caught first), builds and pushes the arm64 image to ECR, discovers
the default VPC, creates or updates the CloudFormation stack (secrets and
network are preserved on updates), translates stack failures into the actual
root cause (ECS stopped-task reasons included), and polls `/healthz` until
the ALB answers. State lands in `.rya/deploy.json`, so the second run is an
update. Flags: `--region`, `--stack`, `--count`, `--no-ha`, `--skip-build`.

First run on a fresh account: ~15 minutes, most of it RDS. Prereqs: AWS CLI
authenticated to the target account, Docker with buildx, this repo inside the
rya checkout, and Bedrock Anthropic model access enabled (step 0 below - the
preflight checks it and tells you if not).

The manual steps below are what the command automates - useful for
understanding, debugging, or air-gapped variants.

## 0. Bedrock access (the one account-specific step - do it FIRST)
Console -> Bedrock -> Model access -> enable Anthropic Claude models. The
account needs a valid payment method (Marketplace entitlement). Verify before
anything else:
```bash
aws bedrock-runtime converse --region us-east-1 \
  --model-id us.anthropic.claude-haiku-4-5-20251001-v1:0 \
  --messages '[{"role":"user","content":[{"text":"ok"}]}]' \
  --inference-config '{"maxTokens":5}'
```
If this fails, fix model access / billing before deploying - nothing else will work.

## 1. Image
```bash
ACCT=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1
aws ecr create-repository --repository-name rya-control-plane 2>/dev/null || true
aws ecr get-login-password | docker login --username AWS --password-stdin $ACCT.dkr.ecr.$REGION.amazonaws.com
# from the rya repo root:
docker buildx build --platform linux/arm64 -f deploy/aws/Dockerfile.project \
  --build-arg PROJECT=examples/loan-renewal \
  -t $ACCT.dkr.ecr.$REGION.amazonaws.com/rya-control-plane:loan-renewal-v1 --push .
```

## 2. Network (default VPC is fine)
```bash
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query "Vpcs[0].VpcId" --output text)
SUBNETS=$(aws ec2 describe-subnets --filters Name=vpc-id,Values=$VPC \
  --query "Subnets[].SubnetId" --output text | tr '\t' ',')
```

## 3. Stack (~12 min; RDS is the clock)
```bash
aws cloudformation deploy --stack-name loan-renewal-live \
  --template-file deploy/aws/template.yaml --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    "VpcId=$VPC" "PublicSubnetIds=$SUBNETS" "PrivateSubnetIds=$SUBNETS" \
    "ContainerImage=$ACCT.dkr.ecr.$REGION.amazonaws.com/rya-control-plane:loan-renewal-v1" \
    "DBUsername=rya" "DBInstanceClass=db.t3.micro" \
    "DesiredCount=2" "DBMultiAZ=true" "AnthropicKeyEnabled=false" \
    "RyaSecretKey=$(openssl rand -hex 24)" "RyaAdminToken=$(openssl rand -hex 24)"
```

## 4. Smoke + first user
```bash
URL=$(aws cloudformation describe-stacks --stack-name loan-renewal-live \
  --query "Stacks[0].Outputs[?OutputKey=='AlbUrl'].OutputValue" --output text)
curl $URL/healthz          # expect authEnabled:true, multiTenant:true, store:postgres
open $URL/app/             # sign up the first account -> owns the first workspace
```

## Known sharp edges
- New-account Fargate/RDS service quotas are usually fine for this size; if
  task placement fails, check Service Quotas for Fargate vCPUs.
- The ALB is HTTP. Put CloudFront + ACM in front before real users
  (ENTERPRISE.md, security block).
- The stack is self-contained (own DB, secrets, S3 bucket, IAM). Deleting the
  stack deletes everything except the ECR image.

The end state for this document is `rya deploy aws` doing all of it as one
command - this runbook is that command's spec.
