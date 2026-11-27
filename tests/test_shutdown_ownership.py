"""Shutdown/ownership regressions. Effects and child processes use local fixtures."""
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import pytest

from ii_pro_core import Action, Core, CoreState as C, EffectStatus as E, MockExecutor, RunStatus as R, TaskStatus as T
from ii_pro_core.errors import Conflict, CoreBusy, OwnershipLost, RecoveryRequired, ValidationError
from ii_pro_core.storage import MIGRATIONS, SCHEMA_VERSION, migration_checksum, utc_now
from conftest import planned


class Blocking(MockExecutor):
    def __init__(self, *, cancellable=False, fail=False):
        super().__init__()
        self.supports_cancellation = cancellable
        self.started, self.release = Event(), Event()
        self.request = None
        self.fail = fail

    def execute(self, request):
        self.request = request
        self.started.set()
        assert self.release.wait(10), "Fixture did not release executor"
        if self.fail:
            raise RuntimeError("Unconfirmed effect")
        return super().execute(request)


def setup_run(core, key="task"):
    task = planned(core, key)
    return task, core.runs.create(task.id, idempotency_key=f"{key}-run")


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("Fixture condition did not become true")
        time.sleep(0.005)


def test_close_drains_before_releasing_ownership_and_stops_admission(tmp_path):
    state, executor = tmp_path / "state", Blocking()
    core = Core(state, executors=[executor])
    task, run = setup_run(core)
    other_task, other = setup_run(core, "other")
    generation = core.store.generation
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(core.runs.execute, run.id)
        assert executor.started.wait(5)
        closing = pool.submit(core.close, timeout=3)
        try:
            wait_for(lambda: core.state == C.STOPPING)
            assert core.active_run_ids == (run.id,) and not closing.done()
            with pytest.raises(CoreBusy):
                Core(state)
            with pytest.raises(CoreBusy):
                core.runs.execute(other.id)
            with pytest.raises(CoreBusy):
                core.runs.create(other_task.id, idempotency_key="late-run")
            with pytest.raises(CoreBusy):
                planned(core, "late-task")
            with pytest.raises(CoreBusy):
                core.runs.recover()
        finally:
            executor.release.set()
        result = running.result(timeout=5)
        assert result.status == R.SUCCEEDED
        closing.result(timeout=5)
    assert core.state == C.CLOSED and core.active_run_ids == ()
    core.close()  # idempotent close
    with sqlite3.connect(state / "core.sqlite3") as conn:
        requested, closed = conn.execute("SELECT shutdown_requested_at,closed_at FROM owner_sessions").fetchone()
        assert requested and closed and closed >= requested
    with Core(state) as reopened:
        assert reopened.store.generation == generation + 1
        assert reopened.recovered_run_ids == []
        assert reopened.runs.get(run.id) == result
        assert reopened.runs.get(other.id).status == R.CREATED
        assert reopened.tasks.get(task.id).status == T.VERIFYING


@pytest.mark.parametrize("cancellable", [False, True])
def test_timeout_and_ignored_cancel_keep_owner_until_real_return(tmp_path, cancellable):
    state, executor = tmp_path / "state", Blocking(cancellable=cancellable)
    core = Core(state, executors=[executor])
    _, run = setup_run(core)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(core.runs.execute, run.id)
        try:
            assert executor.started.wait(5)
            started = time.monotonic()
            with pytest.raises(CoreBusy):
                core.close(timeout=0.03, cancel=True)
            assert time.monotonic() - started < 1
            assert core.state == C.STOPPING and core.store.connection is not None
            assert core.active_run_ids == (run.id,)
            current = core.runs.get(run.id)
            assert current.status == R.RUNNING and current.execution_stopped == 0
            if cancellable:
                assert executor.request.cancellation.requested
            else:
                assert executor.request.cancellation is None
            assert core.store.one("SELECT closed_at FROM owner_sessions")[0] is None
            with pytest.raises(CoreBusy):
                Core(state)
            with pytest.raises(CoreBusy):
                core.store.close(timeout=0)
        finally:
            executor.release.set()
        assert running.result(timeout=5).status == R.SUCCEEDED
    assert core.state == C.STOPPING  # no background auto-release
    core.close()
    with Core(state) as reopened:
        assert reopened.runs.get(run.id).status == R.SUCCEEDED


def test_cooperative_cancel_does_not_claim_absence_of_effect(tmp_path):
    started = Event()
    class Cooperative(MockExecutor):
        supports_cancellation = True

        def execute(self, request):
            started.set()
            assert request.cancellation.wait(5)
            raise RuntimeError("Stopped cooperatively; effect not verified")

    state = tmp_path / "state"
    core = Core(state, executors=[Cooperative()])
    task, run = setup_run(core)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(core.runs.execute, run.id)
        assert started.wait(5)
        core.close(timeout=2, cancel=True)
        result = running.result(timeout=5)
        assert result.status == R.UNKNOWN and result.effect_status == E.UNKNOWN
        assert result.execution_stopped == 0
    with Core(state) as reopened:
        assert reopened.runs.get(run.id) == result
        with pytest.raises(RecoveryRequired):
            reopened.runs.execute(run.id)
        reopened.runs.resolve_interrupted(run.id, confirm_stopped=True)
        with pytest.raises(RecoveryRequired):
            reopened.tasks.transition(task.id, T.PLANNED)


def test_executor_cannot_deadlock_by_closing_its_own_core(tmp_path):
    class SelfClosing(MockExecutor):
        def execute(self, request):
            core.close(timeout=5)
            pytest.fail("Self-close must not release ownership")

    core = Core(tmp_path / "state", executors=[SelfClosing()])
    _, run = setup_run(core)
    started = time.monotonic()
    result = core.runs.execute(run.id)
    assert time.monotonic() - started < 1
    assert result.status == R.UNKNOWN and result.error_type == "CoreBusy"
    assert core.state == C.STOPPING and core.active_run_ids == ()
    core.close()


def test_concurrent_closers_do_not_release_active_owner(tmp_path):
    executor = Blocking()
    core = Core(tmp_path / "state", executors=[executor])
    _, run = setup_run(core)
    with ThreadPoolExecutor(max_workers=4) as pool:
        running = pool.submit(core.runs.execute, run.id)
        assert executor.started.wait(5)
        closers = [pool.submit(core.close, timeout=3) for _ in range(3)]
        try:
            wait_for(lambda: core.state == C.STOPPING)
            with pytest.raises(CoreBusy):
                Core(tmp_path / "state")
        finally:
            executor.release.set()
        assert running.result(timeout=5).status == R.SUCCEEDED
        for closer in closers:
            closer.result(timeout=5)
    assert core.state == C.CLOSED


def test_simultaneous_requests_dispatch_only_one_run(tmp_path):
    executor, barrier = Blocking(), Barrier(2)
    with Core(tmp_path / "state", executors=[executor]) as core:
        _, first = setup_run(core, "first")
        _, second = setup_run(core, "second")
        def dispatch(run_id):
            barrier.wait(timeout=5)
            try:
                return core.runs.execute(run_id).status
            except CoreBusy:
                return "busy"
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(dispatch, r.id) for r in (first, second)]
            try:
                assert executor.started.wait(5)
                wait_for(lambda: any(f.done() for f in futures))
                assert sum(core.runs.get(r.id).status == R.RUNNING for r in (first, second)) == 1
            finally:
                executor.release.set()
            assert sorted(f.result(timeout=5) for f in futures) == sorted([R.SUCCEEDED, "busy"])
        assert executor.calls == 1 and core.active_run_ids == ()


@pytest.mark.parametrize("phase", ["prepare", "import"])
def test_entire_invocation_is_counted_including_preparation_and_import(tmp_path, monkeypatch, phase):
    started, release = Event(), Event()
    class Preparing(MockExecutor):
        def prepare(self, request):
            if phase == "prepare":
                started.set()
                assert release.wait(10)
            super().prepare(request)

    state = tmp_path / "state"
    core = Core(state, executors=[Preparing()])
    _, run = setup_run(core)
    real_import = core.artifacts.write_outputs
    def blocked_import(*args):
        if phase == "import":
            started.set()
            assert release.wait(10)
        return real_import(*args)
    monkeypatch.setattr(core.artifacts, "write_outputs", blocked_import)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(core.runs.execute, run.id)
        try:
            assert started.wait(5)
            before = time.monotonic()
            with pytest.raises(CoreBusy):
                core.close(timeout=0.03)
            assert time.monotonic() - before < 1
            with pytest.raises(CoreBusy):
                Core(state)
        finally:
            release.set()
        assert running.result(timeout=5).status == R.SUCCEEDED
    core.close()


@pytest.mark.parametrize("fail", [False, True])
def test_old_generation_cannot_publish_result_error_or_artifacts(tmp_path, fail):
    state, executor = tmp_path / "state", Blocking(fail=fail)
    core = Core(state, executors=[executor])
    task, run = setup_run(core)
    old_generation = core.store.generation
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(core.runs.execute, run.id)
        try:
            assert executor.started.wait(5)
            before = core.events()
            # Deliberate fault injection, NOT an offered live takeover API.
            # A real second Core is blocked by the still-held OS lock.
            with sqlite3.connect(state / "core.sqlite3") as conn:
                conn.execute("INSERT INTO owner_sessions(owner_id,opened_at) VALUES(?,?)", ("fixture-new-owner", utc_now()))
            with pytest.raises(OwnershipLost):
                core.tasks.create("Stale write", idempotency_key="stale")
            with pytest.raises(CoreBusy):
                Core(state)
        finally:
            executor.release.set()
        with pytest.raises(OwnershipLost):
            running.result(timeout=5)
    assert core.events() == before and core.runs.get(run.id).status == R.RUNNING
    assert core.artifacts.list(run.id) == []
    assert not list((state / "workspace").rglob("result.json"))
    core.close()
    with sqlite3.connect(state / "core.sqlite3") as conn:
        assert conn.execute("SELECT closed_at FROM owner_sessions WHERE owner_id='fixture-new-owner'").fetchone()[0] is None
    with Core(state) as reopened:
        recovered = reopened.runs.get(run.id)
        assert reopened.store.generation == old_generation + 2
        assert recovered.status == R.UNKNOWN and recovered.owner_generation == old_generation
        assert recovered.execution_stopped == 0
        with pytest.raises(RecoveryRequired):
            reopened.runs.create(task.id, idempotency_key="retry")


def test_run_claim_generation_is_checked_separately(tmp_path):
    executor = Blocking()
    core = Core(tmp_path / "state", executors=[executor])
    _, run = setup_run(core)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(core.runs.execute, run.id)
        try:
            assert executor.started.wait(5)
            with core.store.transaction() as conn:
                conn.execute("UPDATE runs SET owner_generation=NULL WHERE id=?", (run.id,))
        finally:
            executor.release.set()
        with pytest.raises(Conflict):
            running.result(timeout=5)
    assert core.runs.get(run.id).status == R.RUNNING
    assert core.artifacts.list(run.id) == []
    core.close()
    with Core(tmp_path / "state") as reopened:
        assert reopened.runs.get(run.id).status == R.UNKNOWN


def test_close_failure_retains_owner_and_can_be_retried(tmp_path):
    state = tmp_path / "state"
    core = Core(state)
    with core.store.transaction() as conn:
        conn.execute("""CREATE TRIGGER fail_close BEFORE UPDATE ON owner_sessions
            BEGIN SELECT RAISE(ABORT,'fixture close failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        core.close()
    assert core.state == C.STOPPING and core.store.connection is not None
    assert core.store.one("SELECT closed_at FROM owner_sessions")[0] is None
    with pytest.raises(CoreBusy):
        Core(state)
    with core.store.transaction() as conn:
        conn.execute("DROP TRIGGER fail_close")
    core.close()
    assert core.state == C.CLOSED


def test_external_sqlite_writer_does_not_make_shutdown_wait_unbounded(tmp_path):
    state = tmp_path / "state"
    core = Core(state)
    with sqlite3.connect(state / "core.sqlite3") as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            started = time.monotonic()
            with pytest.raises(CoreBusy):
                core.close(timeout=0.03)
            assert time.monotonic() - started < 1
            assert core.state == C.STOPPING and core.store.connection is not None
            with pytest.raises(CoreBusy):
                Core(state)
        finally:
            writer.rollback()
    core.close()


def test_shutdown_wait_for_storage_lock_is_bounded(tmp_path):
    entered, release = Event(), Event()
    core = Core(tmp_path / "state")
    def transaction():
        with core.store.transaction():
            entered.set()
            assert release.wait(5)
    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(transaction)
        try:
            assert entered.wait(5)
            before = time.monotonic()
            with pytest.raises(CoreBusy):
                core.close(timeout=0.03)
            assert time.monotonic() - before < 1
            assert core.state == C.STOPPING
            with pytest.raises(CoreBusy):
                Core(tmp_path / "state")
        finally:
            release.set()
        writer.result(timeout=5)
    core.close()


def test_cancel_is_delivered_to_an_admitted_invocation_waiting_for_storage(tmp_path):
    observed = Event()
    class Cooperative(MockExecutor):
        supports_cancellation = True

        def execute(self, request):
            assert request.cancellation.requested
            observed.set()
            return super().execute(request)

    core = Core(tmp_path / "state", executors=[Cooperative()])
    _, run = setup_run(core)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with core.store.lock:
            running = pool.submit(core.runs.execute, run.id)
            wait_for(lambda: core.active_run_ids == (run.id,))
            with pytest.raises(CoreBusy):
                core.close(timeout=0, cancel=True)
        assert running.result(timeout=5).status == R.SUCCEEDED
    assert observed.is_set()
    core.close()


def test_failed_os_unlock_can_be_retried_without_new_dispatch(tmp_path, monkeypatch):
    state = tmp_path / "state"
    core = Core(state)
    real_close = core.store.owner.close
    def fail_unlock():
        raise OSError("Fixture unlock failure")
    monkeypatch.setattr(core.store.owner, "close", fail_unlock)
    with pytest.raises(OSError):
        core.close()
    assert core.state == C.STOPPING and core.store.connection is None
    with pytest.raises(CoreBusy):
        Core(state)
    monkeypatch.setattr(core.store.owner, "close", real_close)
    core.close()
    assert core.state == C.CLOSED
    with Core(state):
        pass


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan"), True, "1"])
def test_invalid_shutdown_policy_does_not_change_lifecycle(tmp_path, timeout):
    with Core(tmp_path / "state") as core:
        with pytest.raises(ValidationError):
            core.close(timeout=timeout)
        assert core.state == C.RUNNING


def test_hard_exit_releases_os_lock_but_never_proves_effect_absence(tmp_path):
    state, counter = tmp_path / "state", tmp_path / "counter.txt"
    ready, release = tmp_path / "ready", tmp_path / "release"
    with Core(state) as core:
        task, run = setup_run(core)
    program = """
import os, sys, time
from pathlib import Path
from ii_pro_core import Core, MockExecutor
class Crash(MockExecutor):
    def execute(self, request):
        with Path(sys.argv[2]).open('w') as output:
            output.write('1'); output.flush(); os.fsync(output.fileno())
        Path(sys.argv[3]).write_text('ready')
        deadline = time.monotonic() + 5
        while not Path(sys.argv[4]).exists() and time.monotonic() < deadline:
            time.sleep(.01)
        os._exit(41)
with Core(sys.argv[1], executors=[Crash()]) as core:
    core.runs.execute(sys.argv[5])
"""
    child = subprocess.Popen([sys.executable, "-B", "-c", program, str(state), str(counter),
                              str(ready), str(release), run.id],
                             cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_for(ready.exists)
        with pytest.raises(CoreBusy):
            Core(state)
    finally:
        release.write_text("release fixture")
        _, error = child.communicate(timeout=10)
    assert child.returncode == 41, error
    for index in range(2):
        executor = MockExecutor()
        with Core(state, executors=[executor]) as core:
            current = core.runs.get(run.id)
            assert current.status == R.UNKNOWN and current.execution_stopped == 0
            assert core.recovered_run_ids == ([run.id] if index == 0 else [])
            with pytest.raises(RecoveryRequired):
                core.runs.execute(run.id)
            with pytest.raises(RecoveryRequired):
                core.runs.create(task.id, idempotency_key="retry")
            assert counter.read_text() == "1" and executor.calls == 0
            assert core.store.one("SELECT closed_at FROM owner_sessions WHERE generation=?", (current.owner_generation,))[0] is None


def make_v3(state):
    state.mkdir()
    path = state / "core.sqlite3"
    with sqlite3.connect(path) as conn:
        for version in (1, 2, 3):
            for statement in MIGRATIONS[version]:
                conn.execute(statement)
            conn.execute("INSERT INTO migrations VALUES(?,?,?)", (version, migration_checksum(MIGRATIONS[version]), utc_now()))
        conn.execute("PRAGMA user_version=3")
    return path


def test_v3_upgrade_keeps_consistent_backup_and_old_migrations(tmp_path):
    state = tmp_path / "state"
    path = make_v3(state)
    action = Action.build()
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)", (
            "a" * 32, "Legacy uncertain", "", "mock", "failed", 1, utc_now(), utc_now(), "task", "legacy"))
        conn.execute("""INSERT INTO runs(id,task_id,attempt,executor,status,idempotency_key,
            action_json,action_hash,title,description,error_code,error_type,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            "b" * 32, "a" * 32, 1, "mock", "unknown", "run", action.json, action.hash,
            "Legacy uncertain", "", "executor_failed", "RuntimeError", utc_now(), utc_now()))
    with Core(state) as core:
        assert core.store.generation == 1
        assert core.store.one("PRAGMA user_version")[0] == SCHEMA_VERSION
        assert core.store.one("PRAGMA integrity_check")[0] == "ok"
        assert core.store.all("PRAGMA foreign_key_check") == []
        legacy = core.runs.get("b" * 32)
        assert legacy.status == R.UNKNOWN and legacy.owner_generation is None
        assert legacy.effect_status == E.UNKNOWN and legacy.execution_stopped == 0
        assert legacy.error_code == "executor_failed" and core.recovered_run_ids == []
        with pytest.raises(RecoveryRequired):
            core.runs.execute(legacy.id)
        backups = list((state / "backups").glob(f"pre-v{SCHEMA_VERSION}-*.sqlite3"))
        assert len(backups) == 1
        with sqlite3.connect(backups[0]) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
            assert conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 3
            assert conn.execute("SELECT name FROM sqlite_master WHERE name='owner_sessions'").fetchone() is None


def test_v4_upgrade_failure_rolls_back_schema_and_history(tmp_path, monkeypatch):
    state = tmp_path / "state"
    path = make_v3(state)
    with monkeypatch.context() as patch:
        patch.setitem(MIGRATIONS, 4, (*MIGRATIONS[4], "INVALID SQL"))
        with pytest.raises(sqlite3.OperationalError):
            Core(state)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 3
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='owner_sessions'").fetchone() is None
        assert "owner_generation" not in [r[1] for r in conn.execute("PRAGMA table_info(runs)")]
    with Core(state) as core:
        assert core.store.generation == 1
