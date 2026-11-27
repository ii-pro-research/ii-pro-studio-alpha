"""Regression coverage for effects that cannot be inferred from Python errors.

All authoritative evidence here is a local fixture counter, never an external
service. Production integrations must supply their own trusted domain verifier.
"""
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event

import pytest

from ii_pro_core import (Action, ApprovalStatus as A, Core, EffectStatus as E,
                        MockExecutor, Reconciler, ReconciliationReceipt,
                        RunStatus as R, TaskStatus as T)
from ii_pro_core.errors import ApprovalRequired, Conflict, RecoveryRequired, ValidationError
from ii_pro_core.executors import ExecutionResult, ProducedArtifact
from ii_pro_core.storage import MIGRATIONS, SCHEMA_VERSION, migration_checksum, utc_now
from conftest import planned


class CounterExecutor(MockExecutor):
    revision = "counter-fixture-v1"

    def approval_configuration(self):
        return {**super().approval_configuration(), "counter": str(self.counter),
                "apply": self.apply, "invalid_output": self.invalid_output}

    def __init__(self, counter, *, apply=True, invalid_output=False):
        super().__init__()
        self.counter, self.apply, self.invalid_output = counter, apply, invalid_output

    def execute(self, request):
        self.calls += 1
        config = request.operation_spec.executor_configuration if request.operation_spec else self.approval_configuration()
        counter = Path(config["counter"])
        if config["apply"]:
            counter.write_text(str(int(counter.read_text()) + 1))
        if config["invalid_output"]:
            return ExecutionResult((ProducedArtifact("../invalid", b"invalid"),))
        raise RuntimeError("Response unavailable after dispatch")


class CounterVerifier(Reconciler):
    name = "counter"

    def __init__(self, counter):
        self.counter = counter

    def reconcile(self, run):
        # Each fixture has exactly one mutator and no surviving child. The
        # counter is authoritative only under these explicit test assumptions.
        value = int(self.counter.read_text())
        return ReconciliationReceipt(run.task_id, run.id, run.action_hash,
                                     E.APPLIED if value else E.NOT_APPLIED,
                                     f"fixture:counter={value};run={run.id}")


def setup_failure(tmp_path, *, apply=True, invalid_output=False, protected=False):
    counter = tmp_path / "counter.txt"
    counter.write_text("0")
    executor = CounterExecutor(counter, apply=apply, invalid_output=invalid_output)
    core = Core(tmp_path / "state", executors=[executor], reconcilers=[CounterVerifier(counter)])
    task = planned(core)
    run = core.runs.create(task.id, idempotency_key="first",
                           action=Action.build("mock.protected_echo" if protected else "mock.echo"))
    approval_id = None
    if protected:
        approval = core.approvals.request(run.id)
        core.approvals.decide(approval.id, allow=True)
        approval_id = approval.id
    result = core.runs.execute(run.id, approval_id=approval_id)
    return core, task, result, counter, executor, approval_id


def assert_retry_blocked(core, task, run):
    with pytest.raises(RecoveryRequired):
        core.runs.execute(run.id)
    with pytest.raises(RecoveryRequired):
        core.runs.create(task.id, idempotency_key="new-key")
    with pytest.raises(RecoveryRequired):
        core.tasks.transition(task.id, T.PLANNED)


def test_effect_then_exception_is_unknown_and_never_replayed(tmp_path):
    core, task, run, counter, executor, _ = setup_failure(tmp_path)
    with core:
        assert counter.read_text() == "1" and executor.calls == 1
        assert run.status == R.UNKNOWN and run.effect_status == E.UNKNOWN
        assert run.execution_stopped == 0
        assert (run.error_code, run.error_type) == ("executor_failed", "RuntimeError")
        assert run.started_at and run.finished_at
        assert_retry_blocked(core, task, run)
        assert core.runs.create(task.id, idempotency_key="first") == run
        assert core.runs.get(run.id) == run
        assert counter.read_text() == "1" and executor.calls == 1
        assert [e.kind for e in core.events() if e.run_id == run.id] == [
            "run.created", "run.started", "run.uncertain"]
        history = core.events()
    for _ in range(2):
        with Core(tmp_path / "state", executors=[executor]) as reopened:
            assert reopened.runs.get(run.id) == run
            assert reopened.recovered_run_ids == [] and reopened.events() == history
            assert_retry_blocked(reopened, task, run)
            assert counter.read_text() == "1" and executor.calls == 1


def test_failure_before_execute_is_safely_retryable(tmp_path):
    class NotReady(MockExecutor):
        def execute(self, request):
            pytest.fail("execute must not be called after failed prepare")

    with Core(tmp_path / "state", executors=[NotReady(failures=1)]) as core:
        task = planned(core)
        run = core.runs.execute(core.runs.create(task.id, idempotency_key="first").id)
        assert run.status == R.FAILED and run.effect_status == E.NOT_STARTED
        assert run.started_at is None and run.execution_stopped == 1
        core.tasks.transition(task.id, T.PLANNED)
        retry = core.runs.create(task.id, idempotency_key="retry")
        assert retry.attempt == 2 and core.runs.get(run.id) == run


def test_artifact_error_after_effect_is_unknown(tmp_path):
    core, task, run, counter, _, _ = setup_failure(tmp_path, invalid_output=True)
    with core:
        assert counter.read_text() == "1"
        assert run.status == R.UNKNOWN and run.error_code == "artifact_failed"
        assert core.artifacts.list(run.id) == []
        assert_retry_blocked(core, task, run)


def test_confirming_stop_neither_resolves_effect_nor_permits_retry(tmp_path):
    core, task, run, counter, _, _ = setup_failure(tmp_path)
    with core:
        with pytest.raises(RecoveryRequired):
            core.runs.reconcile(run.id, verifier="counter")
        stopped = core.runs.resolve_interrupted(run.id, confirm_stopped=True)
        assert stopped.status == R.UNKNOWN and stopped.effect_status == E.UNKNOWN
        assert stopped.execution_stopped == 1
        assert_retry_blocked(core, task, stopped)
        with pytest.raises(RecoveryRequired):
            core.runs.reconcile(run.id, verifier="unregistered")
        assert not any(e.kind == "run.reconciled" for e in core.events())
        assert counter.read_text() == "1"


def test_confirmed_applied_effect_retains_error_and_blocks_new_attempt(tmp_path):
    core, task, run, counter, _, _ = setup_failure(tmp_path)
    with core:
        core.runs.resolve_interrupted(run.id, confirm_stopped=True)
        resolved = core.runs.reconcile(run.id, verifier="counter")
        assert resolved.status == R.FAILED and resolved.effect_status == E.APPLIED
        assert (resolved.error_code, resolved.error_type, resolved.started_at) == (
            run.error_code, run.error_type, run.started_at)
        receipt = json.loads(resolved.reconciliation_json)
        assert receipt["run_id"] == run.id and receipt["action_hash"] == run.action_hash
        assert core.runs.execute(run.id) == resolved
        with pytest.raises(RecoveryRequired):
            core.tasks.transition(task.id, T.PLANNED)
        with pytest.raises(RecoveryRequired):
            core.runs.create(task.id, idempotency_key="retry")
        assert counter.read_text() == "1" and len(core.runs.list(task.id)) == 1
    with Core(tmp_path / "state") as core:
        assert core.runs.get(run.id) == resolved
        with pytest.raises(RecoveryRequired):
            core.tasks.transition(task.id, T.PLANNED)


def test_verified_absence_allows_new_attempt_with_complete_history(tmp_path):
    core, task, run, counter, _, approval_id = setup_failure(tmp_path, apply=False, protected=True)
    with core:
        before = core.events()
        core.runs.resolve_interrupted(run.id, confirm_stopped=True)
        resolved = core.runs.reconcile(run.id, verifier="counter")
        assert resolved.effect_status == E.NOT_APPLIED and resolved.status == R.FAILED
        assert resolved.error_code == run.error_code and resolved.error_type == run.error_type
        assert core.events()[:len(before)] == before
        core.tasks.transition(task.id, T.PLANNED)
        retry = core.runs.create(task.id, action=Action.from_json(run.action_json), idempotency_key="retry")
        assert retry.id != run.id and retry.attempt == 2
        with pytest.raises(ApprovalRequired):
            core.runs.execute(retry.id, approval_id=approval_id)
        assert core.approvals.get(approval_id).status == A.CONSUMED
    successful = MockExecutor()
    with Core(tmp_path / "state", executors=[successful]) as reopened:
        approval = reopened.approvals.request(retry.id)
        reopened.approvals.decide(approval.id, allow=True)
        assert reopened.runs.execute(retry.id, approval_id=approval.id).status == R.SUCCEEDED
        assert reopened.runs.get(run.id) == resolved
        assert [r.attempt for r in reopened.runs.list(task.id)] == [1, 2]
        assert successful.calls == 1 and counter.read_text() == "0"


@pytest.mark.parametrize("field,value", [
    ("run_id", "other"), ("task_id", "other"), ("action_hash", "other"),
    ("outcome", E.NOT_STARTED), ("outcome", "invented"), ("outcome", None),
    ("evidence_ref", ""), ("evidence_ref", "password=private-fixture"),
])
def test_invalid_receipt_cannot_unlock_retry(tmp_path, field, value):
    core, task, run, _, _, _ = setup_failure(tmp_path)
    with core:
        original = core.runs.resolve_interrupted(run.id, confirm_stopped=True)
        before = core.events()
        verifier = core.runs.reconcilers["counter"]
        valid = verifier.reconcile(original)
        verifier.reconcile = lambda _: replace(valid, **{field: value})
        with pytest.raises(ValidationError):
            core.runs.reconcile(run.id, verifier="counter")
        assert core.runs.get(run.id) == original and core.events() == before
        assert_retry_blocked(core, task, original)


def test_verifier_error_leaves_state_unresolved(tmp_path):
    core, task, run, _, _, _ = setup_failure(tmp_path)
    with core:
        original = core.runs.resolve_interrupted(run.id, confirm_stopped=True)
        def unavailable(_):
            raise OSError("Evidence unavailable")
        core.runs.reconcilers["counter"].reconcile = unavailable
        with pytest.raises(OSError):
            core.runs.reconcile(run.id, verifier="counter")
        assert core.runs.get(run.id) == original
        assert_retry_blocked(core, task, original)


def test_unresolved_receipts_are_preserved_in_journal(tmp_path):
    core, task, run, _, _, _ = setup_failure(tmp_path)
    with core:
        core.runs.resolve_interrupted(run.id, confirm_stopped=True)
        verifier = core.runs.reconcilers["counter"]
        for index, outcome in enumerate((E.PARTIAL, E.UNKNOWN, E.NOT_APPLIED)):
            verifier.reconcile = lambda r: ReconciliationReceipt(
                r.task_id, r.id, r.action_hash, outcome, f"fixture:review-{index}")
            current = core.runs.reconcile(run.id, verifier="counter")
            assert current.effect_status == outcome
            if outcome != E.NOT_APPLIED:
                assert_retry_blocked(core, task, current)
        events = [e for e in core.events() if e.kind == "run.reconciled"]
        assert [e.payload["receipt"]["evidence_ref"] for e in events] == [
            "fixture:review-0", "fixture:review-1", "fixture:review-2"]
        core.tasks.transition(task.id, T.PLANNED)


@pytest.mark.parametrize("event", ["run.uncertain", "run.reconciled"])
def test_outcome_and_journal_rollback_together(tmp_path, event):
    counter = tmp_path / "counter.txt"
    counter.write_text("0")
    executor = CounterExecutor(counter)
    with Core(tmp_path / "state", executors=[executor], reconcilers=[CounterVerifier(counter)]) as core:
        task = planned(core)
        run = core.runs.create(task.id, idempotency_key="first")
        if event == "run.reconciled":
            core.runs.execute(run.id)
            core.runs.resolve_interrupted(run.id, confirm_stopped=True)
        before = core.runs.get(run.id)
        with core.store.transaction() as conn:
            # event is one of two static fixture constants, never outside input.
            conn.execute(f"""CREATE TRIGGER fault BEFORE INSERT ON events
                WHEN NEW.kind='{event}' BEGIN SELECT RAISE(ABORT,'fault'); END""")
        with pytest.raises(sqlite3.IntegrityError):
            if event == "run.uncertain":
                core.runs.execute(run.id)
            else:
                core.runs.reconcile(run.id, verifier="counter")
        current = core.runs.get(run.id)
        if event == "run.reconciled":
            assert current == before
        else:
            assert current.status == R.RUNNING and current.effect_status == E.UNKNOWN
        assert not any(e.kind == event for e in core.events())
    with Core(tmp_path / "state", executors=[executor]) as reopened:
        current = reopened.runs.get(run.id)
        assert current.status == R.UNKNOWN and current.effect_status == E.UNKNOWN
        assert counter.read_text() == "1" and executor.calls == 1
        assert_retry_blocked(reopened, task, current)


def test_concurrent_verification_commits_only_one_receipt(tmp_path):
    core, _, run, _, _, _ = setup_failure(tmp_path, apply=False)
    barrier = Barrier(2)
    with core:
        core.runs.resolve_interrupted(run.id, confirm_stopped=True)
        verifier = core.runs.reconcilers["counter"]
        real_verify = verifier.reconcile
        def verify(snapshot):
            receipt = real_verify(snapshot)
            barrier.wait(timeout=5)
            return receipt
        verifier.reconcile = verify
        def attempt():
            try:
                return core.runs.reconcile(run.id, verifier="counter").effect_status
            except Conflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        assert sorted(results) == sorted([E.NOT_APPLIED, "conflict"])
        assert sum(e.kind == "run.reconciled" for e in core.events()) == 1


def test_live_execution_cannot_be_reconciled(tmp_path):
    started, release = Event(), Event()
    class Blocking(MockExecutor):
        def execute(self, request):
            started.set()
            assert release.wait(5)
            return super().execute(request)
    with Core(tmp_path / "state", executors=[Blocking()]) as core:
        task = planned(core)
        run = core.runs.create(task.id, idempotency_key="run")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(core.runs.execute, run.id)
            try:
                assert started.wait(5)
                with pytest.raises(Conflict):
                    core.runs.resolve_interrupted(run.id, confirm_stopped=True)
                assert core.runs.get(run.id).status == R.RUNNING
            finally:
                release.set()
            assert future.result(timeout=5).status == R.SUCCEEDED


def test_real_process_crash_after_effect_recovers_without_replay(tmp_path):
    state, counter = tmp_path / "state", tmp_path / "counter.txt"
    counter.write_text("0")
    with Core(state) as core:
        task = planned(core)
        run = core.runs.create(task.id, idempotency_key="first")
    program = """
import os, sys
from pathlib import Path
from ii_pro_core import Core, MockExecutor
class Crash(MockExecutor):
    def execute(self, request):
        path = Path(sys.argv[2])
        with path.open('w') as output:
            output.write('1')
            output.flush()
            os.fsync(output.fileno())
        os._exit(31)
with Core(sys.argv[1], executors=[Crash()]) as core:
    core.runs.execute(sys.argv[3])
"""
    child = subprocess.run([sys.executable, "-c", program, str(state), str(counter), run.id],
                           cwd=Path(__file__).resolve().parents[1], timeout=15,
                           capture_output=True, text=True)
    assert child.returncode == 31, child.stderr
    for index in range(2):
        executor = MockExecutor()
        with Core(state, executors=[executor], reconcilers=[CounterVerifier(counter)]) as core:
            assert core.recovered_run_ids == ([run.id] if index == 0 else [])
            unknown = core.runs.get(run.id)
            assert unknown.status == R.UNKNOWN and unknown.effect_status == E.UNKNOWN
            assert_retry_blocked(core, task, unknown)
            core.runs.resolve_interrupted(run.id, confirm_stopped=True)
            assert_retry_blocked(core, task, core.runs.get(run.id))
            assert executor.calls == 0 and counter.read_text() == "1"
            assert sum(e.kind == "run.interrupted" for e in core.events()) == 1


def legacy_v2(state, *, status="failed", pending=False):
    state.mkdir()
    path = state / "core.sqlite3"
    action = Action.build()
    with sqlite3.connect(path) as conn:
        for version in (1, 2):
            for statement in MIGRATIONS[version]:
                conn.execute(statement)
            conn.execute("INSERT INTO migrations VALUES(?,?,?)",
                         (version, migration_checksum(MIGRATIONS[version]), utc_now()))
        conn.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)", (
            "a" * 32, "Legacy", "", "mock", "planned" if pending else "failed", 1,
            utc_now(), utc_now(), "task", "legacy"))
        def insert(run_id, attempt, run_status):
            conn.execute("""INSERT INTO runs(id,task_id,attempt,executor,status,idempotency_key,
                action_json,action_hash,title,description,error_code,error_type,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                run_id, "a" * 32, attempt, "mock", run_status, f"attempt-{attempt}",
                action.json, action.hash, "Legacy", "", "executor_failed", "RuntimeError",
                utc_now(), utc_now()))
        insert("b" * 32, 1, status)
        if pending:
            insert("c" * 32, 2, "created")
        conn.execute("PRAGMA user_version=2")
    return path


@pytest.mark.parametrize("status", ["failed", "succeeded", "unknown", "running", "created", "cancelled"])
def test_v2_migration_is_conservative_and_keeps_backup(tmp_path, status):
    state = tmp_path / "state"
    path = legacy_v2(state, status=status)
    # A running run must have a running task, as produced by the old API.
    if status == "running":
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE tasks SET status='running'")
    with Core(state) as core:
        run = core.runs.get("b" * 32)
        errors = ("execution_interrupted", "Interrupted") if status == "running" else ("executor_failed", "RuntimeError")
        assert (run.error_code, run.error_type) == errors and run.attempt == 1
        expected = E.NOT_STARTED if status in {"created", "cancelled"} else E.UNKNOWN
        assert run.effect_status == expected
        assert core.store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert core.store.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert core.store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        backups = list((state / "backups").glob(f"pre-v{SCHEMA_VERSION}-*.sqlite3"))
        assert len(backups) == 1
        with sqlite3.connect(backups[0]) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
            assert conn.execute("SELECT status FROM runs").fetchone()[0] == status
            assert "effect_status" not in [r[1] for r in conn.execute("PRAGMA table_info(runs)")]
        if status in {"failed", "succeeded", "unknown", "running"}:
            with pytest.raises(RecoveryRequired):
                core.tasks.transition("a" * 32, T.PLANNED)
    with Core(state) as core:
        assert len(list((state / "backups").glob(f"pre-v{SCHEMA_VERSION}-*.sqlite3"))) == 1


def test_migrated_pending_retry_cannot_bypass_old_uncertainty(tmp_path):
    state = tmp_path / "state"
    legacy_v2(state, pending=True)
    mock = MockExecutor()
    with Core(state, executors=[mock]) as core:
        with pytest.raises(RecoveryRequired):
            core.runs.execute("c" * 32)
        assert mock.calls == 0 and core.runs.get("c" * 32).status == R.CREATED


def test_failed_v3_migration_rolls_back_columns_and_ledger(tmp_path, monkeypatch):
    state = tmp_path / "state"
    path = legacy_v2(state)
    with monkeypatch.context() as patch:
        patch.setitem(MIGRATIONS, 3, (*MIGRATIONS[3], "INVALID SQL"))
        with pytest.raises(sqlite3.OperationalError):
            Core(state)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 2
        assert "effect_status" not in [r[1] for r in conn.execute("PRAGMA table_info(runs)")]
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    with Core(state) as core:
        assert core.runs.get("b" * 32).effect_status == E.UNKNOWN


def test_legacy_failed_requires_real_verification_before_retry(tmp_path):
    state, counter = tmp_path / "state", tmp_path / "counter.txt"
    legacy_v2(state)
    counter.write_text("0")
    with Core(state, reconcilers=[CounterVerifier(counter)]) as core:
        run = core.runs.get("b" * 32)
        core.runs.resolve_interrupted(run.id, confirm_stopped=True)
        with pytest.raises(RecoveryRequired):
            core.tasks.transition(run.task_id, T.PLANNED)
        verified = core.runs.reconcile(run.id, verifier="counter")
        assert verified.effect_status == E.NOT_APPLIED and verified.error_code == "executor_failed"
        core.tasks.transition(run.task_id, T.PLANNED)
        retry = core.runs.create(run.task_id, idempotency_key="retry")
        assert retry.attempt == 2
        assert core.runs.execute(retry.id).status == R.SUCCEEDED


def test_legacy_success_cannot_be_verified_with_absent_effect(tmp_path):
    state, counter = tmp_path / "state", tmp_path / "counter.txt"
    path = legacy_v2(state, status="succeeded")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE tasks SET status='verifying'")
    counter.write_text("0")
    with Core(state, reconcilers=[CounterVerifier(counter)]) as core:
        run = core.runs.get("b" * 32)
        with pytest.raises(RecoveryRequired):
            core.tasks.transition(run.task_id, T.VERIFIED)
        core.runs.resolve_interrupted(run.id, confirm_stopped=True)
        core.runs.reconcile(run.id, verifier="counter")
        with pytest.raises(RecoveryRequired):
            core.tasks.transition(run.task_id, T.VERIFIED)


def test_reconciler_must_be_explicitly_registered_by_trusted_application(tmp_path):
    verifier = CounterVerifier(tmp_path / "unused")
    with pytest.raises(ValidationError):
        Core(tmp_path / "state", reconcilers=[verifier, verifier])
    verifier.name = "not.a.valid.name"
    with pytest.raises(ValidationError):
        Core(tmp_path / "state", reconcilers=[verifier])
    assert not (tmp_path / "state").exists()
