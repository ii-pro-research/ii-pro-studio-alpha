import pytest

from ii_pro_core import Action, ApprovalStatus as A, Core, MockExecutor, RunStatus as R, TaskStatus as T
from ii_pro_core.errors import ApprovalRequired, Conflict, ValidationError
from conftest import planned


def protected(core, key="task", text="Protected fixture"):
    task = planned(core, key)
    run = core.runs.create(task.id, action=Action.build("mock.protected_echo", {"text": text}),
                           idempotency_key=f"{key}-run")
    return task, run


def test_dangerous_action_missing_pending_and_rejected_do_not_execute(core, mock):
    task, run = protected(core)
    with pytest.raises(ApprovalRequired):
        core.runs.execute(run.id)
    approval = core.approvals.request(run.id)
    assert approval.status == A.PENDING and core.tasks.get(task.id).status == T.AWAITING_APPROVAL
    assert core.approvals.request(run.id).id == approval.id
    with pytest.raises(ApprovalRequired):
        core.runs.execute(run.id, approval_id=approval.id)
    core.approvals.decide(approval.id, allow=False)
    with pytest.raises(ApprovalRequired):
        core.runs.execute(run.id, approval_id=approval.id)
    assert mock.calls == 0 and core.runs.get(run.id).status == R.CREATED
    assert sum(e.kind == "run.blocked" for e in core.events(task.id)) == 3


def test_explicit_allow_consumes_exact_action_once(core, mock):
    _, run = protected(core)
    approval = core.approvals.request(run.id)
    granted = core.approvals.decide(approval.id, allow=True)
    assert core.approvals.decide(approval.id, allow=True) == granted
    assert core.runs.execute(run.id, approval_id=approval.id).status == R.SUCCEEDED
    assert core.approvals.get(approval.id).status == A.CONSUMED
    assert core.runs.execute(run.id, approval_id=approval.id).status == R.SUCCEEDED and mock.calls == 1
    with pytest.raises(Conflict):
        core.approvals.decide(approval.id, allow=True)


def test_approval_is_bound_to_run_even_for_identical_parameters(core, mock):
    _, first = protected(core, "a")
    _, second = protected(core, "b")
    approval = core.approvals.request(first.id)
    core.approvals.decide(approval.id, allow=True)
    with pytest.raises(ApprovalRequired, match="scope_mismatch"):
        core.runs.execute(second.id, approval_id=approval.id)
    assert core.approvals.get(approval.id).status == A.APPROVED and mock.calls == 0


def test_changed_parameters_do_not_reuse_approval(core, mock):
    _, run = protected(core)
    approval = core.approvals.request(run.id)
    core.approvals.decide(approval.id, allow=True)
    changed = Action.build("mock.protected_echo", {"text": "Different operation"})
    with core.store.transaction() as conn:
        conn.execute("UPDATE runs SET action_json=?,action_hash=? WHERE id=?", (changed.json, changed.hash, run.id))
    with pytest.raises(ApprovalRequired, match="scope_mismatch"):
        core.runs.execute(run.id, approval_id=approval.id)
    assert mock.calls == 0 and core.approvals.get(approval.id).status == A.APPROVED


def test_consumed_approval_cannot_authorize_retry(tmp_path):
    mock = MockExecutor(failures=1)
    with Core(tmp_path / "state", executors=[mock]) as core:
        task, run = protected(core)
        approval = core.approvals.request(run.id)
        core.approvals.decide(approval.id, allow=True)
        assert core.runs.execute(run.id, approval_id=approval.id).status == R.FAILED
        core.tasks.transition(task.id, T.PLANNED)
        retry = core.runs.create(task.id, action=Action.from_json(run.action_json), idempotency_key="retry")
        with pytest.raises(ApprovalRequired):
            core.runs.execute(retry.id, approval_id=approval.id)
        assert mock.calls == 1


def test_expired_approval_fails_closed(core, mock, monkeypatch):
    import ii_pro_core.approvals as module
    _, run = protected(core)
    approval = core.approvals.request(run.id)
    core.approvals.decide(approval.id, allow=True)
    monkeypatch.setattr(module, "utc_now", lambda: "2099-01-01T00:00:00.000000+00:00")
    with pytest.raises(ApprovalRequired, match="expired"):
        core.runs.execute(run.id, approval_id=approval.id)
    assert core.approvals.get(approval.id).status == A.EXPIRED and mock.calls == 0


def test_rejection_is_final_but_new_request_is_possible(core):
    _, run = protected(core)
    first = core.approvals.request(run.id)
    core.approvals.decide(first.id, allow=False)
    with pytest.raises(Conflict):
        core.approvals.decide(first.id, allow=True)
    second = core.approvals.request(run.id)
    assert second.id != first.id and second.status == A.PENDING


def test_action_snapshot_does_not_change_when_input_dict_is_mutated():
    parameters = {"text": "Original"}
    action = Action.build("mock.protected_echo", parameters)
    before = action.hash
    parameters["text"] = "Changed"
    assert action.hash == before


@pytest.mark.parametrize("kind,params", [("shell", {"text": "whoami"}),
                                         ("mock.echo", {"token": "private"}),
                                         ("mock.echo", {"text": "ok", "approved": True})])
def test_unknown_or_spoofed_actions_are_rejected(kind, params):
    with pytest.raises(ValidationError):
        Action.build(kind, params)


def test_database_command_is_not_a_trusted_action(core, mock):
    task = planned(core)
    run = core.runs.create(task.id, idempotency_key="bad-db")
    with core.store.transaction() as conn:
        conn.execute("UPDATE runs SET action_json=? WHERE id=?", ('{"kind":"shell","parameters":{"text":"whoami"}}', run.id))
    with pytest.raises(ValidationError):
        core.runs.execute(run.id)
    assert mock.calls == 0 and core.runs.get(run.id).status == R.CREATED


def test_cancel_invalidates_pending_approval(core):
    task, run = protected(core)
    approval = core.approvals.request(run.id)
    core.approvals.decide(approval.id, allow=True)
    core.runs.cancel(run.id)
    assert core.approvals.get(approval.id).status == A.REJECTED
    assert core.tasks.get(task.id).status == T.CANCELLED
