import copy
import json

import pytest

from ii_pro_core import Action, Core
from ii_pro_core.errors import Conflict
from ii_pro_core.executors import ExecutionResult, Executor, ProducedArtifact
from ii_pro_core.knowledge import KnowledgeExecutor
from ii_pro_core.memory import validate_review_memory
from ii_pro_core.memory_transfer import export_card, import_card, source_execution_blocked, sync_revocation
from ii_pro_core.security import canonical, digest
from ii_pro_team.engineering import SOURCE
from ii_pro_team.workbench import package
from test_engineering_memory import entry


class SealedReceiptFixture(Executor):
    name = "engineering_worker"
    revision = "transfer-test-only"
    lose_receipt = False

    def approval_configuration(self):
        return {"backend": {"backend": "docker-fixed"}}

    def execute(self, request):
        if self.lose_receipt:
            raise RuntimeError("Fixture loses its receipt")
        frozen = json.loads(request.action.parameters_json)["arguments"]["package"]
        checks = {
            "method": "pricing-arithmetic-tests-v1", "package_id": frozen["id"],
            "package_digest": frozen["digest"], "source_sha256": frozen["source_sha256"],
            "suite_digest": frozen["suite_digest"],
            "cases": [{"id": row["id"], "status": "passed", "diagnostic": "ok"} for row in frozen["cases"]],
            "passed": 6, "total": 6, "resources": {"memory_peak": 1024, "cpu_usage_usec": 0, "pids_current": 1},
        }
        receipt = {"result": {"stdout": canonical(checks)}, "run_id": request.run_id,
                   "stop_confirmed": True, "process_exit_code": 0}
        return ExecutionResult((ProducedArtifact("result.json", canonical(receipt).encode()),), storage="sqlite")


def prepare(core, executor, operation, *, key=None):
    key = key or executor
    task = core.tasks.create(executor, executor=executor, idempotency_key=key)
    core.tasks.transition(task.id, "planned")
    return core.runs.create(task.id, idempotency_key=key, action=operation)


def execute(core, run):
    approval = core.approvals.request(run.id)
    core.approvals.decide(approval.id, allow=True)
    assert core.runs.execute(run.id, approval_id=approval.id).status == "succeeded"


def test_transfer_origin_history_stale_replay_and_destination_restart(tmp_path):
    verifier = KnowledgeExecutor()
    executor = SealedReceiptFixture()
    source = tmp_path / "source"
    dest = tmp_path / "destination"
    with Core(source, executors=[executor, verifier]) as owner:
        verifier.core = owner
        frozen = package("a" * 32, SOURCE.replace("price +", "price -"))
        worker = prepare(owner, "engineering_worker", Action.build("worker.fixed", {
            "fixture": "python_test", "arguments": {"package": frozen}}))
        execute(owner, worker)
        card = {
            **entry(), "category": "debugging", "task": "Python regex validation", "mistake": "Assuming validity",
            "approach": "Check a decisive input", "language": "python", "tools": [],
            "source": {"task_id": worker.task_id, "run_id": worker.id},
            "evidence": {"passed": 6, "total": 6, "sha256": [owner.artifacts.list(worker.id)[0].sha256]},
        }
        owner.memory.record("known", card, tested=True)
        with pytest.raises(Conflict):
            export_card(owner.store.path, "known")
        review = prepare(owner, "knowledge_review", Action.build("memory.verify", {"entry": "known", "sha256": digest(card)}))
        execute(owner, review)
        envelope = export_card(owner.store.path, "known")
        assert "approvals" not in canonical(envelope) and review.id not in canonical(envelope)
        with Core(dest) as replica:
            tampered = copy.deepcopy(envelope)
            tampered["bundle"]["card"]["observation"] = "Modified"
            tampered["sha256"] = digest(tampered["bundle"])
            with pytest.raises(Conflict):
                import_card(replica, owner.store.path, tampered)
            assert import_card(replica, owner.store.path, envelope) == "known"
            assert import_card(replica, owner.store.path, envelope) == "known"
            assert not replica.store.all("SELECT * FROM runs") and not replica.store.all("SELECT * FROM approvals")
            assert [r["status"] for r in replica.memory.history("known")] == ["tested", "verified"]
            action = replica.memory.prepare_review("regex validation", "b" * 64)
            validate_review_memory(replica.store.connection, action)
        with Core(dest) as replica:
            assert replica.memory.recall("regex validation")
            assert not source_execution_blocked(replica.store.connection, "known", pilot=True)
            unknown = prepare(owner, executor.name, Action.from_json(worker.action_json), key="second")
            unknown_approval = owner.approvals.request(unknown.id)
            owner.approvals.decide(unknown_approval.id, allow=True)
            executor.lose_receipt = True
            assert owner.runs.execute(unknown.id, approval_id=unknown_approval.id).status == "unknown"
            assert source_execution_blocked(replica.store.connection, "known", pilot=True)
            owner.memory.assess("known", "revoked", reviewer="Operator", reason="Scope withdrawn")
            assert replica.memory.recall("regex validation") == []
            with pytest.raises(Conflict):
                validate_review_memory(replica.store.connection, action)
            with pytest.raises(Conflict):
                import_card(replica, owner.store.path, envelope)
            assert sync_revocation(replica, "known") == "revoked"
            assert replica.memory.history("known")[-1]["status"] == "revoked"
