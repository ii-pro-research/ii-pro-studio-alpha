import json

from test_local_engine_ui import (  # noqa: F401
    accepted,
    decision,
    mock_model,
    registry,
    request,
    settled,
    studio,
)
from test_skills import reply

from ii_pro_school import evolution_probe as probe
from ii_pro_team.evolution import deployment, evaluate


def call(server, path="", body=None):
    status, value = request(server, "/api/team/evolution" + path, body)
    assert status == 200, value
    return value


def test_paired_evolution_keeps_approvals_and_separates_decision_from_deployment(tmp_path, registry, monkeypatch):  # noqa: F811
    projects, _ = registry
    calls = mock_model(monkeypatch, [reply("[]"), reply("[1, 2, 3]"), reply("6"), reply("6")])
    root = tmp_path / "studio"
    with studio(root, projects) as server:
        state = call(server)
        assert not state["frozen"] and state["active"] is None
        plan = state["plan"]
        assert plan["order"] == ["window:a", "window:b", "fee:b", "fee:a"]
        assert request(server, "/api/team/evolution-freeze", {"sha256": plan["sha256"]}, trusted=False)[0] == 403
        assert request(server, "/api/team/evolution-prepare", {})[0] == 409
        assert request(server, "/api/team/evolution-freeze", {"sha256": "x"})[0] == 409
        assert call(server, "-freeze", {"sha256": plan["sha256"]})["frozen"]
        assert request(server, "/api/team/evolution-decision", {"operation": "accept"})[0] == 409
        job = call(server, "-prepare", {})
        assert job["approval"]["status"] == "pending" and not calls
        accepted(server, "decision", decision(job))
        settled(server, job["job"])
    with studio(root, projects) as server:
        assert call(server)["plan"] == plan
        for i in range(3):
            job = call(server, "-prepare", {})
            assert len(calls) == i + 1
            accepted(server, "decision", decision(job))
            settled(server, job["job"])
        state = call(server)
        assert state["evaluation"]["verdict"] == "supported" and state["evaluation"]["a"] == 1
        assert state["evaluation"]["b"] == 2 and state["active"] is None
        assert all(row["artifacts"] and row["input_bytes"] < 1470 for row in state["slots"])
        assert request(server, "/api/team/evolution-prepare", {})[0] == 409
        pending = call(server, "-decision", {"operation": "accept"})["pending_decision"]
        assert deployment(server.training.shared_core.store) is None
        assert request(server, "/api/team/evolution-decision", {"id": pending["id"], "sha256": "wrong", "confirm": True})[0] == 409
        payload = {"id": pending["id"], "sha256": pending["sha256"], "confirm": True}
        assert call(server, "-decision", payload)["active"] == probe.KEY
        assert request(server, "/api/team/evolution-decision", payload)[0] == 409
        assert not server.training.shared_core.memory.history("skill-compare-v1")
    with studio(root, projects) as server:
        assert call(server)["active"] == probe.KEY
        pending = call(server, "-decision", {"operation": "rollback"})["pending_decision"]
        state = call(server, "-decision", {"id": pending["id"], "sha256": pending["sha256"], "confirm": True})
        assert state["active"] is None and [item["operation"] for item in state["decisions"]] == ["accept", "rollback"]
    for i, j in ((0, 1), (3, 2)):
        a, b = (json.loads(calls[k]["messages"][1]["content"]) for k in (i, j))
        assert b.pop("changed_operations")
        assert a == b and "expected" not in a and not a["files"]
        assert calls[i]["format"] == calls[j]["format"] and calls[i]["options"] == calls[j]["options"]


def test_unknown_experiment_stays_inconclusive_and_cannot_be_repeated(tmp_path, registry, monkeypatch):  # noqa: F811
    projects, _ = registry
    calls = mock_model(monkeypatch, [RuntimeError("lost transport")])
    with studio(tmp_path / "studio", projects) as server:
        call(server, "-freeze", {"sha256": call(server)["plan"]["sha256"]})
        job = call(server, "-prepare", {})
        accepted(server, "decision", decision(job))
        assert settled(server, job["job"])["status"] == "unknown"
        assert call(server)["evaluation"]["verdict"] == "inconclusive"
        assert request(server, "/api/team/evolution-prepare", {})[0] == 409
        assert len(calls) == 1


def test_evaluation_rejects_compensating_regression_and_incomplete_evidence():
    plan = {"order": ["one:a", "one:b", "two:b", "two:a", "three:a", "three:b"],
            "criteria": probe.CRITERIA}
    rows = [{"case": case, "arm": arm, "status": "succeeded", "correct": score}
            for name, score in zip(plan["order"], [True, False, True, False, False, True], strict=True)
            for case, arm in [name.split(":")]]
    assert evaluate(plan, rows)["verdict"] == "not_supported"
    assert evaluate(plan, rows[1:])["verdict"] == "inconclusive"
    assert probe.structural_map("def bad(:", "42") is None
