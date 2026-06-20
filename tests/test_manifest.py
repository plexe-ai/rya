import pytest

from rya.errors import RyaError
from rya.manifest import load_manifest


def test_loads_scaffolded_manifest(project):
    m = load_manifest(project / "rya.agent.yaml")
    assert m.name == "test-agent"
    assert m.tool_permission("email.send").value == "approval_required"
    assert m.tool_permission("crm.lookup").value == "allowed"


def test_missing_manifest_raises_stable_code(tmp_path):
    with pytest.raises(RyaError) as exc:
        load_manifest(tmp_path / "nope.yaml")
    assert exc.value.code == "E_MANIFEST_NOT_FOUND"


def test_invalid_runtime_rejected(tmp_path):
    (tmp_path / "rya.agent.yaml").write_text("name: x\nruntime: cobol\n")
    with pytest.raises(RyaError) as exc:
        load_manifest(tmp_path / "rya.agent.yaml")
    assert exc.value.code == "E_MANIFEST_INVALID"


def test_cron_trigger_requires_schedule(tmp_path):
    (tmp_path / "rya.agent.yaml").write_text(
        "name: x\nruntime: python\ntriggers:\n  - id: t\n    type: cron\n    handler: h\n"
    )
    with pytest.raises(RyaError) as exc:
        load_manifest(tmp_path / "rya.agent.yaml")
    assert exc.value.code == "E_MANIFEST_INVALID"
