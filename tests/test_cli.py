import json
import subprocess
import sys
from pathlib import Path

from ii_pro_core import Core

ROOT = Path(__file__).resolve().parents[1]


def cli(state, *args, expected=0):
    result = subprocess.run([sys.executable, "-m", "ii_pro_core", "--state-dir", str(state), *args],
                            cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == expected, result.stderr + result.stdout
    return json.loads(result.stdout)


def test_cli_protected_workflow_requires_explicit_decision(tmp_path):
    state = tmp_path / "state"
    task = cli(state, "create", "--title", "CLI test", "--key", "task")
    cli(state, "plan", task["id"])
    run = cli(state, "prepare", task["id"], "--key", "run", "--protected")
    assert cli(state, "execute", run["id"], expected=2) == {"error": "ApprovalRequired"}
    approval = cli(state, "request-approval", run["id"])
    assert cli(state, "approval", approval["id"])["status"] == "pending"
    cli(state, "approve", approval["id"])
    assert cli(state, "execute", run["id"], "--approval", approval["id"])["status"] == "succeeded"
    cli(state, "verify", task["id"])
    assert cli(state, "save", task["id"])["status"] == "saved"


def test_cli_errors_do_not_print_credential_inputs(tmp_path):
    state = tmp_path / "state"
    result = cli(state, "create", "--title", "password=canary-private", "--key", "task", expected=2)
    assert result == {"error": "ValidationError"}
    with Core(state) as core:
        assert core.events() == []


def test_cli_replay_across_processes_does_not_dispatch_twice(tmp_path):
    state = tmp_path / "state"
    task = cli(state, "create", "--title", "Replay", "--key", "task")
    cli(state, "plan", task["id"])
    run = cli(state, "prepare", task["id"], "--key", "run")
    first = cli(state, "execute", run["id"])
    assert cli(state, "execute", run["id"]) == first
    with Core(state) as core:
        assert sum(e.kind == "run.started" for e in core.events()) == 1
        assert len(core.artifacts.list(run["id"])) == 1
