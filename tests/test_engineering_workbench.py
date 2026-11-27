"""The Team engineering route, using durable Core state and an inert Worker double."""

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import time

import pytest

from ii_pro_core import Action
from ii_pro_core.errors import ApprovalRequired, ArtifactMismatch, ValidationError
from ii_pro_core.executors import ExecutionResult, Executor, ProducedArtifact
from ii_pro_core.security import canonical
from ii_pro_local.engineering_executor import artifact_json, change
from ii_pro_local.snapshot import Snapshot
from ii_pro_team.workbench import package
from ii_pro_worker.identity import archived_school_review
from ii_pro_worker.wire import fingerprint
from ii_pro_worker.profile import DockerProfile
from ii_pro_ui.functional import IMAGE, REVIEW
from test_gpt_engineering import BASE, JOB, draft, studio, team
from test_gpt_team import decision, wait
from test_local_agent import BUGGY, fixture_project
from test_local_engine_ui import request


@pytest.fixture
def lab(tmp_path):
    project = tmp_path / "project"
    _, revision = fixture_project(project)
    snapshot = Snapshot.register(project, base=revision, revision=revision, paths=["pricing.py"])
    return {"pricing": ("Developer fixture", snapshot)}, project


class WorkerDouble(Executor):
    name = "engineering_worker"
    revision = "engineering-fixture-v1"

    def __init__(self, passed):
        self.passed, self.calls = passed, 0

    def approval_configuration(self):
        return {"fixture_passes": self.passed}

    def execute(self, request):
        self.calls += 1
        if self.passed is None:
            raise RuntimeError("Fixture loses the result after dispatch")
        frozen = json.loads(request.action.parameters_json)["arguments"]["package"]
        cases = [
            {"id": c["id"], "status": "passed" if index < self.passed else "failed",
             "diagnostic": "ok" if index < self.passed else "wrong_value"}
            for index, c in enumerate(frozen["cases"])
        ]
        payload = {key: frozen[key] for key in ("source_sha256", "suite_digest")}
        payload.update(method="pricing-arithmetic-tests-v1", package_id=frozen["id"], package_digest=frozen["digest"],
                       cases=cases, passed=self.passed, total=len(cases),
                       resources={"memory_peak": 100, "cpu_usage_usec": 0, "pids_current": 1})
        receipt = {"stop_confirmed": True, "process_exit_code": 0, "result": {
            "status": "ok", "exit_code": 0, "stdout": canonical(payload), "stderr": ""}}
        return ExecutionResult((ProducedArtifact("result.json", canonical(receipt).encode()),), storage="sqlite")


def advance(server, step):
    return team(server, "advance", {"job_id": JOB, "phase": step})["working_copy"]


def seed(server):
    plan = team(server, "prepare", draft("plan"))["phases"][0]
    team(server, "decision", decision(plan))
    wait(server.training, JOB)
    engineer = team(server, "advance", {"job_id": JOB, "phase": "implement"})["phases"][1]
    team(server, "decision", decision(engineer))
    wait(server.training, JOB)


def submit(operation):
    approval = operation["approval"]
    return {"run_id": operation["run"], "approval_id": approval["id"], "spec_digest": approval["digest"], "allow": True}


def completed(server):
    deadline = time.monotonic() + 4
    while server.training.threads and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not server.training.threads
    return team(server, "state")["jobs"][0]["working_copy"]


@pytest.mark.parametrize("passed", [6, 2])
def test_apply_worker_rollback_and_recovery_over_http(tmp_path, lab, passed):
    projects, source = lab
    root = tmp_path / "studio"
    worker = WorkerDouble(passed)
    with studio(root, projects, engineering_worker=worker) as server:
        seed(server)
        prepared = advance(server, "apply")
        apply = prepared["phases"]["apply"]
        assert apply["approval"]["status"] == "pending" and prepared["backup"] == "planned"
        core = server.training.shared_core
        with pytest.raises(ApprovalRequired):
            core.runs.execute(apply["run"])
        assert request(server, BASE + "decision", submit(apply), trusted=False)[0] == 403
        team(server, "decision", submit(apply))
        applied = completed(server)
        assert applied["backup"] == "sealed"
        assert artifact_json(core, apply["run"], "backup.json")["files"] == {"pricing.py": BUGGY}
        assert (source / "pricing.py").read_text() == BUGGY
        testing = advance(server, "test")["phases"]["test"]
        assert testing["approval"]["id"] != apply["approval"]["id"]
        assert request(server, BASE + "decision", {**submit(testing), "approval_id": apply["approval"]["id"]})[0] == 409
        team(server, "revoke", {k: v for k, v in submit(testing).items() if k != "allow"})
        assert request(server, BASE + "decision", submit(testing))[0] == 409
        testing = advance(server, "test")["phases"]["test"]
        with ThreadPoolExecutor(max_workers=2) as clients:
            for response in clients.map(lambda _: request(server, BASE + "decision", submit(testing)), range(2)):
                assert response[0] == 200
        view = completed(server)
        assert worker.calls == 1 and view["test"]["passed"] == passed
        assert view["test"]["status"] == ("mock_passed" if passed == 6 else "mock_failed")
        if passed < 6:
            assert view["rollback"] == "baseline_selected"
            (source / "pricing.py").write_text("# independent local change\n", encoding="utf-8")
            restore = advance(server, "rollback")["phases"]["rollback"]
            team(server, "decision", submit(restore))
            view = completed(server)
            assert view["rollback"] == "restored"
            assert (source / "pricing.py").read_text() == "# independent local change\n"
    with studio(root, projects) as server:
        persisted = completed(server)
        assert persisted["test"] == view["test"]
        assert persisted["rollback"] == view["rollback"]
        api = request(server, "/api/local-engine/state")[1]
        assert api["unreviewed_materials"][0]["status"] == "unreviewed"
        assert api["unreviewed_materials"][0]["material"]["backend"] == "mock"
        assert not json.loads(server.training.shared_core.memory.prepare_review("discount", "f" * 64).parameters_json).get("memory")


def test_changed_ref_blocks_apply_and_leaves_checkout_unchanged(tmp_path, lab):
    projects, source = lab
    with studio(tmp_path / "study", projects) as server:
        seed(server)
        operation = advance(server, "apply")["phases"]["apply"]
        (source / ".git" / "HEAD").write_text("e" * 40 + "\n", encoding="ascii")
        team(server, "decision", submit(operation))
        failed = completed(server)
        assert failed["phases"]["apply"]["status"] == "failed"
        assert not failed["phases"]["apply"]["artifacts"]
        assert (source / "pricing.py").read_text() == BUGGY


@pytest.mark.parametrize("candidate", [
    "def discounted(price, percent):\n    return price.real\n",
    "def discounted(price, percent):\n    return int(price)\n",
    "def discounted(price, percent=0):\n    return price\n",
])
def test_pricing_profile_rejects_unapproved_execution_shapes(candidate):
    with pytest.raises(ValidationError):
        package(JOB, candidate)


def test_deletion_is_a_separate_approved_action():
    params = {"project": "pricing", "snapshot": "a" * 64, "changes": [change("pricing.py", BUGGY, None)], "parent": None}
    with pytest.raises(ValidationError):
        Action.build("engineering.apply", params)
    assert Action.build("engineering.delete", params).requires_approval


def test_school_keeps_its_exact_approved_image():
    assert fingerprint(archived_school_review(DockerProfile(IMAGE))) == REVIEW


def test_artifact_integrity_error_quarantines_candidate(tmp_path, lab, monkeypatch):
    projects, _ = lab
    with studio(tmp_path / "study", projects, engineering_worker=WorkerDouble(6)) as server:
        seed(server)
        operation = advance(server, "apply")["phases"]["apply"]
        team(server, "decision", submit(operation))
        assert completed(server)["backup"] == "sealed"
        core = server.training.shared_core
        with pytest.raises(sqlite3.IntegrityError), core.store.transaction() as conn:
            conn.execute("UPDATE artifact_blobs SET content=? WHERE artifact_id=(SELECT id FROM artifacts WHERE run_id=? AND relative_path LIKE '%/working-copy.json')",
                         (b"{}", operation["run"]))

        def corrupt_copy(core, run_id, name):
            if name == "working-copy.json":
                raise ArtifactMismatch("Simulated corrupt storage")
            return artifact_json(core, run_id, name)

        monkeypatch.setattr("ii_pro_team.workbench.artifact_json", corrupt_copy)
        assert completed(server)["rollback"] == "quarantined"
        assert request(server, BASE + "advance", {"job_id": JOB, "phase": "test"})[0] == 409


def test_unknown_worker_remains_blocked_after_reopen(tmp_path, lab):
    projects, _ = lab
    root = tmp_path / "studio"
    backend = WorkerDouble(None)
    with studio(root, projects, engineering_worker=backend) as server:
        seed(server)
        operation = advance(server, "apply")["phases"]["apply"]
        team(server, "decision", submit(operation))
        assert completed(server)["backup"] == "sealed"
        attempt = advance(server, "test")["phases"]["test"]
        team(server, "decision", submit(attempt))
        assert completed(server)["test"]["status"] == "unknown"
        assert backend.calls == 1
    with studio(root, projects, engineering_worker=backend) as server:
        assert completed(server)["test"]["status"] == "unknown"
        assert request(server, BASE + "decision", submit(attempt))[0] == 409
        assert request(server, BASE + "advance", {"job_id": JOB, "phase": "rollback"})[0] == 409
        assert backend.calls == 1


@pytest.mark.parametrize("deleting", [False, True])
def test_approved_creation_or_deletion_can_restore_the_baseline(tmp_path, deleting):
    source = tmp_path / "copy-source"
    _, revision = fixture_project(source)
    snapshot = Snapshot.register(source, base=revision, revision=revision, paths=["pricing.py"],
                                 create_paths=["extra.py"])
    with studio(tmp_path / "study", {"pricing": ("Python fixture", snapshot)}) as server:
        server.training._open()
        core = server.training.shared_core

        def run_action(phase, item, parent=None):
            action = Action.build("engineering." + phase, {"project": "pricing", "snapshot": snapshot.identity,
                                  "changes": [item], "parent": parent})
            task = core.tasks.create(phase, executor="engineering_copy", idempotency_key=phase)
            core.tasks.transition(task.id, "planned")
            run = core.runs.create(task.id, action=action, idempotency_key=phase)
            permit = core.approvals.request(run.id)
            core.approvals.decide(permit.id, allow=True)
            assert core.runs.execute(run.id, approval_id=permit.id).status == "succeeded"
            return run.id

        if deleting:
            path, before, after = "pricing.py", BUGGY, None
        else:
            path, before, after = "extra.py", None, "CONSTANT = 1\n"
        applied = run_action("delete" if deleting else "apply", change(path, before, after))
        restored = run_action("rollback", change(path, after, before), applied)
        assert artifact_json(core, restored, "working-copy.json")["files"] == {"pricing.py": BUGGY}
        assert not (source / "extra.py").exists()
        assert (source / "pricing.py").read_text() == BUGGY
