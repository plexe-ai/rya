import json
import os

from typer.testing import CliRunner

from rya.cli.main import app

runner = CliRunner()


def _run(args, cwd):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return runner.invoke(app, args)
    finally:
        os.chdir(old)


def test_dev_check_json(project):
    """`rya dev --check` is the instant manifest+code validation CI and tight
    edit loops depend on. Bare `rya dev` now starts the real two-process dev
    deployment (PLATFORM_DESIGN §10) and does not return."""
    res = _run(["dev", "--check", "--json"], project)
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data["ok"] is True
    assert data["ready"] is True
    assert "crm.lookup" in data["tools"]


def test_event_then_approve_flow_json(project):
    sent = _run(["events", "send", "--type", "message.received",
                 "--payload", '{"email":"ada@example.com"}', "--json"], project)
    assert sent.exit_code == 0
    payload = json.loads(sent.stdout)
    assert payload["status"] == "waiting_approval"
    approval_id = payload["pendingApproval"]

    listed = _run(["approvals", "list", "--status", "pending", "--json"], project)
    assert json.loads(listed.stdout)["count"] == 1

    approved = _run(["approvals", "approve", approval_id, "--json"], project)
    assert json.loads(approved.stdout)["runStatus"] == "completed"


def test_bad_payload_exit_code(project):
    res = _run(["events", "send", "--payload", "{not json}", "--json"], project)
    assert res.exit_code == 7  # EXIT_VALIDATION
    assert json.loads(res.stdout)["error"]["code"] == "E_VALIDATION"


def test_run_not_found_exit_code(project):
    res = _run(["runs", "trace", "run_nope", "--json"], project)
    assert res.exit_code == 4  # EXIT_NOT_FOUND
    assert json.loads(res.stdout)["error"]["code"] == "E_RUN_NOT_FOUND"


def test_status_json(project):
    _run(["events", "send", "--payload", '{"email":"a@b.com"}', "--json"], project)
    res = _run(["status", "--json"], project)
    data = json.loads(res.stdout)
    assert data["approvalsPending"] == 1
    assert data["runs"]["total"] == 1


def test_project_optional_needs_no_manifest(tmp_path):
    """`rya serve` must not require a mounted agent project.

    Since D21 `build_app` is manifest-free — it learns what agents exist from
    published versions and environment pointers — so a control plane serving only
    published bundles has no `rya.agent.yaml` anywhere. `serve` read one anyway, to
    print a name in its banner, and that made exactly that deployment unstartable
    (`E_MANIFEST_NOT_FOUND` on boot, crash-looping).
    """
    from rya.cli.main import _project, _project_optional

    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        root, manifest = _project_optional()
        assert manifest is None          # nothing mounted, and that is not an error
        assert root == tmp_path.resolve()  # still resolves a root for build_app

        # The strict helper still refuses, because every other command acts on one agent.
        try:
            _project()
        except Exception as exc:  # RyaError
            assert getattr(exc, "code", None) == "E_MANIFEST_NOT_FOUND"
        else:
            raise AssertionError("_project must still require a manifest")
    finally:
        os.chdir(old)


def test_project_optional_reads_the_manifest_when_there_is_one(project):
    """With a project mounted it behaves exactly as `_project` did, so the
    single-tenant banner keeps naming the agent it serves."""
    from rya.cli.main import _project_optional

    old = os.getcwd()
    os.chdir(project)
    try:
        _, manifest = _project_optional()
        assert manifest is not None
        assert manifest.name == "test-agent"
    finally:
        os.chdir(old)
