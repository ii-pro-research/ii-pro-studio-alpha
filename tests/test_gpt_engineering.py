"""Real HTTP, Core approvals and official DevKit with mock fetch only."""

from contextlib import contextmanager
from dataclasses import replace
import json
from threading import Thread

import pytest
from test_devkit_bridge import MockBridge, require_devkit
from test_gpt_team import decision, wait
from test_gpt_text import grant
from test_local_agent import BUGGY, fixture_project
from test_local_engine_ui import (
    accepted,
    mock_model,
    request,
    settled,
)
from test_local_engine_ui import (
    decision as local_decision,
)
from test_local_engine_ui import (
    payload as local_payload,
)

from ii_pro_local.snapshot import Snapshot
from ii_pro_team import engineering
from ii_pro_team.devkit import DevkitTextAdapter
from ii_pro_ui.server import LocalServer
from ii_pro_ui.training import Training

BASE = "/api/team/"
JOB = "e" * 32
LOCAL_JOB = "a" * 32


@pytest.fixture
def lab(tmp_path):
    project = tmp_path / "project"
    _, revision = fixture_project(project)
    snapshot = Snapshot.register(
        project, base=revision, revision=revision, paths=["pricing.py"]
    )
    return {"pricing": ("Developer fixture", snapshot)}, project


@contextmanager
def studio(path, projects, mode="success", engineering_worker=None):
    require_devkit()
    server = LocalServer(0, None)
    server.training = Training(
        path,
        server.token,
        include_ollama=True,
        include_team=True,
        local_projects=projects,
        local_live=True,
        engineering_worker=engineering_worker,
        text_adapter_factory=lambda: DevkitTextAdapter(
            grant(), bridge=MockBridge(mode), mock=True
        ),
    )
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.03})
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(3)
        server.server_close()


def draft(phase, source=None, job=JOB):
    return {
        "project_id": "pricing",
        "request_id": job,
        "phase": phase,
        "source_job": source,
    }


def team(server, command, body=None):
    status, view = request(server, BASE + command, body)
    assert status == 200, view
    return view


def test_roles_approved_independently_over_http_and_restore(tmp_path, lab, monkeypatch):
    projects, source = lab
    state = tmp_path / "studio"
    calls = mock_model(
        monkeypatch,
        [
            {
                "tool": "read_file",
                "args": {"path": "pricing.py", "line": 1},
                "plan": "Inspect",
            },
            {
                "tool": "propose_patch",
                "args": {
                    "path": "pricing.py",
                    "before": "missing",
                    "after": "candidate",
                },
            },
            {
                "tool": "propose_patch",
                "args": {
                    "path": "pricing.py",
                    "before": "return price +",
                    "after": "return (",
                },
            },
        ],
    )
    with studio(state, projects) as server:
        payload = {
            **local_payload(LOCAL_JOB),
            "task": engineering.TASK,
            "requirements": engineering.CRITERIA,
        }
        for _ in range(3):
            step = accepted(server, "prepare", payload)
            accepted(server, "decision", local_decision(step))
            local = settled(server, LOCAL_JOB)
        assert local["handoff"]["recipient"] == "gpt_engineer"
        prep = draft("plan", LOCAL_JOB)
        assert request(server, BASE + "prepare", prep, trusted=False)[0] == 403
        first = team(server, "prepare", prep)["phases"][0]
        scope = first["text_operation"]["context"]
        assert (
            scope["source"] == {"pricing.py": BUGGY} and scope["architect_plan"] is None
        )
        assert scope["source_job"] == LOCAL_JOB and scope["execution_tools"] == []
        assert scope["observations"][0]["result"].startswith("pricing.py read")
        assert scope["observations"][1]["reason"] == "anchor_not_unique"
        assert scope["observations"][2]["result"] == "candidate rejected"
        assert not first["events"] and first["approval"]["status"] == "pending"
        assert (
            request(server, BASE + "advance", {"job_id": JOB, "phase": "implement"})[0]
            == 409
        )
        assert (
            request(
                server, BASE + "decision", {**decision(first), "spec_digest": "0" * 64}
            )[0]
            == 409
        )
        team(server, "decision", decision(first))
        first = wait(server.training, JOB)["phases"][0]
        assert first["outputs"]["engineering.json"]["plan"]["plan"]
        advance = {"job_id": JOB, "phase": "implement"}
        second = team(server, "advance", advance)["phases"][1]
        assert first["session_id"] != second["session_id"]
        assert first["approval"]["id"] != second["approval"]["id"]
        assert (
            second["text_operation"]["context"]["architect_plan"]["run"]
            == first["run_id"]
        )
        assert (
            request(
                server,
                BASE + "decision",
                {**decision(second), "approval_id": first["approval"]["id"]},
            )[0]
            == 409
        )
        approval = server.training.shared_core.approvals.describe(
            second["approval"]["id"]
        )
        assert (
            approval["action"]["parameters"]["inputs"]["context"]
            == second["text_operation"]["context"]
        )
        team(server, "decision", decision(second))
        final = wait(server.training, JOB)["phases"][1]
        candidate = final["outputs"]["engineering.json"]
        assert (
            candidate["state"] == "proposed"
            and candidate["candidate"]["syntax"] == "valid"
        )
        assert (
            candidate["functional_tests"] == "not_run"
            and not candidate["source_written"]
        )
        assert (
            final["outputs"]["result.json"]["request_body"]["instructions"]
            == second["text_operation"]["instructions"]
        )
        assert (
            final["outputs"]["result.json"]["request_body"]["input"][0]["content"]
            == second["text_operation"]["prompt"]
        )
        assert final["outputs"]["result.json"]["real_inference"] is False
        assert final["outputs"]["shadow-material.json"]["status"] == "unreviewed"
        assert (
            team(server, "decision", decision(second))["phases"][1]["run_id"]
            == second["run_id"]
        )
        assert request(server, "/api/school/training")[0] == 200
    with studio(state, projects) as server:
        restored = team(server, "state")["jobs"][0]["phases"]
        assert restored[1]["outputs"] == final["outputs"]
        assert restored[1]["approval"]["status"] == "consumed"
        assert (
            sum(
                1
                for item in restored[1]["events"]
                if item["agent_event"]["kind"] == "session"
            )
            == 1
        )
    assert len(calls) == 3 and (source / "pricing.py").read_text() == BUGGY


def test_unknown_engineer_cannot_be_replayed_or_passed_off_as_completed(tmp_path, lab):
    projects, _ = lab
    with studio(tmp_path / "studio", projects, mode="eof") as server:
        phase = team(server, "prepare", draft("implement"))["phases"][0]
        team(server, "decision", decision(phase))
        actual = wait(server.training, JOB)["phases"][0]
        assert (
            actual["status"] == "unknown" and actual["approval"]["status"] == "consumed"
        )
        assert actual["outputs"] == {}
        assert request(server, BASE + "decision", decision(phase))[0] == 409
        assert (
            request(server, BASE + "prepare", draft("implement", job="f" * 32))[0]
            == 409
        )


def test_unapproved_files_and_browser_supplied_context_rejected(tmp_path, lab):
    projects, _ = lab
    with studio(tmp_path / "valid", projects) as server:
        for extra in (
            {"context": {"source": {"private.py": "confidential"}}},
            {"text": "Private exam questions"},
            {"model": "other-model"},
            {"architect_plan": "untrusted"},
        ):
            assert (
                request(server, BASE + "prepare", {**draft("implement"), **extra})[0]
                == 409
            )
        assert team(server, "state")["jobs"] == []
    title, snap = projects["pricing"]
    wrong = replace(snap, files=(("pricing.py", BUGGY), ("private.py", "return 0")))
    with studio(tmp_path / "invalid", {"pricing": (title, wrong)}) as server:
        assert request(server, BASE + "prepare", draft("implement"))[0] == 409


@pytest.mark.parametrize(
    "answer",
    [
        '{"path":"private.py","before":"old","after":"new","explanation":"ok","checks":["check"]}',
        '{"path":"pricing.py","before":"old","after":"new","explanation":"ok","checks":["check"],"commands":["python"]}',
        '{"path":"pricing.py","path":"pricing.py","before":"old","after":"new","explanation":"ok","checks":["check"]}',
    ],
)
def test_invalid_proposal_has_no_computer_action(answer):
    result = engineering.assess({"role": "engineer"}, answer)
    assert result["state"] == "invalid_format"
    assert not result["source_written"] and result["functional_tests"] == "not_run"


def test_prepared_engineer_approval_cannot_cross_studio_session(tmp_path, lab):
    projects, _ = lab
    state = tmp_path / "studio"
    with studio(state, projects) as server:
        phase = team(server, "prepare", draft("implement"))["phases"][0]
        assert phase["approval"]["status"] == "pending"
    with studio(state, projects) as server:
        old = team(server, "state")["jobs"][0]["phases"][0]
        assert old["approval"]["status"] == "invalidated"
        assert request(server, BASE + "decision", decision(phase))[0] == 409
        renewed = team(server, "prepare", draft("implement"))["phases"][0]
        assert renewed["run_id"] == phase["run_id"]
        assert renewed["approval"]["id"] != phase["approval"]["id"]
        assert renewed["approval"]["status"] == "pending" and not renewed["events"]


def test_full_team_workflow_uses_separate_approvals_and_sealed_evidence(
    tmp_path, lab, monkeypatch
):
    projects, source = lab
    state = tmp_path / "studio"
    calls = mock_model(
        monkeypatch,
        [
            {
                "tool": "read_file",
                "args": {"path": "pricing.py", "line": 1},
                "plan": "Inspect the current function.",
            },
            {
                "tool": "finish",
                "args": {
                    "report": "The proposed source preserves the signature. No functional tests ran.",
                    "evidence": [1],
                },
            },
        ],
    )
    advance = {"job_id": JOB}
    with studio(state, projects) as server:
        plan = team(server, "prepare", draft("plan"))["phases"][0]
        assert (
            request(server, BASE + "advance", {**advance, "phase": "local"})[0] == 409
        )
        team(server, "decision", decision(plan))
        wait(server.training, JOB)
        implementation = team(server, "advance", {**advance, "phase": "implement"})[
            "phases"
        ][1]
        team(server, "decision", decision(implementation))
        wait(server.training, JOB)
        assert (
            request(server, BASE + "advance", {**advance, "phase": "review"})[0] == 409
        )
        assert (
            request(
                server, BASE + "advance", {**advance, "phase": "local"}, trusted=False
            )[0]
            == 403
        )
        step = team(server, "advance", {**advance, "phase": "local"})["team_local"]
        assert "return price -" in step["requirements"] and not calls
        assert step["approval"]["status"] == "pending"
        same = team(server, "advance", {**advance, "phase": "local"})["team_local"]
        assert same["approval"]["id"] == step["approval"]["id"]
        for index in range(2):
            command = local_decision(step)
            accepted(server, "decision", command)
            assert request(server, "/api/local-engine/decision", command)[0] == 409
            pending = settled(server, step["job"])
            if index == 0:
                assert pending["status"] == "ready_for_next_step"
                step = team(server, "advance", {**advance, "phase": "local"})[
                    "team_local"
                ]
        assert pending["status"] == "finished" and len(calls) == 2
        reviewed = team(server, "advance", {**advance, "phase": "review"})["phases"][2]
        assert reviewed["session_id"] != plan["session_id"]
        assert reviewed["approval"]["id"] not in {
            plan["approval"]["id"],
            implementation["approval"]["id"],
        }
        assert reviewed["text_operation"]["context"]["source_job"] == step["job"]
        payload = reviewed["text_operation"]["prompt"]
        assert implementation["run_id"] in payload and "local_finish_sha256" in payload
        assert "return price -" in payload
        assert (
            "The proposed source preserves" not in payload
        )  # Raw Local model text stays local.
        assert (
            request(
                server,
                BASE + "decision",
                {**decision(reviewed), "approval_id": implementation["approval"]["id"]},
            )[0]
            == 409
        )
        team(server, "decision", decision(reviewed))
        done = wait(server.training, JOB)
        report = done["team_report"]
        assert report["assessment"]["verdict"] == "accepted"
        assert (
            report["functional_tests"] == "not_run"
            and report["knowledge"] == "unreviewed"
        )
        assert (
            done["phases"][2]["outputs"]["shadow-material.json"]["status"]
            == "unreviewed"
        )
    with studio(state, projects) as server:
        recovered = team(server, "state")["jobs"][0]
        assert recovered["team_report"] == report
        assert recovered["team_local"]["status"] == "finished"
        assert request(server, "/api/school/training")[0] == 200
    assert (source / "pricing.py").read_text() == BUGGY


def test_cloud_quota_wait_preserves_unknown_and_needs_explicit_acknowledgement(
    tmp_path, lab
):
    projects, _ = lab
    state = tmp_path / "studio"
    with studio(state, projects, mode="limit") as server:
        pending = team(server, "prepare", draft("implement"))["phases"][0]
        team(server, "decision", decision(pending))
        rejected = wait(server.training, JOB)["phases"][0]
        assert rejected["status"] == "unknown"
        assert rejected["approval"]["status"] == "consumed"
    with studio(state, projects) as server:
        dashboard = team(server, "state")
        assert dashboard["cloud"] == {
            "status": "waiting_for_cloud_quota",
            "run_id": rejected["run_id"],
        }
        assert dashboard["jobs"][0]["phases"][0] == rejected
        assert request(server, BASE + "prepare", draft("plan", job="f" * 32))[0] == 409
        facade = server.training.team
        old = facade.core.runs.get(rejected["run_id"])
        facade.abandon_unknown(
            old.id, action_hash=old.action_hash, accept_unknown_cost=True, no_retry=True
        )
        assert request(server, BASE + "prepare", draft("plan", job="f" * 32))[0] == 409
        facade.acknowledge_cloud_quota(old.id, source="user_reported_chatgpt_reset")
        event = facade.store.one(
            "SELECT payload_json FROM events WHERE kind='team.cloud_quota_acknowledged'"
        )
        import json

        evidence = json.loads(event[0])
        assert evidence["source"] == "user_reported_chatgpt_reset"
        assert evidence["provider_verified"] is False
        assert team(server, "state")["cloud"]["status"] == "ready_to_prepare"
        assert request(server, BASE + "decision", decision(pending))[0] == 409
        assert facade.job(JOB)["phases"][0] == rejected
        fresh = team(server, "prepare", draft("plan", job="f" * 32))["phases"][0]
        assert fresh["approval"]["status"] == "pending" and not fresh["events"]


@pytest.mark.parametrize("mode", ["operator", "budget"])
def test_incomplete_team_attempt_can_be_reviewed_without_claiming_local_success(
    tmp_path, lab, monkeypatch, mode
):
    projects, _ = lab
    root = tmp_path / "studio"
    first = {"tool": "git_diff", "args": {"path": "pricing.py"}, "plan": "Inspect Git"}
    second = {"tool": "analyze_python", "args": {"path": "pricing.py"}}
    mock_model(
        monkeypatch, [first, second] if mode == "operator" else [first, *[second] * 5]
    )
    action = {"job_id": JOB}

    with studio(root, projects) as server:
        plan = team(server, "prepare", draft("plan"))["phases"][0]
        team(server, "decision", decision(plan))
        wait(server.training, JOB)
        eng = team(server, "advance", {**action, "phase": "implement"})["phases"][1]
        team(server, "decision", decision(eng))
        wait(server.training, JOB)
        for _ in range(2 if mode == "operator" else 6):
            current = team(server, "advance", {**action, "phase": "local"})[
                "team_local"
            ]
            accepted(server, "decision", local_decision(current))
            settled(server, current["job"])
        initial = team(server, "state")["jobs"][0]["team_local"]
        if mode == "operator":
            assert initial["status"] == "ready_for_next_step"
            assert (
                request(server, BASE + "advance", {**action, "phase": "review"})[0]
                == 409
            )
            assert (
                request(
                    server,
                    BASE + "advance",
                    {**action, "phase": "close_local"},
                    trusted=False,
                )[0]
                == 403
            )
            pending = team(server, "advance", {**action, "phase": "local"})[
                "team_local"
            ]
            not_started = local_decision(pending)
            closed = team(server, "advance", {**action, "phase": "close_local"})[
                "team_local"
            ]
            assert request(server, "/api/local-engine/decision", not_started)[0] == 409
            assert closed["runs"][-1]["status"] == "cancelled"
            assert closed["approval"]["status"] == "rejected"
            assert closed["runs"][:-1] == initial["runs"]
        else:
            closed = initial
            assert closed["status"] == "budget_exhausted"
        assert closed["attempt_outcome"] == "incomplete"
        assert closed["completion_reason"] == (
            "operator_closed" if mode == "operator" else "step_budget_exhausted"
        )
        assert request(server, BASE + "advance", {**action, "phase": "local"})[0] == 409
        assert closed["results"] == initial["results"]
        assert not any(
            step["result"].get("model_report") for step in closed["results"].values()
        )

    with studio(root, projects) as server:
        restored = team(server, "state")["jobs"][0]["team_local"]
        assert (
            restored["runs"] == closed["runs"]
            and restored["completion_reason"] == closed["completion_reason"]
        )
        assert request(server, BASE + "advance", {**action, "phase": "local"})[0] == 409
        review = team(server, "advance", {**action, "phase": "review"})["phases"][2]
        prompt = json.loads(review["text_operation"]["prompt"])
        attempt = prompt["review"]["local_attempt"]
        assert (
            attempt["outcome"] == "incomplete"
            and attempt["conclusion"] == "not_produced"
        )
        assert [s["run"] for s in attempt["steps"]] == [s["id"] for s in closed["runs"]]
        assert prompt["context"]["observations"][0]["result"] == "Git diff was empty"
        assert (
            prompt["context"]["observations"][1]["result"]
            == "Original Python syntax valid"
        )
        assert (
            "Do not invent a Local conclusion"
            in review["text_operation"]["instructions"]
        )
        assert not review["events"] and review["approval"]["status"] == "pending"
        team(server, "decision", decision(review))
        done = wait(server.training, JOB)
        report = done["team_report"]
        assert (
            report["local_outcome"] == "incomplete"
            and report["functional_tests"] == "not_run"
        )
        assert (
            report["assessment"]["assumptions"] == []
            and report["assessment"]["coaching"]
        )
        shadow = done["phases"][2]["outputs"]["shadow-material.json"]
        assert (
            shadow["status"] == "unreviewed" and shadow["verified_knowledge"] is False
        )


def test_one_review_followup_after_abandonment_restores_and_cannot_be_duplicated(
    tmp_path, lab, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor

    root = tmp_path / "studio"
    projects, _ = lab
    mock_model(
        monkeypatch,
        [
            {
                "tool": "read_file",
                "args": {"path": "pricing.py", "line": 1},
                "plan": "Inspect",
            },
        ],
    )
    action = {"job_id": JOB}
    followup = {**action, "phase": "review_followup"}

    def old_history(core, run_id):
        return tuple(
            tuple(tuple(row) for row in core.store.all(query, (run_id,)))
            for query in (
                "SELECT * FROM runs WHERE id=?",
                "SELECT * FROM approvals WHERE run_id=? ORDER BY rowid",
                "SELECT * FROM events WHERE run_id=? ORDER BY id",
            )
        )

    with studio(root, projects, mode="mentor_eof") as server:
        plan = team(server, "prepare", draft("plan"))["phases"][0]
        team(server, "decision", decision(plan))
        wait(server.training, JOB)
        eng = team(server, "advance", {**action, "phase": "implement"})["phases"][1]
        team(server, "decision", decision(eng))
        wait(server.training, JOB)
        step = team(server, "advance", {**action, "phase": "local"})["team_local"]
        accepted(server, "decision", local_decision(step))
        settled(server, step["job"])
        team(server, "advance", {**action, "phase": "close_local"})
        assert request(server, BASE + "advance", followup)[0] == 409
        review = team(server, "advance", {**action, "phase": "review"})["phases"][2]
        assert request(server, BASE + "advance", followup)[0] == 409
        team(server, "decision", decision(review))
        rejected = wait(server.training, JOB)["phases"][2]
        assert (
            rejected["status"] == "unknown"
            and rejected["approval"]["status"] == "consumed"
        )
        assert request(server, BASE + "advance", followup)[0] == 409
        core = server.training.shared_core
        before = old_history(core, review["run_id"])
        old = core.runs.get(review["run_id"])
        server.training.team.abandon_unknown(
            old.id, action_hash=old.action_hash, accept_unknown_cost=True, no_retry=True
        )
        assert old_history(core, old.id) == before
        assert team(server, "state")["jobs"][0]["review_followup_available"]

    with studio(root, projects) as server:
        assert team(server, "state")["jobs"][0]["review_followup_available"]
        assert request(server, BASE + "advance", followup, trusted=False)[0] == 403
        with ThreadPoolExecutor(max_workers=3) as pool:
            views = list(
                pool.map(lambda _: team(server, "advance", followup), range(3))
            )
        assert len({v["phases"][-1]["run_id"] for v in views}) == 1
        assert len({v["phases"][-1]["approval"]["id"] for v in views}) == 1
        job = views[-1]
        first, new = job["phases"][-2:]
        assert [p["phase"] for p in job["phases"]] == [
            "plan",
            "implement",
            "review",
            "review",
        ]
        assert first["abandoned_unknown"] and first["status"] == "unknown"
        assert (
            new["previous_review_run"] == first["run_id"]
            and new["session_id"] != first["session_id"]
        )
        assert new["approval"]["spec_digest"] != first["approval"]["spec_digest"]
        assert new["text_operation"]["prompt"] == first["text_operation"]["prompt"]
        assert not job["review_followup_available"] and not new["events"]
        assert (
            request(
                server,
                BASE + "decision",
                {**decision(new), "approval_id": first["approval"]["id"]},
            )[0]
            == 409
        )
        assert request(server, BASE + "decision", decision(first))[0] == 409

    with studio(root, projects) as server:
        job = team(server, "state")["jobs"][0]
        assert (
            len(job["phases"]) == 4
            and job["phases"][-1]["approval"]["status"] == "invalidated"
        )
        renewed = team(server, "advance", followup)["phases"][-1]
        assert (
            renewed["run_id"] == new["run_id"]
            and renewed["approval"]["id"] != new["approval"]["id"]
        )
        team(server, "decision", decision(renewed))
        done = wait(server.training, JOB)
        assert (
            done["phases"][-1]["outputs"]["shadow-material.json"]["status"]
            == "unreviewed"
        )
        assert done["team_report"]["phases"]["review"] == new["run_id"]
        assert (
            team(server, "advance", followup)["phases"][-1]["run_id"] == new["run_id"]
        )
        assert old_history(server.training.shared_core, first["run_id"]) == before
