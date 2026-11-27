import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from test_local_engine_ui import accepted, registry, request, studio  # noqa: F401
from test_local_recovery import ROOT, events, interrupted

from ii_pro_core.errors import ApprovalRequired, Conflict, RecoveryRequired
from ii_pro_local.epoch import startup_proof
from ii_pro_school.adapters import LoopbackTransport
from ii_pro_ui.training import REAL_PROFILE


def observe(monkeypatch):
    state = {"host": "a" * 64, "boot": "b" * 32, "uptime_ms": 1000}
    monkeypatch.setattr("ii_pro_local.recovery.windows_boot", lambda: state.copy())
    def unavailable():
        raise Conflict("Fixture has no system log evidence")
    monkeypatch.setattr("ii_pro_local.recovery.windows_startup", unavailable)
    return state


def sample(start):
    return {"wmi": start.isoformat(), "start": start.isoformat(), "start_event_at": start.isoformat(),
            "kind_event_at": start.isoformat(), "start_record": 101, "kind_record": 102, "boot_type": 0}


@pytest.mark.parametrize("problem", ["stale", "missing", "resume", "inconsistent", "uptime", "same_record", None])
def test_full_boot_requires_consistent_independent_evidence(problem):
    now = datetime(2026, 11, 27, 4, tzinfo=UTC)
    captured, start = now - timedelta(minutes=1), now - timedelta(seconds=30)
    facts, uptime = sample(start), 30000
    if problem == "stale":
        facts, uptime = sample(captured - timedelta(seconds=1)), 61000
    if problem == "missing":
        facts.pop("start_record")
    if problem == "resume":
        facts["boot_type"] = 1
    if problem == "inconsistent":
        facts["wmi"] = captured.isoformat()
    if problem == "uptime":
        uptime = 1000
    if problem == "same_record":
        facts["kind_record"] = facts["start_record"]
    if problem:
        with pytest.raises(Conflict):
            startup_proof(facts, captured, uptime, now)
    else:
        assert startup_proof(facts, captured, uptime, now)["started_at"] == start.isoformat()


def test_unchanged_guid_allows_only_corroborated_new_boot(tmp_path, registry, monkeypatch):  # noqa: F811
    state = observe(monkeypatch)
    path = tmp_path / "same-guid-core"
    startup = datetime.now(UTC) - timedelta(seconds=30)
    monkeypatch.setattr("ii_pro_local.recovery.utc_now", lambda: (startup - timedelta(minutes=1)).isoformat())
    with studio(path, registry[0]) as server:
        _, historical, _, body = interrupted(server, monkeypatch, legacy=True)
        accepted(server, "recovery", {**body, "mode": "witness"})
    with studio(path, registry[0]) as server:
        body = {**body, "mode": "fence"}
        assert request(server, ROOT + "recovery", body)[0] == 409
        monkeypatch.setattr("ii_pro_local.recovery.windows_boot", lambda: {
            **state, "uptime_ms": int((datetime.now(UTC) - startup).total_seconds() * 1000),
        })
        monkeypatch.setattr("ii_pro_local.recovery.windows_startup", lambda: sample(startup))
        item = accepted(server, "recovery", body)["runs"][0]["recovery"]["fence"]
        assert item["evidence"]["startup"]["start_record"] == 101
        approval = item["approval"]
        accepted(server, "recovery-decision", {**body, "run": approval["run"], "approval": approval["id"],
                                               "digest": approval["digest"]})
        assert not server.training.team.blocked()
        assert server.training.shared_core.runs.get(historical.id) == historical


def test_fence_requires_boot_change_new_owner_and_exact_approval(tmp_path, registry, monkeypatch):  # noqa: F811
    boot = observe(monkeypatch)
    path = tmp_path / "state"
    projects = registry[0]
    calls = []
    with studio(path, projects) as server:
        core, old, old_approval, body = interrupted(server, monkeypatch, legacy=True, calls=calls)
        original = events(core, old.id)
        fence = {**body, "mode": "fence"}
        witness = {**body, "mode": "witness"}
        assert request(server, ROOT + "recovery", fence)[0] == 409
        queued = accepted(server, "prepare", {"project": "pricing", "job": "f" * 32, "task": "Later independent review",
                                                "requirements": "Preserve the snapshot"})["approval"]
        with core.store.execution("e" * 32):
            assert request(server, ROOT + "recovery", witness)[0] == 409
        assert request(server, ROOT + "recovery", witness, trusted=False)[0] == 403
        accepted(server, "recovery", witness)
        assert request(server, ROOT + "recovery", witness)[0] == 409
        assert request(server, ROOT + "decision", {"project": "pricing", "job": "f" * 32, "run": queued["run"],
                        "approval": queued["id"], "digest": queued["digest"], "allow": True})[0] == 409
        boot.update(boot="c" * 32, uptime_ms=0)
        assert request(server, ROOT + "recovery", fence)[0] == 409   # Same Core owner.
    with studio(path, projects) as server:
        accepted(server, "state")
        core = server.training.shared_core
        boot.update(boot="b" * 32)
        assert request(server, ROOT + "recovery", fence)[0] == 409   # Same Windows boot.
        boot.update(boot="c" * 32, host="d" * 64)
        assert request(server, ROOT + "recovery", fence)[0] == 409   # Copy on a different host.
        boot.update(host="a" * 64, uptime_ms=10**9)
        assert request(server, ROOT + "recovery", fence)[0] == 409   # Uptime predates witness.
        boot.update(uptime_ms=0)
        assert server.training.team.blocked()
        data = accepted(server, "recovery", fence)
        permit = data["runs"][0]["recovery"]["fence"]["approval"]
        with pytest.raises(ApprovalRequired):
            core.runs.execute(permit["run"], approval_id=permit["id"])
        decision = {**fence, "run": permit["run"], "approval": permit["id"], "digest": permit["digest"]}
        with core.store.execution("e" * 32):
            assert request(server, ROOT + "recovery-decision", decision)[0] == 409
        assert request(server, ROOT + "recovery-decision", {**decision, "approval": old_approval.id})[0] == 409
        with ThreadPoolExecutor(2) as pool:
            answers = list(pool.map(lambda _: request(server, ROOT + "recovery-decision", decision)[0], range(2)))
        assert sorted(answers) == [200, 409]
        assert core.runs.get(old.id) == old and events(core, old.id) == original
        assert core.approvals.get(old_approval.id) == old_approval
        assert not server.training.team.blocked()
        with pytest.raises(RecoveryRequired):
            core.runs.execute(old.id, approval_id=old_approval.id)
        with pytest.raises(Conflict):
            core.approvals.decide(queued["id"], allow=True)
        view = accepted(server, "jobs/pricing/" + "f" * 32)
        assert view["approval"]["status"] in {"stale", "invalidated"}
        new = accepted(server, "prepare", {"project": "pricing", "job": "d" * 32, "task": "Independent review",
                                            "requirements": "Preserve the snapshot"})["approval"]
        assert new["operation"]["executor"]["configuration"]["execution_epoch"] == server.training.local_engine.recovery.epoch()
    assert calls == ["chat"]


@pytest.mark.parametrize("reason", ["stop", "length"])
def test_stale_return_cannot_dispatch_or_publish(tmp_path, registry, monkeypatch, reason):  # noqa: F811
    with studio(tmp_path / "state", registry[0]) as server:
        accepted(server, "state")
        engine = server.training.local_engine
        calls = []

        def mock(self, route, payload=None, **kwargs):
            if route == "version":
                return {"version": "fixture"}
            if route == "tags":
                return {"models": [{"name": REAL_PROFILE.model, "digest": REAL_PROFILE.expected_digest}]}
            if route == "show":
                return {}
            calls.append(route)
            monkeypatch.setattr(engine.recovery, "epoch", lambda: "e" * 64)
            return {"model": REAL_PROFILE.model, "done": True, "done_reason": reason, "message": {
                "role": "assistant", "content": json.dumps({"tool": "list_files", "args": {}, "plan": "List"})}}

        monkeypatch.setattr(LoopbackTransport, "request", mock)
        monkeypatch.setattr("ii_pro_local.agent.execute_tool", lambda *_: pytest.fail("Old response reached broker"))
        run, permit = engine.agents["pricing"].prepare("a" * 32, task="Review source", requirements="Report evidence")
        engine.core.approvals.decide(permit.id, allow=True)
        result = engine.core.runs.execute(run.id, approval_id=permit.id)
        assert result.status == "unknown" and not engine.core.artifacts.list(run.id)
        assert not any(row["kind"] == "local_agent.tool_checked" for row in events(engine.core, run.id))
        assert calls == ["chat"]
