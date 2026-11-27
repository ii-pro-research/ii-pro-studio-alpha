import copy
import json
import sqlite3

import pytest
from test_local_engine_ui import (  # noqa: F401
    BASE,
    accepted,
    decision,
    mock_model,
    registry,
    request,
    settled,
    studio,
)

from ii_pro_core.errors import ApprovalRequired, RecoveryRequired, ValidationError
from ii_pro_local import table_probe as table
from ii_pro_local.broker import proposal, proposal_schema
from ii_pro_school.adapters import StudentFailure


def query():
    return {"dataset": table.DATASET,
            "filters": [{"field": "active", "op": "eq", "value": 1},
                        {"field": "score", "op": "gte", "value": 7}],
            "sort": [{"field": "zone", "direction": "asc"}, {"field": "score", "direction": "desc"},
                     {"field": "id", "direction": "asc"}]}


def test_bounded_table_filters_sorts_and_handles_null():
    args = query()
    expected = {"dataset": table.DATASET, "dataset_sha256": table.DATASET_SHA256,
                "ids": [28, 45, 13, 62], "count": 4}
    assert table.evaluate(args) == expected
    assert table.evaluate(args, rows=tuple(reversed(table.ROWS)))["ids"] == expected["ids"]
    args["filters"] = [{"field": "score", "op": "eq", "value": 99}]
    assert table.evaluate(args)["ids"] == []
    args["filters"] = []
    args["sort"] = [{"field": "score", "direction": "asc"}]
    assert table.evaluate(args)["ids"][-1] == 77
    args["sort"][0]["direction"] = "desc"
    assert table.evaluate(args)["ids"][-1] == 77


@pytest.mark.parametrize("update", [
    {"dataset": "../items"}, {"sql": "SELECT 1"},
    {"filters": [{"field": "zone", "op": "eq", "value": 1}]},
    {"filters": [{"field": "active", "op": "select", "value": 1}]},
    {"filters": [{"field": "score", "op": "gte", "value": True}]},
    {"filters": [{"field": "score", "op": "eq", "value": None}]},
    {"filters": [{"field": "score", "op": "eq", "value": 101}]},
    {"sort": []}, {"sort": [{"field": "id", "direction": "random"}]},
    {"sort": [{"field": "id", "direction": "asc"}] * 2},
])
def test_table_rejects_unbounded_arguments(update):
    with pytest.raises(ValidationError):
        table.evaluate({**query(), **update})


def test_table_rejects_oversize_rows_and_non_ascii():
    with pytest.raises(ValidationError):
        table.evaluate(query(), rows=table.ROWS * 2)
    with pytest.raises(ValidationError):
        table.evaluate(query(), rows=((1, 1, 5, "é"),))


def test_probe_schema_scopes_two_tools_to_their_steps(registry):  # noqa: F811
    snap = registry[0]["pricing"][1]
    assert proposal_schema(1, snap, [], table=True)["oneOf"][0]["properties"]["tool"]["enum"] == ["table_check"]
    assert proposal_schema(2, snap, [1], inline_review=True, table=True)["oneOf"][0]["properties"]["tool"]["enum"] == ["finish"]
    assert all(branch["properties"]["tool"]["enum"] != ["table_check"]
               for branch in proposal_schema(1, snap, [])["oneOf"])
    with pytest.raises(ValidationError):
        proposal(json.dumps({"tool": "table_check", "args": query(), "plan": "x"}), 1)
    with pytest.raises(ValidationError):
        proposal(json.dumps({"tool": "table_check", "args": query()}), 2, table=True)


def body():
    return {"project": "pricing", "job": table.JOB, "task": table.TASK, "requirements": table.REQUIREMENTS}


def enable(server):
    accepted(server, "state")
    server.training.local_engine.table_probe_project = "pricing"


def test_studio_bound_table_probe_preserves_observation_and_approvals(tmp_path, registry, monkeypatch):  # noqa: F811
    hypothesis = query()
    hypothesis["filters"][1]["value"] = 9  # Deliberately different from the task.
    finish = {"tool": "finish", "args": {"report": "Threshold was too high.", "evidence": [1],
              "defect": "Threshold mismatch", "comparison": "Use seven", "prediction": "28,45,13,62",
              "distinguishing_test": "Include ID 45"}}
    calls = mock_model(monkeypatch, [{"tool": "table_check", "args": hypothesis, "plan": "Test filters."}, finish])
    with studio(tmp_path / "state", registry[0]) as server:
        assert request(server, BASE + "prepare", body())[0] == 409
        enable(server)
        first = accepted(server, "prepare", body())
        approval = first["approval"]
        core = server.training.shared_core
        with pytest.raises(ApprovalRequired):
            core.runs.execute(approval["run"], approval_id=approval["id"])
        assert calls == []
        accepted(server, "decision", decision(first))
        observed = settled(server, table.JOB)
        assert observed["results"]["1"]["result"]["ids"] == [28, 13, 62]
        assert observed["results"]["1"]["parameters"] == hypothesis
        core.artifacts.validate_run(approval["run"], require_any=True)
        second = accepted(server, "prepare", body())
        assert second["approval"]["id"] != approval["id"]
        assert request(server, BASE + "decision", decision(first))[0] == 409
        accepted(server, "decision", decision(second))
        final = settled(server, table.JOB)
        assert final["status"] == "finished"
        assert final["results"]["2"]["result"]["evidence"] == [1]
        assert request(server, BASE + "prepare", body())[0] == 409
        assert len(calls) == 2
        sent = json.loads(calls[1]["messages"][1]["content"])
        assert json.loads(sent["last_observation"])["ids"] == [28, 13, 62]
        assert sent["prior_parameters"] == hypothesis
        assert set(sent["observed_steps"]) == {1}
        assert "verified_reference_data" not in sent


def test_unknown_probe_cannot_replay_or_advance(tmp_path, registry, monkeypatch):  # noqa: F811
    calls = mock_model(monkeypatch, [StudentFailure("timeout")])
    with studio(tmp_path / "state", registry[0]) as server:
        enable(server)
        first = accepted(server, "prepare", body())
        permit = first["approval"]
        accepted(server, "decision", decision(first))
        final = settled(server, table.JOB)
        assert final["status"] == "unknown"
        assert request(server, BASE + "prepare", body())[0] == 409
        with pytest.raises(RecoveryRequired):
            server.training.shared_core.runs.execute(permit["run"], approval_id=permit["id"])
        assert len(calls) == 1


def test_rejected_probe_has_no_second_tool_call(tmp_path, registry, monkeypatch):  # noqa: F811
    wrong = copy.deepcopy(query())
    wrong["dataset"] = "unknown"
    calls = mock_model(monkeypatch, [{"tool": "table_check", "args": wrong, "plan": "Test filters."}])
    with studio(tmp_path / "state", registry[0]) as server:
        enable(server)
        first = accepted(server, "prepare", body())
        accepted(server, "decision", decision(first))
        final = settled(server, table.JOB)
        assert final["status"] == "budget_exhausted"
        assert final["results"]["1"]["status"] == "rejected"
        assert request(server, BASE + "prepare", body())[0] == 409
        assert len(calls) == 1


def grounded_body():
    return {"project": "pricing", "job": table.GROUNDED_JOB, "task": table.GROUNDED_TASK,
            "requirements": table.GROUNDED_REQUIREMENTS}


@pytest.mark.parametrize(("prediction", "comparison", "status"), [
    ("[64,72,19,84,23]", "confirmed", "observation_matched"),
    ("[72,64,19,84,23]", "confirmed", "observation_mismatch"),
    ("[64,72,19,84]", "confirmed", "observation_mismatch"),
    ("[64,72,19,84,23,55]", "confirmed", "observation_mismatch"),
    ("[64,72,19,84,23]", "revise: wrong threshold", "revision_requested"),
    ("[64,72,19,84,23]", "mismatch", "interpretation_missing"),
    ("[64,72,19,84,23]", "revise:", "interpretation_missing"),
    ("[64,72,19,84,23,]", "confirmed", "observation_mismatch"),
])
def test_grounded_finish_checks_count_order_and_interpretation(prediction, comparison, status):
    observation = {"ids": [64, 72, 19, 84, 23], "count": 5}
    assert table.assess_finish({"prediction": prediction, "comparison": comparison}, observation)["status"] == status


@pytest.mark.parametrize("tamper", [False, True])
def test_grounded_probe_binds_receipt_and_keeps_semantic_failure_separate(
    tmp_path, registry, monkeypatch, tamper,  # noqa: F811
):
    hypothesis = query()
    hypothesis.update(dataset=table.GROUNDED_DATASET, filters=[{"field": "active", "op": "eq", "value": 0}])
    # The instrument is correct for this bad hypothesis. It must not silently apply the missing threshold.
    finish = {"tool": "finish", "args": {"report": "The tool used the wrong filter.", "evidence": [1],
              "defect": "Threshold missing", "comparison": "revise: threshold missing",
              "prediction": "[64,72,19]", "distinguishing_test": "Check ID 55"}}
    calls = mock_model(monkeypatch, [{"tool": "table_check", "args": hypothesis, "plan": "Test filters."}, finish])
    with studio(tmp_path / "state", registry[0]) as server:
        enable(server)
        assert request(server, BASE + "prepare", grounded_body())[0] == 409
        server.training.local_engine.table_probe_job = table.GROUNDED_JOB
        first = accepted(server, "prepare", grounded_body())
        accepted(server, "decision", decision(first))
        observed = settled(server, table.GROUNDED_JOB)["results"]["1"]["result"]
        assert 55 in observed["ids"] and 47 in observed["ids"]
        core = server.training.shared_core
        artifact, = [a for a in core.artifacts.list(first["approval"]["run"])
                     if a.relative_path.endswith("/tool-result.json")]
        if tamper:
            with pytest.raises(sqlite3.IntegrityError, match="artifacts are sealed"), core.store.transaction() as conn:
                conn.execute("UPDATE artifact_blobs SET content=? WHERE artifact_id=?", (b"{}", artifact.id))
            assert len(calls) == 1
            return
        second = accepted(server, "prepare", grounded_body())
        with pytest.raises(ApprovalRequired):
            core.runs.execute(second["approval"]["run"], approval_id=second["approval"]["id"])
        accepted(server, "decision", decision(second))
        final = settled(server, table.GROUNDED_JOB)
        sent = json.loads(calls[1]["messages"][1]["content"])
        frozen = json.loads(second["approval"]["operation"]["description"])
        assert frozen["observation_ref"] == {"run": first["approval"]["run"], "sha256": artifact.sha256}
        assert json.loads(sent["last_observation"]) == observed
        assert final["runs"][-1]["status"] == "succeeded"
        assert final["results"]["2"]["grounding"]["status"] == "observation_mismatch"
        assert final["results"]["2"]["result"]["prediction"] == "[64,72,19]"
