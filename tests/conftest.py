import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rya.cli import scaffold  # noqa: E402
from rya.manifest import load_manifest  # noqa: E402
from rya.runtime import Engine, load_agent  # noqa: E402
from rya.store import Store  # noqa: E402


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
