import json
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256

import pytest
from test_local_engine_ui import accepted, registry, request, studio  # noqa: F401

from ii_pro_core import Action
from ii_pro_core.errors import ApprovalRequired, Conflict, RecoveryRequired
from ii_pro_local.skills import packet
from ii_pro_school.adapters import LoopbackTransport, StudentFailure
from ii_pro_ui.training import REAL_PROFILE

JOB = "c" * 32
ROOT = "/api/local-engine/"


def interrupted(server, monkeypatch, *, terminal=True, complete=True, review=True, calls=None, legacy=False):
    accepted(server, "state")
    current = server.training.local_engine
    agent = current.agents["pricing"]

    def response(self, route, payload=None, **kwargs):
        if route == "version":
            return {"version": "fixture"}
        if route == "show":
            return {}
        if route == "tags":
            return {"models": [{"name": REAL_PROFILE.model, "digest": REAL_PROFILE.expected_digest}]}
        assert route == "chat"
        if calls is not None:
            calls.append(route)
        if legacy:
            raise RuntimeError("Fixture with no diagnostic")
        if not terminal:
            raise StudentFailure("timeout")
        body = {"model": REAL_PROFILE.model, "done": True, "done_reason": "length",
                "eval_count": 384, "message": {"role": "assistant", "content": '{"unfinished":'}}
        wire = json.dumps(body).encode()
        kwargs["observation"].update(http_status=200, response_complete=complete,
                                     response_bytes=len(wire), response_sha256=sha256(wire).hexdigest())
        return body

    monkeypatch.setattr(LoopbackTransport, "request", response)
    core = current.core
    evidence = packet("filter", "Source fragment.", "", "d" * 64) if review else None
    run, approval = agent.prepare(JOB, task="Review this source", requirements="Return evidence", review=evidence)
    core.approvals.decide(approval.id, allow=True)
    unknown = core.runs.execute(run.id, approval_id=approval.id)
    assert unknown.status == "unknown"
    return core, unknown, core.approvals.get(approval.id), {"project": "pricing", "job": JOB, "target": run.id}


def events(core, run):
    return [dict(row) for row in core.store.all("SELECT * FROM events WHERE run_id=? ORDER BY id", (run,))]


def test_recovery_requires_exact_approval_and_preserves_history(tmp_path, registry, monkeypatch):  # noqa: F811
    projects, _ = registry
    calls = []
    state = tmp_path / "state"
    with studio(state, projects) as server:
        core, historical, old, body = interrupted(server, monkeypatch, calls=calls)
        before = events(core, historical.id)
        artifacts = core.artifacts.list(historical.id)
        assert server.training.team.blocked()
        assert request(server, ROOT + "recovery", {**body, "target": "e" * 32})[0] == 409
        assert request(server, ROOT + "recovery", body, trusted=False)[0] == 403
        detail = accepted(server, "recovery", body)
        permit = detail["runs"][0]["recovery"]["approval"]
        with pytest.raises(ApprovalRequired):
            core.runs.execute(permit["run"], approval_id=permit["id"])
        decision = {**body, "run": permit["run"], "approval": permit["id"], "digest": permit["digest"]}
        assert request(server, ROOT + "recovery-decision", {**decision, "approval": old.id})[0] == 409
        assert request(server, ROOT + "recovery-decision", {**decision, "target": "e" * 32})[0] == 409
        detail = accepted(server, "recovery-decision", decision)
        assert detail["runs"][0]["recovery"]["closed"]
        assert core.runs.get(historical.id) == historical and events(core, historical.id) == before
        assert core.approvals.get(old.id) == old and core.artifacts.list(historical.id) == artifacts
        assert request(server, ROOT + "recovery", body)[0] == 409
        assert request(server, ROOT + "recovery-decision", decision)[0] == 409
        with pytest.raises(RecoveryRequired):
            core.runs.execute(historical.id, approval_id=old.id)
        assert not server.training.team.blocked()
    with studio(state, projects) as server:
        detail = accepted(server, "jobs/pricing/" + JOB)
        assert detail["runs"][0]["recovery"]["closed"]
        assert not server.training.team.blocked()
        other = accepted(server, "prepare", {"project": "pricing", "job": "f" * 32, "task": "Independent review",
                                              "requirements": "Read the source"})
        assert other["approval"]["status"] == "pending"
    assert calls == ["chat"]


@pytest.mark.parametrize("case", ["timeout", "incomplete", "unbounded", "legacy"])
def test_recovery_fails_closed_without_terminal_read_only_proof(tmp_path, registry, monkeypatch, case):  # noqa: F811
    with studio(tmp_path / "state", registry[0]) as server:
        core, historical, old, body = interrupted(server, monkeypatch, terminal=case != "timeout",
                                                   complete=case != "incomplete", review=case != "unbounded", legacy=case == "legacy")
        detail = accepted(server, "jobs/pricing/" + JOB)
        assert not detail["runs"][0]["recovery"]["eligible"]
        assert request(server, ROOT + "recovery", body)[0] == 409
        assert not core.store.all("SELECT 1 FROM runs WHERE executor='local_recovery'")
        assert core.approvals.get(old.id).status == "consumed" and core.runs.get(historical.id).status == "unknown"
        assert server.training.team.blocked()


def test_recovery_active_lease_and_concurrent_decisions(tmp_path, registry, monkeypatch):  # noqa: F811
    with studio(tmp_path / "state", registry[0]) as server:
        core, _, _, body = interrupted(server, monkeypatch)
        with core.store.execution("b" * 32):
            assert request(server, ROOT + "recovery", body)[0] == 409
        permit = accepted(server, "recovery", body)["runs"][0]["recovery"]["approval"]
        decision = {**body, "run": permit["run"], "approval": permit["id"], "digest": permit["digest"]}
        with core.store.execution("b" * 32):
            assert request(server, ROOT + "recovery-decision", decision)[0] == 409
            assert core.approvals.get(permit["id"]).status == "pending"
        with ThreadPoolExecutor(2) as pool:
            statuses = list(pool.map(lambda _: request(server, ROOT + "recovery-decision", decision)[0], range(2)))
        assert sorted(statuses) == [200, 409]


def test_recovery_fails_if_state_changes_after_preparation(tmp_path, registry, monkeypatch):  # noqa: F811
    with studio(tmp_path / "state", registry[0]) as server:
        core, historical, _, body = interrupted(server, monkeypatch)
        permit = accepted(server, "recovery", body)["runs"][0]["recovery"]["approval"]
        core.runs.resolve_interrupted(historical.id, confirm_stopped=True)  # Independent state change in the fixture.
        decision = {**body, "run": permit["run"], "approval": permit["id"], "digest": permit["digest"]}
        assert request(server, ROOT + "recovery-decision", decision)[0] == 409
        assert core.approvals.get(permit["id"]).status == "pending"
        assert server.training.team.blocked()


def test_recovery_does_not_accept_another_executor(tmp_path, registry, monkeypatch):  # noqa: F811
    with studio(tmp_path / "state", registry[0]) as server:
        core, _, _, _ = interrupted(server, monkeypatch)
        task = core.tasks.create("Independent cloud boundary", executor="team_text", idempotency_key="foreign-task")
        core.tasks.transition(task.id, "planned")
        run = core.runs.create(task.id, action=Action.build(), idempotency_key="foreign-run")
        with pytest.raises(Conflict):
            server.training.local_engine.recovery.prepare_action(run.id)
        assert server.training.team.blocked()
