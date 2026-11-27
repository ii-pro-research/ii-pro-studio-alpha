"""H03 regressions. All executors are local trusted fixtures; no model calls."""
import hashlib
import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Barrier, Event
from types import MethodType

import pytest

from ii_pro_core import Action, ApprovalStatus as A, Core, ExecutionPolicy, MockExecutor, RunStatus as R
from ii_pro_core.errors import ApprovalRequired, Conflict, CoreBusy, ValidationError
from ii_pro_core.storage import MIGRATIONS, SCHEMA_VERSION, migration_checksum, utc_now
from test_approvals import protected


def granted(core):
    task, run = protected(core)
    approval = core.approvals.request(run.id)
    core.approvals.decide(approval.id, allow=True)
    return task, run, approval


def test_reviewable_immutable_snapshot_binds_every_operation_dimension(core):
    task, run, approval = granted(core)
    body = core.approvals.describe(approval.id)
    assert (body["task_id"], body["run_id"], body["attempt"]) == (task.id, run.id, 1)
    assert body["action"] == json.loads(run.action_json)
    assert body["action_hash"] == run.action_hash
    assert body["title"] == run.title and body["description"] == run.description
    assert body["executor"]["revision"] == "mock-v1"
    assert body["executor"]["configuration"] == {"failures": 0, "interrupt": False}
    assert len(body["executor"]["code_sha256"]) == 64
    assert body["policy"]["revision"] == "core-policy-v1"
    assert len(body["policy"]["implementation_sha256"]) == 64
    assert hashlib.sha256(approval.operation_json.encode()).hexdigest() == approval.spec_digest
    body["executor"]["configuration"]["interrupt"] = True
    assert core.approvals.describe(approval.id)["executor"]["configuration"]["interrupt"] is False
    with pytest.raises(FrozenInstanceError):
        approval.spec_digest = "bad"
    with pytest.raises(FrozenInstanceError):
        core.approvals.policy.revision = "changed"
    with pytest.raises(TypeError):
        core.runs.executors["mock"] = MockExecutor()


@pytest.mark.parametrize("field,value", [("title", "Changed operation"), ("description", "Changed input semantics"),
                                        ("attempt", 2), ("idempotency_key", "different-operation-key")])
def test_changed_run_snapshot_never_uses_old_grant(core, mock, field, value):
    _, run, approval = granted(core)
    with core.store.transaction() as conn:
        conn.execute(f"UPDATE runs SET {field}=? WHERE id=?", (value, run.id))
    with pytest.raises(ApprovalRequired, match="scope_mismatch"):
        core.runs.execute(run.id, approval_id=approval.id)
    assert mock.calls == 0 and core.approvals.get(approval.id).status == A.APPROVED
    assert core.runs.get(run.id).status == R.CREATED


def test_changed_action_cannot_drop_approval_requirement(core, mock):
    _, run, approval = granted(core)
    action = Action.build("mock.echo", {"text": "Changed unprotected action"})
    with core.store.transaction() as conn:
        conn.execute("UPDATE runs SET action_json=?,action_hash=? WHERE id=?", (action.json, action.hash, run.id))
    with pytest.raises(ApprovalRequired):
        core.runs.execute(run.id)
    with pytest.raises(ApprovalRequired):
        core.runs.execute(run.id, approval_id=approval.id)
    fresh = core.approvals.request(run.id)
    assert fresh.status == A.PENDING and fresh.spec_digest != approval.spec_digest
    assert core.approvals.get(approval.id).status == A.INVALIDATED
    core.approvals.decide(fresh.id, allow=True)
    assert core.runs.execute(run.id, approval_id=fresh.id).status == R.SUCCEEDED and mock.calls == 1


@pytest.mark.parametrize("field,value", [("revision", "mock-v2"), ("interrupt", True), ("failures", 1), ("supports_cancellation", True)])
def test_changed_executor_revision_configuration_or_capability_blocks(core, mock, field, value):
    _, run, approval = granted(core)
    setattr(mock, field, value)
    with pytest.raises(ApprovalRequired, match="scope_mismatch"):
        core.runs.execute(run.id, approval_id=approval.id)
    assert mock.calls == 0
    fresh = core.approvals.request(run.id)
    assert fresh.id != approval.id and fresh.spec_digest != approval.spec_digest and fresh.status == A.PENDING
    assert core.approvals.get(approval.id).status == A.INVALIDATED


def test_telemetry_does_not_change_operation_scope(core, mock):
    _, run, approval = granted(core)
    mock.calls = 30
    assert core.approvals.request(run.id).id == approval.id
    assert core.runs.execute(run.id, approval_id=approval.id).status == R.SUCCEEDED
    assert mock.calls == 31


def test_changed_callable_is_detected_even_with_same_class_and_revision(core, mock):
    _, run, approval = granted(core)
    def changed(self, request):
        raise AssertionError("Changed code must never execute")
    mock.execute = MethodType(changed, mock)
    with pytest.raises(ApprovalRequired, match="scope_mismatch"):
        core.runs.execute(run.id, approval_id=approval.id)
    assert mock.calls == 0


def test_fresh_request_is_pending_and_invalidated_old_grant_never_revives(core, mock):
    _, run, approval = granted(core)
    mock.revision = "mock-v2"
    fresh = core.approvals.request(run.id)
    assert fresh.status == A.PENDING
    with pytest.raises(ApprovalRequired):
        core.runs.execute(run.id, approval_id=fresh.id)
    mock.revision = "mock-v1"
    with pytest.raises(Conflict):
        core.approvals.decide(approval.id, allow=True)
    with pytest.raises(ApprovalRequired):
        core.runs.execute(run.id, approval_id=approval.id)
    third = core.approvals.request(run.id)
    assert third.status == A.PENDING and third.id not in {approval.id, fresh.id}
    assert third.spec_digest == approval.spec_digest  # same operation, NEW consent
    assert mock.calls == 0


def test_changed_scope_cannot_be_approved_by_stale_decision(core, mock):
    _, run = protected(core)
    pending = core.approvals.request(run.id)
    mock.revision = "new-revision"
    with pytest.raises(Conflict, match="Operation changed"):
        core.approvals.decide(pending.id, allow=True)
    assert core.approvals.get(pending.id).status == A.INVALIDATED
    assert core.approvals.request(run.id).status == A.PENDING and mock.calls == 0


@pytest.mark.parametrize("policy", [ExecutionPolicy(revision="policy-v2"),
                                   ExecutionPolicy(permissions_revision="permissions-v2"),
                                   ExecutionPolicy(approval_actions=("mock.echo", "mock.protected_echo"))])
def test_policy_changes_require_new_consent_after_restart(tmp_path, policy):
    state = tmp_path / "state"
    with Core(state) as first:
        _, run, approval = granted(first)
    mock = MockExecutor()
    with Core(state, executors=[mock], policy=policy) as second:
        with pytest.raises(ApprovalRequired, match="scope_mismatch"):
            second.runs.execute(run.id, approval_id=approval.id)
        fresh = second.approvals.request(run.id)
        assert fresh.status == A.PENDING and fresh.spec_digest != approval.spec_digest
        second.approvals.decide(fresh.id, allow=True)
        assert second.runs.execute(run.id, approval_id=fresh.id).status == R.SUCCEEDED
        assert mock.calls == 1


def test_relaxed_policy_cannot_ungate_a_previously_gated_run(tmp_path):
    state = tmp_path / "state"
    stricter = ExecutionPolicy(approval_actions=("mock.echo", "mock.protected_echo"))
    from conftest import planned
    with Core(state, policy=stricter) as first:
        task = planned(first)
        run = first.runs.create(task.id, idempotency_key="run")
        approval = first.approvals.request(run.id)
        first.approvals.decide(approval.id, allow=True)
    with Core(state) as second:
        with pytest.raises(ApprovalRequired):
            second.runs.execute(run.id)
        with pytest.raises(ApprovalRequired):
            second.runs.execute(run.id, approval_id=approval.id)
        assert second.approvals.request(run.id).status == A.PENDING


def test_unchanged_grant_and_snapshot_survive_reopen(tmp_path):
    state = tmp_path / "state"
    with Core(state) as core:
        _, run, approval = granted(core)
        saved = core.approvals.get(approval.id)
    with Core(state) as core:
        assert core.approvals.get(approval.id) == saved
        assert core.runs.execute(run.id, approval_id=approval.id).status == R.SUCCEEDED
        assert core.approvals.get(approval.id).status == A.CONSUMED


@pytest.mark.parametrize("approved", [False, True])
def test_revoke_is_terminal_idempotent_and_new_request_needs_decision(core, mock, approved):
    _, run = protected(core)
    old = core.approvals.request(run.id)
    if approved:
        core.approvals.decide(old.id, allow=True)
    revoked = core.approvals.revoke(old.id)
    assert revoked.status == A.REVOKED and revoked.revoked_at
    assert core.approvals.revoke(old.id) == revoked
    with pytest.raises(Conflict):
        core.approvals.decide(old.id, allow=True)
    with pytest.raises(ApprovalRequired):
        core.runs.execute(run.id, approval_id=old.id)
    fresh = core.approvals.request(run.id)
    assert fresh.id != old.id and fresh.status == A.PENDING and mock.calls == 0
    core.approvals.decide(fresh.id, allow=True)
    assert core.runs.execute(run.id, approval_id=fresh.id).status == R.SUCCEEDED


def test_consumed_permission_cannot_be_revoked_as_if_effect_were_undone(core):
    _, run, approval = granted(core)
    core.runs.execute(run.id, approval_id=approval.id)
    with pytest.raises(Conflict, match="undo"):
        core.approvals.revoke(approval.id)
    assert core.approvals.get(approval.id).status == A.CONSUMED


def test_revocation_and_rejection_remain_terminal_after_restart(tmp_path):
    state = tmp_path / "state"
    with Core(state) as core:
        _, run = protected(core)
        rejected = core.approvals.request(run.id)
        core.approvals.decide(rejected.id, allow=False)
        revoked = core.approvals.request(run.id)
        core.approvals.decide(revoked.id, allow=True)
        core.approvals.revoke(revoked.id)
    with Core(state) as core:
        assert core.approvals.get(rejected.id).status == A.REJECTED
        assert core.approvals.get(revoked.id).status == A.REVOKED
        for old in (rejected, revoked):
            with pytest.raises(Conflict):
                core.approvals.decide(old.id, allow=True)
            with pytest.raises(ApprovalRequired):
                core.runs.execute(run.id, approval_id=old.id)
        assert core.approvals.request(run.id).status == A.PENDING


def test_revocation_after_claim_cannot_pretend_to_stop_live_executor(tmp_path):
    started, release = Event(), Event()
    class Blocking(MockExecutor):
        revision = "blocking-fixture-v1"

        def approval_configuration(self):
            return super().approval_configuration()

        def execute(self, request):
            started.set()
            assert release.wait(5)
            return super().execute(request)

    mock = Blocking()
    with Core(tmp_path / "state", executors=[mock]) as core:
        _, run, approval = granted(core)
        with ThreadPoolExecutor(max_workers=1) as pool:
            executing = pool.submit(core.runs.execute, run.id, approval_id=approval.id)
            try:
                assert started.wait(5)
                with pytest.raises(Conflict):
                    core.approvals.revoke(approval.id)
                assert core.runs.get(run.id).status == R.RUNNING
                assert core.runs.get(run.id).execution_stopped == 0
                with pytest.raises(CoreBusy):
                    core.close(timeout=0)
            finally:
                release.set()
            assert executing.result(timeout=5).status == R.SUCCEEDED


def test_expired_request_can_be_replaced_without_reviving_old(core, mock, monkeypatch):
    import ii_pro_core.approvals as module
    _, run, approval = granted(core)
    with monkeypatch.context() as patch:
        patch.setattr(module, "utc_now", lambda: "2099-01-01T00:00:00.000000+00:00")
        with pytest.raises(ApprovalRequired):
            core.runs.execute(run.id, approval_id=approval.id)
    assert core.approvals.get(approval.id).status == A.EXPIRED
    fresh = core.approvals.request(run.id)
    assert fresh.status == A.PENDING and fresh.id != approval.id and mock.calls == 0


def test_expiry_during_preparation_rolls_back_consumption_and_blocks_call(tmp_path, monkeypatch):
    import ii_pro_core.approvals as module
    class SlowPreparation(MockExecutor):
        revision = "expiry-fixture-v1"

        def approval_configuration(self):
            return super().approval_configuration()

        def prepare(self, request):
            monkeypatch.setattr(module, "utc_now", lambda: "2099-01-01T00:00:00.000000+00:00")

    mock = SlowPreparation()
    with Core(tmp_path / "state", executors=[mock]) as core:
        _, run, approval = granted(core)
        with pytest.raises(ApprovalRequired, match="expired_during_preparation"):
            core.runs.execute(run.id, approval_id=approval.id)
        assert core.approvals.get(approval.id).status == A.EXPIRED
        assert core.runs.get(run.id).status == R.CREATED and mock.calls == 0
        assert not any(e.kind == "approval.consumed" for e in core.events())


def test_expiry_after_claim_still_blocks_actual_dispatch(core, mock, monkeypatch):
    import ii_pro_core.approvals as module
    _, run, approval = granted(core)
    event = core.store.event
    def expire_after_claim(conn, kind, **kwargs):
        event(conn, kind, **kwargs)
        if kind == "run.started":
            monkeypatch.setattr(module, "utc_now", lambda: "2099-01-01T00:00:00.000000+00:00")
    monkeypatch.setattr(core.store, "event", expire_after_claim)
    result = core.runs.execute(run.id, approval_id=approval.id)
    assert result.status == R.UNKNOWN and result.error_code == "approval_no_longer_valid"
    assert mock.calls == 0 and core.approvals.get(approval.id).status == A.CONSUMED


def test_concurrent_requests_and_decisions_are_one_scope_and_one_decision(core):
    _, run = protected(core)
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: core.approvals.request(run.id).id, range(8)))
    assert len(set(ids)) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(lambda _: core.approvals.decide(ids[0], allow=True).status, range(8)))
    assert set(statuses) == {A.APPROVED}
    assert core.store.one("SELECT COUNT(*) FROM operation_specs")[0] == 1
    assert sum(e.kind == "approval.requested" for e in core.events()) == 1
    assert sum(e.kind == "approval.decided" for e in core.events()) == 1


@pytest.mark.parametrize("iteration", range(6))
def test_revoke_execute_race_has_only_serializable_outcomes(core, mock, iteration):
    _, run, approval = granted(core)
    barrier = Barrier(2)
    def dispatch():
        barrier.wait(timeout=5)
        try:
            return core.runs.execute(run.id, approval_id=approval.id).status
        except ApprovalRequired:
            return "blocked"
    def revoke():
        barrier.wait(timeout=5)
        try:
            return core.approvals.revoke(approval.id).status
        except Conflict:
            return "already_consumed"
    with ThreadPoolExecutor(max_workers=2) as pool:
        execution, revocation = pool.submit(dispatch), pool.submit(revoke)
        pair = execution.result(timeout=5), revocation.result(timeout=5)
    assert pair in {(R.SUCCEEDED, "already_consumed"), ("blocked", A.REVOKED)}
    assert mock.calls == (1 if pair[0] == R.SUCCEEDED else 0)


def test_preparation_drift_rolls_back_claim_and_never_executes(tmp_path):
    class Drifting(MockExecutor):
        revision = "drifting-fixture-v1"

        def approval_configuration(self):
            return super().approval_configuration()

        def prepare(self, request):
            self.interrupt = True
    mock = Drifting()
    with Core(tmp_path / "state", executors=[mock]) as core:
        _, run, approval = granted(core)
        before = core.events()
        with pytest.raises(ApprovalRequired, match="during_preparation"):
            core.runs.execute(run.id, approval_id=approval.id)
        assert core.runs.get(run.id).status == R.CREATED and mock.calls == 0
        assert core.approvals.get(approval.id).status == A.APPROVED
        assert core.events() == before


def test_drift_after_durable_claim_blocks_dispatch_and_stays_uncertain(core, mock, monkeypatch):
    _, run, approval = granted(core)
    original = core.store.event
    def change_after_claim(conn, kind, **kwargs):
        original(conn, kind, **kwargs)
        if kind == "run.started":
            mock.interrupt = True
    monkeypatch.setattr(core.store, "event", change_after_claim)
    result = core.runs.execute(run.id, approval_id=approval.id)
    assert result.status == R.UNKNOWN and result.error_code == "execution_spec_changed"
    assert mock.calls == 0 and result.execution_stopped == 0
    assert core.approvals.get(approval.id).status == A.CONSUMED
    with pytest.raises(Conflict):
        core.approvals.request(run.id)


def test_late_callable_and_config_replacement_cannot_replace_frozen_invocation(core, mock, monkeypatch):
    _, run, approval = granted(core)
    matches = core.approvals.matches
    changed_calls = []
    def changed(self, request):
        changed_calls.append(True)
        raise AssertionError("Changed callable must not run")
    def mutate_after_final_guard(row, spec, bound_methods=None):
        result = matches(row, spec, bound_methods)
        if row["status"] == R.RUNNING:
            mock.execute = MethodType(changed, mock)
            mock.interrupt = True
        return result
    monkeypatch.setattr(core.approvals, "matches", mutate_after_final_guard)
    assert core.runs.execute(run.id, approval_id=approval.id).status == R.SUCCEEDED
    assert mock.calls == 1 and changed_calls == []


@pytest.mark.parametrize("table,sql", [
    ("spec", "UPDATE operation_specs SET body_json='{}'"),
    ("spec", "DELETE FROM operation_specs"),
    ("grant", "DELETE FROM approvals"),
    ("grant", "UPDATE approvals SET action_hash='changed'"),
    ("grant", "UPDATE approvals SET spec_digest=NULL"),
    ("grant", "UPDATE approvals SET expires_at='2099-01-01'"),
])
def test_scope_rows_are_immutable_in_sql(core, table, sql):
    _, _, approval = granted(core)
    before = core.approvals.get(approval.id)
    with pytest.raises(sqlite3.IntegrityError), core.store.transaction() as conn:
        conn.execute(sql)
    assert core.approvals.get(approval.id) == before


def legacy_v5(state, status="approved"):
    state.mkdir()
    path = state / "core.sqlite3"
    action = Action.build("mock.protected_echo")
    with sqlite3.connect(path) as conn:
        for number in range(1, 6):
            for sql in MIGRATIONS[number]:
                conn.execute(sql)
            conn.execute("INSERT INTO migrations VALUES(?,?,?)", (number, migration_checksum(MIGRATIONS[number]), utc_now()))
        conn.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)", ("a"*32, "Legacy", "", "mock", "awaiting_approval", 1, utc_now(), utc_now(), "task", "old"))
        conn.execute("""INSERT INTO runs(id,task_id,attempt,executor,status,idempotency_key,action_json,action_hash,title,description,created_at,updated_at,effect_status,execution_stopped)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", ("b"*32,"a"*32,1,"mock","created","run",action.json,action.hash,"Legacy","",utc_now(),utc_now(),"not_started",1))
        conn.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?)", ("c"*32,"a"*32,"b"*32,action.hash,status,utc_now(),"2099-01-01T00:00:00+00:00",None,None))
        conn.execute("PRAGMA user_version=5")
    return path


@pytest.mark.parametrize("status", ["pending", "approved", "rejected", "consumed", "expired"])
def test_legacy_approvals_are_preserved_but_not_silently_reauthorized(tmp_path, status):
    state = tmp_path / "state"
    legacy_v5(state, status)
    with Core(state) as core:
        old = core.approvals.get("c"*32)
        assert old.status == (A.INVALIDATED if status in {"pending","approved"} else status)
        assert old.operation_json is None and old.spec_digest is None
        with pytest.raises(ApprovalRequired):
            core.runs.execute("b"*32, approval_id=old.id)
        fresh = core.approvals.request("b"*32)
        assert fresh.status == A.PENDING and fresh.operation_json
        assert core.store.one("PRAGMA integrity_check")[0] == "ok"
        assert core.store.all("PRAGMA foreign_key_check") == []
        backups = list((state / "backups").glob(f"pre-v{SCHEMA_VERSION}-*.sqlite3"))
        assert len(backups) == 1
        with sqlite3.connect(backups[0]) as backup:
            assert backup.execute("PRAGMA user_version").fetchone()[0] == 5
            assert backup.execute("SELECT status FROM approvals").fetchone()[0] == status
    with Core(state) as reopened:
        assert reopened.approvals.get(fresh.id).status == A.PENDING


def test_failed_v6_migration_rolls_back_dropped_table_and_ledger(tmp_path, monkeypatch):
    state = tmp_path / "state"
    path = legacy_v5(state)
    with monkeypatch.context() as patch:
        patch.setitem(MIGRATIONS, 6, (*MIGRATIONS[6], "INVALID SQL"))
        with pytest.raises(sqlite3.OperationalError):
            Core(state)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("SELECT status FROM approvals").fetchone()[0] == "approved"
        assert conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 5
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='operation_specs'").fetchone() is None
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='approvals_by_run'").fetchone()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_cli_exposes_exact_scope_and_explicit_revoke(tmp_path):
    from test_cli import cli
    state = tmp_path / "state"
    task = cli(state, "create", "--title", "Review exact operation", "--key", "task")
    cli(state, "plan", task["id"])
    run = cli(state, "prepare", task["id"], "--key", "run", "--protected")
    approval = cli(state, "request-approval", run["id"])
    body = cli(state, "approval-spec", approval["id"])
    assert body["run_id"] == run["id"] and body["executor"]["revision"] == "mock-v1"
    assert cli(state, "revoke", approval["id"])["status"] == "revoked"
    assert cli(state, "execute", run["id"], "--approval", approval["id"], expected=2) == {"error": "ApprovalRequired"}


def test_school_history_migrates_only_on_a_copy_and_export_is_identical(tmp_path, monkeypatch):
    from ii_pro_school import School
    from test_school import FakeStudent
    import ii_pro_core.storage as storage
    original = tmp_path / "original-v5"
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 5)
        with School(original, FakeStudent()) as previous:
            previous.install_curriculum()
            previous.register_student("fixture", "Offline fixture")
            session_id = previous.create_session("fixture")
            run = previous.prepare_attempt(session_id, "bug-positive-sum")
            previous.execute(run.id)
            previous.assess(run.id)
            expected = previous.export_session(session_id)
    original_hash = hashlib.sha256((original / "core.sqlite3").read_bytes()).hexdigest()
    copy = tmp_path / "school-copy"
    shutil.copytree(original, copy)
    def school_rows():
        with sqlite3.connect(copy / "core.sqlite3") as conn:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'school_%' AND name NOT LIKE 'school_v2_%' ORDER BY name")]
            return {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables}
    before = school_rows()
    adapter = FakeStudent()
    with School(copy, adapter) as school:
        assert school.export_session(session_id) == expected
        assert school.store.one("PRAGMA user_version")[0] == SCHEMA_VERSION
        assert adapter.calls == 0
    assert school_rows() == before
    with sqlite3.connect(copy / "core.sqlite3") as conn:
        added = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'school_v2_%' AND name!='school_v2_policy'")]
        assert added and all(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0 for table in added)
    assert hashlib.sha256((original / "core.sqlite3").read_bytes()).hexdigest() == original_hash


def test_protected_extensions_cannot_silently_inherit_incomplete_mock_configuration(tmp_path):
    class Undeclared(MockExecutor):
        def execute(self, request):
            raise AssertionError("Unversioned extension must not execute")
    with Core(tmp_path / "state", executors=[Undeclared()]) as core:
        _, run = protected(core)
        with pytest.raises(ValidationError):
            core.approvals.request(run.id)
        assert core.store.one("SELECT COUNT(*) FROM approvals")[0] == 0


def test_school_profile_and_transport_are_immutable_without_any_backend_call(tmp_path):
    from ii_pro_school import ModelProfile, OllamaStudentAdapter
    from ii_pro_school.adapters import LoopbackTransport
    from ii_pro_core import TaskStatus as T
    profile = ModelProfile.build("qwen7b-fast:latest", "a"*64)
    adapter = OllamaStudentAdapter(profile)
    policy = ExecutionPolicy(approval_actions=("mock.protected_echo", "school.propose"))
    with Core(tmp_path / "state", executors=[adapter], policy=policy) as core:
        task = core.tasks.create("Review local student profile", executor=adapter.name, idempotency_key="task")
        core.tasks.transition(task.id, T.PLANNED)
        action = Action.build("school.propose", {"session_id": "d"*32, "lesson_id": "fixture", "revision": 1,
                             "lesson_digest": "c"*64, "profile_digest": profile.id, "prompt": "No inference in this test"})
        run = core.runs.create(task.id, action=action, idempotency_key="run")
        approval = core.approvals.request(run.id)  # metadata scope only; no probe or dispatch
        config = core.approvals.describe(approval.id)["executor"]["configuration"]
        assert config["model_profile"]["expected_digest"] == "a"*64
        assert config["transport_port"] == 11434
        with pytest.raises(AttributeError):
            adapter.profile = profile
        with pytest.raises(AttributeError):
            adapter.transport = LoopbackTransport("http://127.0.0.1:1")
        with pytest.raises(AttributeError):
            adapter.transport.port = 1
        assert core.runs.get(run.id).status == R.CREATED


@pytest.mark.parametrize("injected", [True, False])
def test_protected_school_rejects_opaque_or_mismatched_transport(injected):
    from ii_pro_school import ModelProfile, OllamaStudentAdapter
    from ii_pro_school.adapters import LoopbackTransport
    transport = object() if injected else LoopbackTransport("http://127.0.0.1:1")
    adapter = OllamaStudentAdapter(ModelProfile.build("qwen7b-fast:latest", "a"*64), transport=transport)
    with pytest.raises(ValidationError):
        adapter.approval_configuration()


def test_committed_first_qwen_lesson_bytes_match_the_original_manifest():
    folder = Path(__file__).resolve().parents[1] / "experiments/school_v01_first"
    if not folder.exists():
        pytest.skip("Original lesson evidence is kept in the private research repository")
    manifest = json.loads((folder / "manifest.json").read_bytes())
    for filename, metadata in manifest["files"].items():
        assert hashlib.sha256((folder / filename).read_bytes()).hexdigest() == metadata["sha256"]
    exported = json.loads((folder / "session.json").read_bytes())
    assert exported["attempts"][0]["response"]["text"].encode() == (folder / "response.txt").read_bytes()
    assert len(exported["attempts"]) == 1
