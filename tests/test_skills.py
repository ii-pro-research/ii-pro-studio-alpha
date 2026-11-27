"""Skill selection, frozen comparison and memory provenance via the Studio API."""

import json

import pytest

from ii_pro_core.errors import ValidationError
from ii_pro_local.skills import BY_ID, CATALOG, changed_fragments, packet, verified_reference
from ii_pro_school.skill_pilot import plan
from test_local_engine_ui import accepted, decision, mock_model, registry, request, settled, studio  # noqa: F401


def reply(prediction):
    return {"tool": "finish", "plan": "Trace both expressions", "args": {
        "report": "Comparison complete", "evidence": [], "defect": "boundary",
        "comparison": "The branches differ.", "prediction": prediction,
        "distinguishing_test": "Check the input in the question.",
    }}


def test_registered_skills_are_versioned_and_require_both_fragments():
    assert len(CATALOG) == len({skill.key for skill in CATALOG}) == 7
    assert all(skill.evidence and skill.history and skill.tools == ("finish",) for skill in CATALOG)
    before, after = changed_fragments("def f():\n    return 1\n", "def f():\n    return 2\n")
    assert "return 1" in before and "return 2" in after
    with pytest.raises(ValidationError):
        packet("comparison", before, "", "a" * 64)
    with pytest.raises(ValidationError):
        packet("filter", before, "", "a" * 64, skill=BY_ID["compare"].reference())
    with pytest.raises(ValidationError):
        changed_fragments(before, before)


def test_pilot_is_frozen_approval_is_manual_and_arms_differ_only_by_skill(tmp_path, registry, monkeypatch):  # noqa: F811
    projects, _ = registry
    calls = mock_model(monkeypatch, [reply("False"), reply("True")])
    with studio(tmp_path / "studio", projects) as server:
        state = accepted(server, "state")
        assert [s["status"] for s in state["skills"]] == ["unreviewed"] * 7
        assert all(s["pass"] is None for s in state["skill_pilot"])
        assert request(server, "/api/local-engine/skill-pilot", {}, trusted=False)[0] == 403
        a = accepted(server, "skill-pilot", {})
        assert accepted(server, "skill-pilot", {})["approval"] == a["approval"]
        assert not calls and a["approval"]["status"] == "pending"
        agent = server.training.local_engine.agents["pricing"]
        assert plan(agent)["steps_per_slot"] == 1
        assert verified_reference(server.training.shared_core.memory, "compare") is None
        accepted(server, "decision", decision(a))
        settled(server, a["job"])
    with studio(tmp_path / "studio", projects) as server:
        b = accepted(server, "skill-pilot", {})
        assert b["job"] != a["job"] and len(calls) == 1
        accepted(server, "decision", decision(b))
        settled(server, b["job"])
        result = accepted(server, "state")
        assert [s["pass"] for s in result["skill_pilot"]] == [False, True, None, None]
        assert result["skill_pilot"][0]["error"] == "prediction_mismatch"
        assert result["skill_uses"][0]["skill"]["sha256"] == BY_ID["compare"].sha256
        assert [s["status"] for s in result["skills"]] == ["unreviewed"] * 7
    first, second = [json.loads(call["messages"][1]["content"]) for call in calls]
    assert second.pop("skill_guidance") == BY_ID["compare"].guidance
    assert first == second
    assert calls[0]["format"] == calls[1]["format"] and calls[0]["options"] == calls[1]["options"]
    assert "expected" not in first and not first["files"]


def test_unknown_skill_slot_is_not_repeated(tmp_path, registry, monkeypatch):  # noqa: F811
    projects, _ = registry
    calls = mock_model(monkeypatch, [RuntimeError("lost transport")])
    with studio(tmp_path / "studio", projects) as server:
        a = accepted(server, "skill-pilot", {})
        accepted(server, "decision", decision(a))
        assert settled(server, a["job"])["status"] == "unknown"
        assert request(server, "/api/local-engine/skill-pilot", {})[0] == 409
        assert len(calls) == 1
