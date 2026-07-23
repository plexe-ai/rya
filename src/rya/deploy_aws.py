"""`rya deploy aws` - one command from a project directory to a running stack.

The spec is the manual deploy of loan-renewal-live (2026-07-23) and its
runbook (DEPLOY.md): preflight -> image -> network discovery -> CloudFormation
-> translated failures -> smoke. State in .rya/deploy.json makes the second
run an update. Uses boto3 (rya[bedrock] extra) and the docker CLI.
"""

from __future__ import annotations

import json
import secrets as _secrets
import subprocess
import time
from pathlib import Path
from typing import Optional

from .errors import RyaError

STATE_FILE = ".rya/deploy.json"
TEMPLATE = Path(__file__).parent / "deploy_assets" / "template.yaml"
LF_TEMPLATE = Path(__file__).parent / "deploy_assets" / "langfuse.yaml"


def _boto(service: str, region: str):
    try:
        import boto3
    except ImportError:
        raise RyaError("E_VALIDATION", "rya deploy aws needs boto3.",
                       hint="pip install 'rya[bedrock]'")
    return boto3.client(service, region_name=region)


# ---- state ------------------------------------------------------------------
def load_state(root: Path) -> Optional[dict]:
    p = root / STATE_FILE
    return json.loads(p.read_text()) if p.is_file() else None


def save_state(root: Path, state: dict) -> None:
    p = root / STATE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2))


# ---- preflight --------------------------------------------------------------
def preflight(root: Path, manifest, region: str, log) -> dict:
    """Fail fast on everything that would otherwise cost 12 minutes to learn."""
    sts = _boto("sts", region)
    try:
        ident = sts.get_caller_identity()
    except Exception as e:
        raise RyaError("E_VALIDATION", f"AWS credentials not usable: {e}",
                       hint="Configure credentials for the target account (aws configure / SSO).")
    account = ident["Account"]
    log(f"account {account} ({region})")

    if subprocess.run(["docker", "buildx", "version"], capture_output=True).returncode != 0:
        raise RyaError("E_VALIDATION", "docker buildx is required to build the image.",
                       hint="Install Docker Desktop / docker + buildx.")

    if manifest.model.provider == "bedrock":
        # The most expensive-to-discover failure: model access / payment.
        rt = _boto("bedrock-runtime", region)
        model = manifest.model.default
        if model in ("mock-llm", "mock"):
            model = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        try:
            rt.converse(modelId=model, messages=[{"role": "user", "content": [{"text": "ok"}]}],
                        inferenceConfig={"maxTokens": 5})
            log(f"bedrock access verified: {model}")
        except Exception as e:
            raise RyaError(
                "E_VALIDATION",
                f"Bedrock model call failed for {model}: {str(e)[:160]}",
                hint="Enable Anthropic model access in the Bedrock console and ensure "
                     "the account has a valid Marketplace payment method - nothing else "
                     "will work until this call succeeds.")
    return {"account": account}


# ---- image ------------------------------------------------------------------
def build_and_push(root: Path, name: str, account: str, region: str, log,
                   repo: str = "rya-control-plane", skip_build: bool = False) -> str:
    ecr = _boto("ecr", region)
    try:
        ecr.create_repository(repositoryName=repo)
        log(f"created ECR repo {repo}")
    except Exception:
        pass  # exists
    registry = f"{account}.dkr.ecr.{region}.amazonaws.com"
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                         text=True, cwd=root).stdout.strip() or "latest"
    image = f"{registry}/{repo}:{name}-{sha}"
    if skip_build:
        # trust but verify: a tag for THIS commit must actually be in ECR,
        # otherwise ECS gets an unpullable image and the update hangs.
        try:
            ecr.describe_images(repositoryName=repo,
                                imageIds=[{"imageTag": f"{name}-{sha}"}])
            log(f"skipping build; using {image}")
            return image
        except Exception:
            log(f"--skip-build requested but {name}-{sha} is not in ECR; building")
    login = subprocess.run(f"aws ecr get-login-password --region {region} | "
                           f"docker login --username AWS --password-stdin {registry}",
                           shell=True, capture_output=True, text=True)
    if login.returncode != 0:
        raise RyaError("E_RUNTIME", f"ECR login failed: {login.stderr[:200]}")
    # Find the rya repo root (Dockerfile.project builds runtime + project).
    rya_root = Path(__file__).resolve().parents[2]
    dockerfile = rya_root / "deploy" / "aws" / "Dockerfile.project"
    if not dockerfile.is_file():
        raise RyaError("E_VALIDATION", "Dockerfile.project not found (needs a rya source checkout).",
                       hint="Run from an environment with the rya repo available.")
    rel_project = root.resolve().relative_to(rya_root) if str(root.resolve()).startswith(str(rya_root)) else None
    if rel_project is None:
        raise RyaError("E_VALIDATION", "Project must live inside the rya checkout to bake the image.",
                       hint="Clone the agent into rya/examples/ or build/push the image manually.")
    log(f"building {image} (arm64)...")
    b = subprocess.run(["docker", "buildx", "build", "--platform", "linux/arm64",
                        "-f", str(dockerfile), "--build-arg", f"PROJECT={rel_project}",
                        "-t", image, "--push", str(rya_root)],
                       capture_output=True, text=True)
    if b.returncode != 0:
        raise RyaError("E_RUNTIME", f"image build failed: {b.stderr[-300:]}")
    log("image pushed")
    return image


# ---- network ----------------------------------------------------------------
def discover_network(region: str, log, vpc_id: Optional[str] = None) -> dict:
    ec2 = _boto("ec2", region)
    if vpc_id is None:
        vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
        if not vpcs:
            raise RyaError("E_VALIDATION", "No default VPC in this account/region.",
                           hint="Pass --vpc and --subnets explicitly.")
        vpc_id = vpcs[0]["VpcId"]
    subnets = sorted(s["SubnetId"] for s in
                     ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"])
    if not subnets:
        raise RyaError("E_VALIDATION", f"VPC {vpc_id} has no subnets.")
    log(f"network: {vpc_id}, {len(subnets)} subnets")
    return {"vpc": vpc_id, "subnets": subnets[:3]}


# ---- stack ------------------------------------------------------------------
def deploy_stack(stack: str, region: str, image: str, net: dict, log,
                 count: int = 2, multi_az: bool = True,
                 db_class: str = "db.t3.micro",
                 extra_params: Optional[dict] = None) -> dict:
    cfn = _boto("cloudformation", region)
    params = {
        "VpcId": net["vpc"],
        "PublicSubnetIds": ",".join(net["subnets"]),
        "PrivateSubnetIds": ",".join(net["subnets"]),
        "ContainerImage": image,
        "DBUsername": "rya",
        "DBInstanceClass": db_class,
        "DesiredCount": str(count),
        "DBMultiAZ": "true" if multi_az else "false",
        "AnthropicKeyEnabled": "false",
        "RyaSecretKey": _secrets.token_hex(24),
        "RyaAdminToken": _secrets.token_hex(24),
    }
    params.update(extra_params or {})
    exists = True
    try:
        cur = cfn.describe_stacks(StackName=stack)["Stacks"][0]
        # On update, keep existing secrets (rotating logs everyone out) and the
        # network (changing the RDS subnet set while the instance is in it fails).
        current = {p["ParameterKey"]: p for p in cur.get("Parameters", [])}
        for key in ("RyaSecretKey", "RyaAdminToken",
                    "VpcId", "PublicSubnetIds", "PrivateSubnetIds"):
            if key in current:
                params[key] = None  # use UsePreviousValue
        # keep existing Langfuse wiring unless this run re-specifies it
        for key in ("LangfuseHost", "LangfusePublicKey", "LangfuseSecretKey"):
            if key in current and key not in params:
                params[key] = None
    except Exception:
        exists = False

    cf_params = [({"ParameterKey": k, "UsePreviousValue": True} if v is None
                  else {"ParameterKey": k, "ParameterValue": v}) for k, v in params.items()]
    body = TEMPLATE.read_text()
    kwargs = dict(StackName=stack, TemplateBody=body, Parameters=cf_params,
                  Capabilities=["CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND"])
    try:
        if exists:
            log(f"updating stack {stack}...")
            cfn.update_stack(**kwargs)
        else:
            log(f"creating stack {stack} (~12 min - RDS is the clock)...")
            cfn.create_stack(**kwargs)
    except Exception as e:
        if "No updates are to be performed" in str(e):
            log("stack already up to date")
            return _outputs(cfn, stack)
        raise RyaError("E_RUNTIME", f"CloudFormation rejected the deploy: {str(e)[:200]}")

    return wait_stack(cfn, stack, region, log, creating=not exists)


def wait_stack(cfn, stack: str, region: str, log, creating: bool) -> dict:
    good = {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
    bad = {"ROLLBACK_COMPLETE", "ROLLBACK_IN_PROGRESS", "CREATE_FAILED",
           "UPDATE_ROLLBACK_COMPLETE", "UPDATE_ROLLBACK_IN_PROGRESS", "DELETE_COMPLETE"}
    last = ""
    while True:
        try:
            st = cfn.describe_stacks(StackName=stack)["Stacks"][0]["StackStatus"]
        except Exception as e:
            if type(e).__name__ in ("EndpointConnectionError", "ConnectTimeoutError",
                                    "ReadTimeoutError", "ConnectionClosedError"):
                log("network blip while polling; retrying...")
                time.sleep(15)
                continue
            raise
        if st != last:
            log(f"stack: {st}")
            last = st
        if st in good:
            return _outputs(cfn, stack)
        if st in bad:
            raise RyaError("E_RUNTIME", f"stack {st}", hint=translate_failure(cfn, stack, region))
        time.sleep(15)


def _outputs(cfn, stack: str) -> dict:
    out = cfn.describe_stacks(StackName=stack)["Stacks"][0].get("Outputs", [])
    return {o["OutputKey"]: o["OutputValue"] for o in out}


def translate_failure(cfn, stack: str, region: str) -> str:
    """The 90 minutes I spent in stopped-task JSON, as one sentence."""
    try:
        events = cfn.describe_stack_events(StackName=stack)["StackEvents"]
        fails = [e for e in events if "FAILED" in e.get("ResourceStatus", "")
                 and e.get("ResourceStatusReason")]
        # "Resource update/creation cancelled" is the cascade, not the cause.
        fail = next((e for e in fails if "cancelled" not in e["ResourceStatusReason"]),
                    fails[0] if fails else None)
        if fail is None:
            return "Check `aws cloudformation describe-stack-events` for the first failure."
        reason = fail["ResourceStatusReason"]
        if fail["LogicalResourceId"] == "Service":
            # dig out the real ECS stopped-task reason
            try:
                ecs = _boto("ecs", region)
                cluster = next(c for c in ecs.list_clusters()["clusterArns"] if stack in c)
                tasks = ecs.list_tasks(cluster=cluster, desiredStatus="STOPPED")["taskArns"][:1]
                if tasks:
                    t = ecs.describe_tasks(cluster=cluster, tasks=tasks)["tasks"][0]
                    reason = t.get("stoppedReason", reason)
                    if "secret" in reason.lower():
                        reason += " -> a required Secrets Manager JSON key is missing"
                    if "pull" in reason.lower():
                        reason += " -> check the image URI / ECR permissions"
            except Exception:
                pass
        return f"{fail['LogicalResourceId']}: {reason[:300]}"
    except Exception:
        return "Could not read stack events - inspect the CloudFormation console."



# ---- langfuse ---------------------------------------------------------------
_LF_SECRET_KEYS = ("PostgresPassword", "ClickhousePassword", "RedisPassword",
                   "Salt", "EncryptionKey", "NextAuthSecret",
                   "ProjectPublicKey", "ProjectSecretKey",
                   "AdminEmail", "AdminPassword")


def deploy_langfuse(stack: str, region: str, net: dict, log,
                    prior: Optional[dict] = None, persist=None) -> dict:
    """Create or update the in-VPC Langfuse stack; returns url + API keys.

    All secrets are generated on create and recorded in deploy state; on
    update every secret uses UsePreviousValue (the LANGFUSE_INIT_* values
    only take effect on first boot, so rotating them via the stack would
    desync from the database).
    """
    cfn = _boto("cloudformation", region)
    prior = prior or {}
    exists = True
    try:
        cfn.describe_stacks(StackName=stack)
    except Exception:
        exists = False

    if exists:
        if not prior.get("public_key"):
            raise RyaError(
                "E_VALIDATION",
                f"Langfuse stack {stack} exists but .rya/deploy.json has no keys.",
                hint="Copy the project API keys from the Langfuse UI into the "
                     "langfuse section of .rya/deploy.json, then rerun.")
        cf_params = ([{"ParameterKey": k, "UsePreviousValue": True}
                      for k in _LF_SECRET_KEYS]
                     + [{"ParameterKey": k, "UsePreviousValue": True}
                        for k in ("VpcId", "SubnetIds", "TaskCpu", "TaskMemory")])
        info = dict(prior)
        try:
            log(f"updating langfuse stack {stack}...")
            cfn.update_stack(StackName=stack, TemplateBody=LF_TEMPLATE.read_text(),
                             Parameters=cf_params, Capabilities=["CAPABILITY_IAM"])
        except Exception as e:
            if "No updates are to be performed" in str(e):
                log("langfuse stack already up to date")
                info["url"] = _outputs(cfn, stack).get("LangfuseUrl", info.get("url"))
                return info
            raise RyaError("E_RUNTIME", f"Langfuse stack update rejected: {str(e)[:200]}")
        out = wait_stack(cfn, stack, region, log, creating=False)
        info["url"] = out.get("LangfuseUrl", info.get("url"))
        return info

    info = {
        "public_key": f"pk-lf-{_secrets.token_hex(12)}",
        "secret_key": f"sk-lf-{_secrets.token_hex(24)}",
        "admin_email": "admin@rya.local",
        "admin_password": _secrets.token_hex(12),
    }
    values = {
        "VpcId": net["vpc"],
        "SubnetIds": ",".join(net["subnets"]),
        "PostgresPassword": _secrets.token_hex(16),
        "ClickhousePassword": _secrets.token_hex(16),
        "RedisPassword": _secrets.token_hex(16),
        "Salt": _secrets.token_hex(16),
        "EncryptionKey": _secrets.token_hex(32),
        "NextAuthSecret": _secrets.token_hex(24),
        "ProjectPublicKey": info["public_key"],
        "ProjectSecretKey": info["secret_key"],
        "AdminEmail": info["admin_email"],
        "AdminPassword": info["admin_password"],
    }
    if persist:
        persist(dict(info, stack=stack))  # before create: a crash mid-wait must not lose the keys
    log(f"creating langfuse stack {stack} (~7 min)...")
    cfn.create_stack(StackName=stack, TemplateBody=LF_TEMPLATE.read_text(),
                     Parameters=[{"ParameterKey": k, "ParameterValue": v}
                                 for k, v in values.items()],
                     Capabilities=["CAPABILITY_IAM"])
    out = wait_stack(cfn, stack, region, log, creating=True)
    info["url"] = out["LangfuseUrl"]
    return info


# ---- smoke ------------------------------------------------------------------
def smoke(url: str, log, timeout: int = 300) -> dict:
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/healthz", timeout=10) as r:
                h = json.loads(r.read())
                log(f"healthz: agent={h.get('agent')} auth={h.get('authEnabled')} "
                    f"store={h.get('store')}")
                return h
        except Exception:
            time.sleep(10)
    raise RyaError("E_RUNTIME", f"{url}/healthz did not come up within {timeout}s",
                   hint="Tasks may still be rolling - check the ECS service events.")


# ---- destroy ----------------------------------------------------------------
def destroy(stack: str, region: str, log) -> None:
    cfn = _boto("cloudformation", region)
    log(f"deleting stack {stack}...")
    cfn.delete_stack(StackName=stack)
    while True:
        try:
            st = cfn.describe_stacks(StackName=stack)["Stacks"][0]["StackStatus"]
        except Exception:
            log("stack deleted")
            return
        log(f"stack: {st}")
        time.sleep(20)
