import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from ii_pro_core import (Action, ApprovalStatus as A, Core, MockExecutor, RunStatus as R,
                        TaskStatus as T, EffectStatus as E, Reconciler, ReconciliationReceipt)
from ii_pro_core.errors import CoreBusy, RecoveryRequired, SchemaError
from ii_pro_core.executors import SimulatedInterruption
from ii_pro_core.storage import MIGRATIONS, SCHEMA_VERSION, migration_checksum, utc_now
from conftest import planned


def fail_event(core, event):
    assert event in {"task.updated", "run.started", "run.succeeded"}
    with core.store.transaction() as conn:
        conn.execute(f"""CREATE TRIGGER fail_test_event BEFORE INSERT ON events
            WHEN NEW.kind='{event}' BEGIN SELECT RAISE(ABORT,'Injected failure'); END""")


def test_transition_and_journal_rollback_together(core):
    task = core.tasks.create("Before", idempotency_key="task")
    before_events = core.events()
    fail_event(core, "task.updated")
    with pytest.raises(sqlite3.IntegrityError):
        core.tasks.update(task.id, expected_version=1, title="After")
    assert core.tasks.get(task.id) == task and core.events() == before_events


def test_claim_failure_does_not_consume_approval_or_dispatch(core, mock):
    task = planned(core)
    run = core.runs.create(task.id, action=Action.build("mock.protected_echo"), idempotency_key="run")
    approval = core.approvals.request(run.id)
    core.approvals.decide(approval.id, allow=True)
    before_events = core.events()
    fail_event(core, "run.started")
    with pytest.raises(sqlite3.IntegrityError):
        core.runs.execute(run.id, approval_id=approval.id)
    assert core.runs.get(run.id).status == R.CREATED and mock.calls == 0
    assert core.approvals.get(approval.id).status == A.APPROVED and core.events() == before_events
    assert core.tasks.get(task.id).status == T.AWAITING_APPROVAL
    with core.store.transaction() as conn:
        conn.execute("DROP TRIGGER fail_test_event")
    assert core.runs.execute(run.id, approval_id=approval.id).status == R.SUCCEEDED


def test_completion_transaction_failure_is_recoverable_and_not_replayed(tmp_path):
    state, mock = tmp_path / "state", MockExecutor()
    with Core(state, executors=[mock]) as core:
        task = planned(core)
        run = core.runs.create(task.id, idempotency_key="run")
        fail_event(core, "run.succeeded")
        with pytest.raises(sqlite3.IntegrityError):
            core.runs.execute(run.id)
        assert mock.calls == 1 and core.runs.get(run.id).status == R.RUNNING
        assert core.tasks.get(task.id).status == T.RUNNING and core.artifacts.list(run.id) == []
        assert list((state / "workspace").rglob("result.json"))  # retained unregistered evidence
    with Core(state, executors=[mock]) as reopened:
        assert reopened.runs.get(run.id).status == R.UNKNOWN and mock.calls == 1
        with pytest.raises(RecoveryRequired):
            reopened.runs.execute(run.id)


def test_restart_preserves_task_artifacts_and_history(tmp_path):
    state = tmp_path / "state"
    with Core(state) as core:
        task = planned(core)
        run = core.runs.create(task.id, idempotency_key="run")
        core.runs.execute(run.id)
        artifact = core.artifacts.list(run.id)[0]
        before_events = core.events()
    with Core(state) as reopened:
        assert reopened.runs.get(run.id).status == R.SUCCEEDED
        assert reopened.tasks.get(task.id).status == T.VERIFYING
        assert reopened.artifacts.validate(artifact.id) and reopened.events() == before_events
        assert reopened.recovered_run_ids == []


def test_pending_approval_survives_restart_without_auto_grant(tmp_path):
    state = tmp_path / "state"
    with Core(state) as core:
        task = planned(core)
        run = core.runs.create(task.id, action=Action.build("mock.protected_echo"), idempotency_key="run")
        approval = core.approvals.request(run.id)
    with Core(state) as core:
        assert core.approvals.get(approval.id).status == A.PENDING
        assert core.runs.get(run.id).status == R.CREATED
        assert core.tasks.get(task.id).status == T.AWAITING_APPROVAL


def test_interrupted_attempt_requires_reconciliation_before_retry(tmp_path):
    class PureMockVerifier(Reconciler):
        name = "pure-mock"

        def reconcile(self, run):
            # This fixture's MockExecutor(interrupt=True) raises before producing
            # outputs and never performs business effects (source-audited here).
            return ReconciliationReceipt(run.task_id, run.id, run.action_hash,
                                         E.NOT_APPLIED, "fixture:pure-mock-interruption")

    state = tmp_path / "state"
    with Core(state, executors=[MockExecutor(interrupt=True)]) as core:
        task = planned(core)
        run = core.runs.create(task.id, idempotency_key="interrupted")
        with pytest.raises(SimulatedInterruption):
            core.runs.execute(run.id)
        assert core.runs.get(run.id).status == R.RUNNING
    mock = MockExecutor()
    with Core(state, executors=[mock], reconcilers=[PureMockVerifier()]) as core:
        assert core.recovered_run_ids == [run.id]
        assert core.runs.get(run.id).status == R.UNKNOWN
        assert core.tasks.get(task.id).status == T.FAILED
        with pytest.raises(RecoveryRequired):
            core.runs.execute(run.id)
        with pytest.raises(RecoveryRequired):
            core.runs.create(task.id, idempotency_key="retry")
        with pytest.raises(RecoveryRequired):
            core.tasks.transition(task.id, T.PLANNED)
        with pytest.raises(RecoveryRequired):
            core.runs.resolve_interrupted(run.id)
        core.runs.resolve_interrupted(run.id, confirm_stopped=True)
        assert core.runs.get(run.id).status == R.UNKNOWN
        with pytest.raises(RecoveryRequired):
            core.tasks.transition(task.id, T.PLANNED)
        core.runs.reconcile(run.id, verifier="pure-mock")
        core.tasks.transition(task.id, T.PLANNED)
        retry = core.runs.create(task.id, idempotency_key="retry")
        assert core.runs.execute(retry.id).status == R.SUCCEEDED and mock.calls == 1
        assert [r.status for r in core.runs.list(task.id)] == [R.FAILED, R.SUCCEEDED]
    with Core(state) as core:
        assert core.recovered_run_ids == []
        assert sum(e.kind == "run.interrupted" for e in core.events()) == 1


def test_real_process_exit_and_separate_process_reopen(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / "examples" / "demo.py"),
                             "--state-dir", str(tmp_path / "demo")], cwd=root,
                            capture_output=True, text=True, timeout=25, check=True)
    data = json.loads(result.stdout)
    assert data["normal"]["task"]["id"] == data["reopened_in_another_process"]["id"]
    assert data["reopened_in_another_process"]["status"] == T.SAVED
    assert data["artifact_valid_after_restart"] is True
    assert data["interruption"]["child_exit_code"] == 23
    assert data["interruption"]["status_after_recovery"] == R.UNKNOWN
    assert data["interruption"]["retry_status"] == R.SUCCEEDED


def test_live_owner_lock_does_not_recover_a_live_core(tmp_path):
    state = tmp_path / "state"
    with Core(state) as first:
        task = planned(first)
        with pytest.raises(CoreBusy):
            Core(state)
        assert first.tasks.get(task.id).status == T.PLANNED
    with Core(state) as second:
        assert second.tasks.get(task.id).status == T.PLANNED


def test_append_only_journal(core):
    task = planned(core)
    before = core.events()
    for sql in ("UPDATE events SET kind='forged'", "DELETE FROM events"):
        with pytest.raises(sqlite3.IntegrityError), core.store.transaction() as conn:
            conn.execute(sql)
    assert core.events() == before and core.tasks.get(task.id).status == T.PLANNED


def create_v1(state):
    state.mkdir()
    path = state / "core.sqlite3"
    with sqlite3.connect(path) as conn:
        for statement in MIGRATIONS[1]:
            conn.execute(statement)
        conn.execute("INSERT INTO migrations VALUES(?,?,?)", (1, migration_checksum(MIGRATIONS[1]), utc_now()))
        conn.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)",
                     ("a" * 32, "Legacy task", "", "mock", "draft", 1, utc_now(), utc_now(), "legacy", "old-hash"))
        conn.execute("PRAGMA user_version=1")
    return path


def test_schema_upgrade_preserves_data_and_creates_consistent_backup(tmp_path):
    state = tmp_path / "state"
    create_v1(state)
    with Core(state) as core:
        assert core.tasks.get("a" * 32).title == "Legacy task"
        assert core.store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        backup = list((state / "backups").glob(f"pre-v{SCHEMA_VERSION}-*.sqlite3"))
        assert len(backup) == 1
        with sqlite3.connect(backup[0]) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
            assert conn.execute("SELECT title FROM tasks").fetchone()[0] == "Legacy task"
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_failed_migration_rolls_back_schema_and_version(tmp_path, monkeypatch):
    state = tmp_path / "state"
    path = create_v1(state)
    with monkeypatch.context() as patch:
        patch.setitem(MIGRATIONS, 2, ("CREATE TABLE partially_created(value TEXT)", "INVALID SQL"))
        with pytest.raises(sqlite3.OperationalError):
            Core(state)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT title FROM tasks").fetchone()[0] == "Legacy task"
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='partially_created'").fetchone() is None
        assert conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 1
    with Core(state) as core:
        assert core.tasks.get("a" * 32).title == "Legacy task"


def test_future_schema_is_refused_without_rewriting_database(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    path = state / "core.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=99")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(SchemaError):
        Core(state)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_changed_migration_checksum_is_refused(tmp_path):
    state = tmp_path / "state"
    path = create_v1(state)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE migrations SET checksum='invalid'")
    before = path.read_bytes()
    with pytest.raises(SchemaError):
        Core(state)
    assert path.read_bytes() == before


def test_unknown_unversioned_database_is_not_adopted(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    with sqlite3.connect(state / "core.sqlite3") as conn:
        conn.execute("CREATE TABLE user_data(value TEXT)")
    with pytest.raises(SchemaError):
        Core(state)


def test_backup_can_be_restored_to_separate_copy(core, tmp_path):
    task = planned(core)
    backup = core.backup()
    restored = tmp_path / "restored"
    restored.mkdir()
    (restored / "core.sqlite3").write_bytes(backup.read_bytes())
    with Core(restored) as copy:
        assert copy.tasks.get(task.id) == core.tasks.get(task.id)
        assert copy.events() == core.events()
    assert core.tasks.get(task.id).status == T.PLANNED
