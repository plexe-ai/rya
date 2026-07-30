import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rya.cli import scaffold  # noqa: E402
from rya.manifest import load_manifest  # noqa: E402
from rya.runtime import Engine, load_agent  # noqa: E402
from rya.store import Store  # noqa: E402

# Credentials whose mere PRESENCE in the shell used to change which world the
# test suite talked to. `model.provider: auto` resolves to a real provider when a
# key is visible, so `pytest` on a developer's machine silently billed live model
# calls and 14 tests asserted against whatever the model happened to say.
#
# PLATFORM_DESIGN D8 makes config declared rather than ambient, and this is the
# test-suite half of that: a test states the world it wants instead of inheriting
# one. Tests that need a credential set it themselves with `monkeypatch.setenv`
# (see tests/test_config.py), which still works — this fixture only clears the
# INHERITED environment.
_AMBIENT_PROVIDER_KEYS = (
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "RYA_LLM_MODEL", "RYA_OPENAI_MODEL",
    "RYA_BEDROCK", "RYA_BEDROCK_MODEL", "RYA_BEDROCK_REGION",
    "RYA_KEYLESS", "RYA_FORCE_MOCK", "RYA_GOVERNANCE_URL", "RYA_PLATFORM_TOKEN",
    "RYA_ADAPTER_MODE", "SLACK_WEBHOOK_URL", "RESEND_API_KEY",
    "RYA_GUARD_PATH", "RYA_DATABASE_URL", "DATABASE_URL", "RYA_ENVIRONMENT",
    # Object stores. A developer with a real bucket exported would otherwise make
    # bundle and file tests reach for S3 — the same class of ambient-input bug D8
    # exists to kill, just aimed at storage instead of models.
    "RYA_BUNDLES_S3_BUCKET", "RYA_BUNDLES_S3_PREFIX", "RYA_BUNDLES_S3_REGION",
    "RYA_FILES_S3_BUCKET",
)


@pytest.fixture(autouse=True)
def hermetic_env(monkeypatch):
    """Every test starts from a declared, credential-free environment."""
    for name in _AMBIENT_PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def live_provider_key(monkeypatch):
    """Opt back in to a real provider credential from the ambient shell.

    For tests that genuinely exercise a live provider. They should also skip
    when the key is absent — a test that needs the network must say so.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("needs a real ANTHROPIC_API_KEY")
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    return key


@pytest.fixture
def project(tmp_path) -> Path:
    """A scaffolded, ready-to-run project in a temp dir."""
    scaffold.write_project(tmp_path, "test-agent", template="demo")
    return tmp_path


@pytest.fixture
def engine(project) -> Engine:
    manifest = load_manifest(project / "rya.agent.yaml")
    agent = load_agent(manifest, project)
    return Engine(manifest, agent, Store(project), project)
