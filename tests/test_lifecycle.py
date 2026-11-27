import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event

import pytest

from ii_pro_core import Action, Core, MockExecutor, RunStatus as R, TaskStatus as T
from ii_pro_core.errors import Conflict, CoreBusy, InvalidTransition, NotFound, ValidationError
from ii_pro_core.executors import Executor
from conftest import planned


def test_create_and_update_task(core):
    task = core.tasks.create("Initial", description="Public specification", idempotency_key="new")
    assert len(task.id) == 32 and task.executor == "mock" and task.status == T.DRAFT
    assert datetime.fromisoformat(task.created_at).utcoffset().total_seconds() == 0
    updated = core.tasks.update(task.id, expected_version=1, title="Updated")
    assert updated.title == "Updated" and updated.version == 2
    assert updated.created_at == task.created_at and updated.updated_at >= task.updated_at
    assert [event.kind for event in core.events(task.id)] == ["task.created", "task.updated"]
    with pytest.raises(Conflict):
        core.tasks.update(task.id, expected_version=1, title="Stale")


@pytest.mark.parametrize("target", [T.RUNNING, T.VERIFIED, T.SAVED, T.FAILED, "invented"])
def test_forbidden_transitions_leave_state_and_events_intact(core, target):
    task = core.tasks.create("Draft", idempotency_key="draft")
    before = core.events(task.id)
    with pytest.raises(InvalidTransition):
        core.tasks.transition(task.id, target)
    assert core.tasks.get(task.id) == task and core.events(task.id) == before


def test_successful_lifecycle_does_not_auto_verify(core, mock):
    task = planned(core)
    run = core.runs.create(task.id, idempotency_key="run")
    result = core.runs.execute(run.id)
    assert result.status == R.SUCCEEDED and mock.calls == 1
    assert result.started_at and result.finished_at
    assert core.tasks.get(task.id).status == T.VERIFYING
    core.tasks.transition(task.id, T.VERIFIED)
    assert core.tasks.transition(task.id, T.SAVED).status == T.SAVED
    with pytest.raises(InvalidTransition):
        core.tasks.transition(task.id, T.PLANNED)


def test_executor_error_retry_keeps_history(tmp_path):
    executor = MockExecutor(failures=1)
    with Core(tmp_path / "state", executors=[executor]) as core:
        task = planned(core)
        first = core.runs.create(task.id, idempotency_key="first")
        failure = core.runs.execute(first.id)
        assert failure.status == R.FAILED and failure.error_type == "RuntimeError"
        assert failure.error_code == "executor_not_started"
        assert core.tasks.get(task.id).status == T.FAILED
        assert core.runs.execute(first.id) == failure and executor.calls == 1
        core.tasks.transition(task.id, T.PLANNED)
        second = core.runs.create(task.id, idempotency_key="second")
        assert core.runs.execute(second.id).status == R.SUCCEEDED
        runs = core.runs.list(task.id)
        assert [r.attempt for r in runs] == [1, 2]
        assert [r.status for r in runs] == [R.FAILED, R.SUCCEEDED]
        assert sum(e.kind == "run.failed" for e in core.events(task.id)) == 1


def test_task_idempotency_replays_or_rejects_changed_payload(core):
    first = core.tasks.create("Same", idempotency_key="one")
    assert core.tasks.create("Same", idempotency_key="one").id == first.id
    with pytest.raises(Conflict):
        core.tasks.create("Different", idempotency_key="one")
    assert len(core.events()) == 1


def test_run_idempotency_no_second_dispatch(core, mock):
    task = planned(core)
    run = core.runs.create(task.id, idempotency_key="one")
    assert core.runs.create(task.id, idempotency_key="one").id == run.id
    with pytest.raises(Conflict):
        core.runs.create(task.id, action=Action.build(parameters={"text": "Changed"}), idempotency_key="one")
    with pytest.raises(Conflict):
        core.runs.create(task.id, idempotency_key="another-active-run")
    first = core.runs.execute(run.id)
    assert core.runs.execute(run.id) == first and mock.calls == 1
    assert sum(e.kind == "run.succeeded" for e in core.events()) == 1


def test_global_run_key_cannot_return_another_task(core):
    first, second = planned(core, "a"), planned(core, "b")
    core.runs.create(first.id, idempotency_key="shared")
    with pytest.raises(Conflict):
        core.runs.create(second.id, idempotency_key="shared")


def test_task_snapshot_cannot_be_edited_while_run_pending(core):
    task = planned(core)
    core.runs.create(task.id, idempotency_key="run")
    with pytest.raises(Conflict):
        core.tasks.update(task.id, expected_version=task.version, description="Changed")


def test_cancel_pending_run_without_execution(core, mock):
    task = planned(core)
    run = core.runs.create(task.id, idempotency_key="run")
    cancelled = core.runs.cancel(run.id)
    assert cancelled.status == R.CANCELLED
    assert core.runs.cancel(run.id) == cancelled
    assert core.runs.execute(run.id).status == R.CANCELLED and mock.calls == 0
    assert core.tasks.get(task.id).status == T.CANCELLED


def test_revision_cycle_creates_new_attempt(core):
    task = planned(core)
    core.runs.execute(core.runs.create(task.id, idempotency_key="first").id)
    core.tasks.transition(task.id, T.NEEDS_REVISION)
    core.tasks.transition(task.id, T.PLANNED)
    run = core.runs.create(task.id, idempotency_key="second")
    assert run.attempt == 2 and core.runs.execute(run.id).status == R.SUCCEEDED


def test_concurrent_duplicate_submit_is_one_record(core):
    task = planned(core)
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: core.runs.create(task.id, idempotency_key="same").id, range(8)))
    assert len(set(ids)) == 1 and len(core.runs.list(task.id)) == 1


def test_duplicate_execute_while_running_is_rejected(tmp_path):
    started, release = Event(), Event()

    class Blocking(MockExecutor):
        def execute(self, request):
            started.set()
            assert release.wait(5)
            return super().execute(request)

    executor = Blocking()
    with Core(tmp_path / "state", executors=[executor]) as core, ThreadPoolExecutor(max_workers=1) as pool:
        task = planned(core)
        run = core.runs.create(task.id, idempotency_key="run")
        other_task = planned(core, "other")
        other_run = core.runs.create(other_task.id, idempotency_key="other-run")
        future = pool.submit(core.runs.execute, run.id)
        try:
            assert started.wait(5)
            with pytest.raises(CoreBusy):
                core.runs.execute(run.id)
            with pytest.raises(CoreBusy):
                core.runs.execute(other_run.id)
            with pytest.raises(Conflict):
                core.runs.cancel(run.id)
        finally:
            release.set()
        assert future.result(timeout=5).status == R.SUCCEEDED
        assert executor.calls == 1


def test_no_database_driven_executor_import(core):
    with pytest.raises(ValidationError):
        core.tasks.create("Invalid", executor="os.system", idempotency_key="bad")
    with pytest.raises(NotFound):
        core.tasks.get("missing")
    with pytest.raises(TypeError):
        Executor()


def test_sql_looking_text_is_inert_data(core):
    title = "'); DROP TABLE tasks; --"
    task = core.tasks.create(title, idempotency_key="sql")
    assert core.tasks.get(task.id).title == title
    assert core.tasks.create("Still works", idempotency_key="next").id


def test_command_text_is_never_executed(core, tmp_path):
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("keep")
    task = planned(core)
    action = Action.build(parameters={"text": f'Remove-Item -LiteralPath "{sentinel}"'})
    run = core.runs.create(task.id, action=action, idempotency_key="inert")
    core.runs.execute(run.id)
    artifact = core.artifacts.list(run.id)[0]
    body = json.loads(core.artifacts.files.path(artifact.relative_path).read_text())
    assert "Remove-Item" in body["text"] and sentinel.read_text() == "keep"
