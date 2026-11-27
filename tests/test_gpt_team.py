from concurrent.futures import ThreadPoolExecutor
import io
import json
import threading
import time

import pytest

from ii_pro_core.errors import (
    ApprovalRequired,
    Conflict,
    RecoveryRequired,
    ValidationError,
)
from ii_pro_team.adapters import MockAgentAdapter
from ii_pro_team.app_server import (
    AppServerClient,
    CodexAgentAdapter,
    StdioTransport,
    TransportLost,
)
from ii_pro_team.workspace import BEFORE, AFTER
from ii_pro_ui.training import Training


def training(tmp_path, factory=MockAgentAdapter, session="first"):
    t = Training(
        tmp_path / "state",
        session,
        include_ollama=True,
        include_team=True,
        team_adapter_factory=factory,
    )
    t._open()
    return t


def preparation(
    key="a", text="Исправить лабораторный slug_words и проверить изменения"
):
    return {"request_id": key * 32, "project_id": "studio-lab", "text": text}


def decision(phase, allow=True):
    return {
        "run_id": phase["run_id"],
        "approval_id": phase["approval"]["id"],
        "spec_digest": phase["approval"]["spec_digest"],
        "allow": allow,
    }


def wait(t, job="a" * 32):
    for _ in range(400):
        with t.lock:
            if not t.threads:
                return t.team.job(job)
        time.sleep(0.01)
    raise AssertionError("Team did not finish")


def run_phase(t, job):
    t.team.decide(decision(job["phases"][-1]))
    return wait(t, job["job_id"])


def test_fixed_team_full_route_git_evidence_and_shadow(tmp_path):
    t = training(tmp_path)
    try:
        job = t.team.prepare(preparation())
        assert job["phases"][0]["status"] == "created"
        assert not t.team.executors["architect"].adapter.calls
        job = run_phase(t, job)
        assert job["phases"][0]["status"] == "succeeded"
        job = run_phase(
            t, t.team.advance({"job_id": job["job_id"], "phase": "implement"})
        )
        engineer = job["phases"][-1]["outputs"]["result.json"]
        assert len(engineer["tests"]) == 4 and all(
            c["passed"] for c in engineer["tests"]
        )
        assert engineer["git_before"] != engineer["git_after"]
        assert "lower()" in engineer["diff"] and not engineer["workspace_code_executed"]
        job = run_phase(t, t.team.advance({"job_id": job["job_id"], "phase": "review"}))
        outputs = job["phases"][-1]["outputs"]
        experience, shadow = outputs["experience.json"], outputs["shadow-task.json"]
        assert (
            experience["status"] == "verified_fixture"
            and not experience["verified_knowledge"]
        )
        assert experience["origin"] == "mock_developer_fixture"
        assert experience["artifacts"] and not experience["user_task_completed"]
        assert not shadow["model_called"] and not shadow["teacher_solution_visible"]
        assert job["phases"][0]["session_id"] == job["phases"][2]["session_id"]
        assert job["phases"][0]["session_id"] != job["phases"][1]["session_id"]
        lab = t.team.laboratory
        assert (
            lab.area(job["job_id"], "qwen") / "solution.py"
        ).read_bytes() == BEFORE.encode()
        assert (
            lab.area(job["job_id"], "engineer") / "solution.py"
        ).read_bytes() == AFTER.encode()
        for phase in job["phases"]:
            assert phase["events"] and phase["artifacts"]
            assert phase["approval"]["status"] == "consumed"
            t.school.core.artifacts.validate_run(phase["run_id"], require_any=True)
        assert not t.school.store.all("SELECT 1 FROM school_responses")
        from ii_pro_ui.reader import Source, read_source

        snapshot = read_source(Source("team", "Team", t.root), tmp_path / "snapshots")
        team_runs = [r for r in snapshot["runs"] if r["executor"].startswith("team_")]
        assert len(team_runs) == 3
        assert all(
            "Mock Adapter" in r["backend"] and not r["real_inference"]
            for r in team_runs
        )
    finally:
        t.close()


def test_concurrent_prepare_and_decision_are_idempotent(tmp_path):
    t = training(tmp_path)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: t.team.prepare(preparation()), range(4)))
        ids = {j["phases"][0]["run_id"] for j in results}
        assert len(ids) == 1
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(
                pool.map(
                    lambda _: t.team.decide(decision(results[0]["phases"][0])), range(4)
                )
            )
        wait(t)
        assert t.team.executors["architect"].adapter.calls == 1
        assert len(t.team.state()["jobs"]) == 1
    finally:
        t.close()


def test_required_approval_and_cross_run_binding(tmp_path):
    t = training(tmp_path)
    try:
        a = t.team.prepare(preparation())["phases"][0]
        b = t.team.prepare(preparation("b"))["phases"][0]
        with pytest.raises(ApprovalRequired):
            t.school.core.runs.execute(a["run_id"])
        wrong = decision(a)
        wrong["approval_id"] = b["approval"]["id"]
        with pytest.raises(Conflict):
            t.team.decide(wrong)
        assert t.team.executors["architect"].adapter.calls == 0
    finally:
        t.close()


@pytest.mark.parametrize("action", ["reject", "revoke"])
def test_rejected_and_revoked_permissions_cannot_execute(tmp_path, action):
    t = training(tmp_path)
    try:
        phase = t.team.prepare(preparation())["phases"][0]
        if action == "reject":
            t.team.decide(decision(phase, False))
        else:
            body = decision(phase)
            body.pop("allow")
            t.team.revoke(body)
        with pytest.raises(Conflict):
            t.team.decide(decision(phase))
        assert t.team.executors["architect"].adapter.calls == 0
    finally:
        t.close()


def test_changed_adapter_invalidates_approval(tmp_path):
    t = training(tmp_path)
    try:
        phase = t.team.prepare(preparation())["phases"][0]
        t.team.executors["architect"].adapter.fault = "disconnect"
        with pytest.raises(Conflict):
            t.team.decide(decision(phase))
        assert t.team.executors["architect"].adapter.calls == 0
    finally:
        t.close()


@pytest.mark.parametrize("fault", ["disconnect", "permission"])
def test_unconfirmed_provider_blocks_replay_and_retains_events(tmp_path, fault):
    t = training(tmp_path, lambda lab: MockAgentAdapter(lab, fault=fault))
    phase = t.team.prepare(preparation())["phases"][0]
    try:
        result = run_phase(t, t.team.job("a" * 32))["phases"][0]
        assert result["status"] == "unknown" and result["events"]
        assert not result["outputs"]
        with pytest.raises(RecoveryRequired):
            t.team.decide(decision(phase))
        with pytest.raises(RecoveryRequired):
            t.team.prepare(preparation("b"))
        if fault == "permission":
            assert any(
                e["agent_event"]["kind"] == "approval_request" for e in result["events"]
            )
    finally:
        t.close()
    t = training(tmp_path, session="new")
    try:
        assert t.team.state()["jobs"][0]["phases"][0]["status"] == "unknown"
        assert t.team.executors["architect"].adapter.calls == 0
        with pytest.raises(RecoveryRequired):
            t.team.advance({"job_id": "a" * 32, "phase": "implement"})
    finally:
        t.close()


def test_restart_prepared_phase_requires_fresh_session_approval(tmp_path):
    t = training(tmp_path)
    phase = t.team.prepare(preparation())["phases"][0]
    t.close()
    t = training(tmp_path, session="new")
    try:
        with pytest.raises(Conflict):
            t.team.decide(decision(phase))
        restored = t.team.prepare(preparation())["phases"][0]
        assert restored["run_id"] == phase["run_id"]
        assert restored["approval"]["id"] != phase["approval"]["id"]
        assert run_phase(t, t.team.job("a" * 32))["phases"][0]["status"] == "succeeded"
    finally:
        t.close()


def test_request_id_cannot_change_task_or_project(tmp_path):
    t = training(tmp_path)
    try:
        t.team.prepare(preparation())
        with pytest.raises(Conflict):
            t.team.prepare(preparation(text="Другая задача"))
        for project in ("D:/Other", "../", "production"):
            with pytest.raises(ValidationError):
                t.team.prepare({**preparation("b"), "project_id": project})
        with pytest.raises(ValidationError):
            t.team.prepare({**preparation("b"), "command": "ignored"})
        with pytest.raises(ValidationError):
            t.team.prepare(preparation("b", "secret=never-store-this"))
    finally:
        t.close()


def test_modified_workspace_cannot_be_certified_as_experience(tmp_path):
    t = training(tmp_path)
    try:
        job = run_phase(t, t.team.prepare(preparation()))
        job = run_phase(
            t, t.team.advance({"job_id": job["job_id"], "phase": "implement"})
        )
        directory = t.team.laboratory.area(job["job_id"], "engineer")
        (directory / "solution.py").write_text("raise RuntimeError('never execute')")
        job = run_phase(t, t.team.advance({"job_id": job["job_id"], "phase": "review"}))
        assert job["phases"][-1]["status"] == "unknown"
        assert not job["phases"][-1]["outputs"]
        assert not t.school.store.all(
            "SELECT 1 FROM artifacts WHERE relative_path LIKE '%experience.json'"
        )
    finally:
        t.close()


class WireFixture:
    def __init__(self, frames):
        self.frames, self.sent = list(frames), []

    def send(self, value):
        self.sent.append(value)

    def receive(self, timeout):
        if not self.frames:
            raise TransportLost("fixture disconnected")
        return self.frames.pop(0)


def test_app_server_initialize_and_safe_auth_without_tokens():
    wire = WireFixture(
        [
            {"id": 1, "result": {"userAgent": "fixture"}},
            {
                "id": 2,
                "result": {
                    "account": {"type": "chatgpt", "email": "private@example.invalid"},
                    "requiresOpenaiAuth": True,
                },
            },
        ]
    )
    client = AppServerClient(wire)
    client.initialize()
    result = client.auth_status()
    assert result["authenticated"] and result["mode"] == "chatgpt"
    assert "email" not in result and "private" not in json.dumps(result)
    assert wire.sent[1]["method"] == "initialized"
    assert wire.sent[2]["params"] == {"refreshToken": False}


def test_app_server_turn_events_tools_declined_and_no_reasoning():
    wire = WireFixture(
        [
            {"id": 1, "result": {"thread": {"id": "thr-a"}}},
            {"id": 2, "result": {"turn": {"id": "turn-a"}}},
            {
                "method": "item/reasoning/textDelta",
                "params": {"threadId": "thr-a", "delta": "hidden"},
            },
            {
                "id": "tool1",
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "thr-a", "command": "private"},
            },
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "thr-a",
                    "turnId": "turn-a",
                    "delta": "Public answer",
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thr-a",
                    "turn": {"id": "turn-a", "status": "completed"},
                },
            },
        ]
    )
    received = []
    adapter = CodexAgentAdapter(
        AppServerClient(wire), "fixture-cwd", "selected-model", isolation_verified=True
    )
    result = adapter.run(
        {
            "role": "architect",
            "session_id": "a" * 32,
            "text": "Public task",
            "inputs": {},
        },
        received.append,
    )
    assert result["text"] == "Public answer"
    assert not any("hidden" in json.dumps(e) for e in received)
    assert any(e["kind"] == "approval_request" for e in received)
    assert {"id": "tool1", "result": {"decision": "decline"}} in wire.sent
    assert wire.sent[0]["params"]["sandbox"] == "read-only"
    assert wire.sent[0]["params"]["approvalsReviewer"] == "user"


def test_app_server_resume_and_recovery_never_replays_turn():
    wire = WireFixture([{"id": 1, "result": {"thread": {"id": "saved"}}}])
    client = AppServerClient(wire)
    assert client.recover("saved")["thread"]["id"] == "saved"
    assert wire.sent == [
        {
            "id": 1,
            "method": "thread/read",
            "params": {"threadId": "saved", "includeTurns": True},
        }
    ]
    with pytest.raises(ValidationError):
        client.call("account/login/start", {"type": "chatgptAuthTokens"})
    assert len(wire.sent) == 1


@pytest.mark.parametrize("kind", ["malformed", "oversized", "eof"])
def test_stdio_transport_rejects_bad_frames(kind):
    raw = {
        "malformed": b'{"a":1,"a":2}\n',
        "oversized": b"x" * 65537 + b"\n",
        "eof": b"",
    }[kind]
    transport = StdioTransport(io.BytesIO(raw), io.BytesIO())
    with pytest.raises(TransportLost):
        transport.receive(0.5)


def test_real_launcher_and_adapter_are_disabled_without_verified_environment(tmp_path):
    with pytest.raises(Conflict):
        StdioTransport.launch("never-run", tmp_path, approved=True)
    adapter = CodexAgentAdapter(AppServerClient(WireFixture([])), tmp_path, "model")
    with pytest.raises(Conflict):
        adapter.run({}, lambda _: None)


def test_stop_is_a_signal_not_proof_of_termination(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class IgnoringMock(MockAgentAdapter):
        def run(self, request, emit, cancellation=None):
            entered.set()
            assert release.wait(5)
            assert cancellation.requested
            return super().run(request, emit, None)

    t = training(tmp_path, IgnoringMock)
    try:
        phase = t.team.prepare(preparation())["phases"][0]
        t.team.decide(decision(phase))
        assert entered.wait(3)
        observed = t.team.stop({"run_id": phase["run_id"]})["phases"][0]
        assert observed["status"] == "running" and not observed["execution_stopped"]
        release.set()
        assert wait(t)["phases"][0]["status"] == "succeeded"
    finally:
        release.set()
        t.close()


def test_api_auth_csrf_strict_schema_and_history(tmp_path):
    from ii_pro_ui.server import LocalServer
    from test_school_v03_ui import request

    class Observer:
        def state(self):
            return {"read_only": True}

    server = LocalServer(0, Observer(), token="capability")
    server.training = training(tmp_path)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        assert request(server, "/api/team/state")[1]["jobs"] == []
        for headers in (
            {"Origin": None},
            {"Origin": "https://other.invalid"},
            {"X-CSRF-Token": "wrong"},
            {"Authorization": None},
            {"Sec-Fetch-Site": "cross-site"},
            {"Host": "other.invalid"},
        ):
            assert (
                request(server, "/api/team/prepare", preparation(), headers=headers)[0]
                == 403
            )
        assert server.training.team.state()["jobs"] == []
        assert (
            request(
                server, "/api/team/prepare", {**preparation(), "shell": "forbidden"}
            )[0]
            == 409
        )
        code, job = request(server, "/api/team/prepare", preparation())
        assert code == 200
        assert (
            request(server, "/api/team/decision", decision(job["phases"][0]))[0] == 200
        )
        result = wait(server.training)
        assert result["phases"][0]["status"] == "succeeded"
        state = request(server, "/api/team/state")[1]
        assert (
            state["jobs"][0]["job_id"] == job["job_id"] and not state["real_connected"]
        )
        assert str(tmp_path) not in json.dumps(state)
        for _ in range(10):
            assert request(server, "/api/team/connect", {"model": "any"})[0] == 405
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_after_edit_disconnect_preserves_git_and_no_automatic_repeat(tmp_path):
    t = training(tmp_path)
    try:
        job = run_phase(t, t.team.prepare(preparation()))
        t.team.executors["engineer"].adapter.fault = "after_edit"
        job = run_phase(
            t, t.team.advance({"job_id": job["job_id"], "phase": "implement"})
        )
        phase = job["phases"][-1]
        assert phase["status"] == "unknown" and not phase["outputs"]
        area = t.team.laboratory.area(job["job_id"], "engineer")
        assert (area / ".git").is_dir() and (
            area / "solution.py"
        ).read_bytes() == AFTER.encode()
        with pytest.raises(RecoveryRequired):
            t.team.decide(decision(phase))
        assert t.team.executors["engineer"].adapter.calls == 1
    finally:
        t.close()


def test_team_crash_fixture_recovers_unknown_without_provider_call(tmp_path):
    t = training(tmp_path)
    phase = t.team.prepare(preparation())["phases"][0]
    with t.school.store.transaction() as db:
        run = t.school.core.runs.get(phase["run_id"])
        db.execute(
            "UPDATE runs SET status='running',effect_status='unknown',execution_stopped=0 WHERE id=?",
            (run.id,),
        )
        db.execute("UPDATE tasks SET status='running' WHERE id=?", (run.task_id,))
    t.close()
    t = training(tmp_path, session="restart")
    try:
        assert t.team.state()["jobs"][0]["phases"][0]["status"] == "unknown"
        assert t.team.executors["architect"].adapter.calls == 0
        with pytest.raises(RecoveryRequired):
            t.team.prepare(preparation("b"))
    finally:
        t.close()


def test_app_server_interruption_and_resume_of_exact_context():
    class Signal:
        requested = True

    wire = WireFixture(
        [
            {"id": 1, "result": {"thread": {"id": "saved-architect"}}},
            {"id": 2, "result": {"turn": {"id": "turn-one"}}},
            {"id": 3, "result": {}},
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "saved-architect",
                    "turn": {"id": "turn-one", "status": "interrupted"},
                },
            },
        ]
    )
    adapter = CodexAgentAdapter(
        AppServerClient(wire), "fixture", "model", isolation_verified=True
    )
    with pytest.raises(TransportLost):
        adapter.run(
            {
                "role": "architect",
                "session_id": "a" * 32,
                "text": "Task",
                "inputs": {"provider_thread_id": "saved-architect"},
            },
            lambda _: None,
            Signal(),
        )
    assert wire.sent[0]["method"] == "thread/resume"
    assert wire.sent[2] == {
        "id": 3,
        "method": "turn/interrupt",
        "params": {"threadId": "saved-architect", "turnId": "turn-one"},
    }
    assert len([r for r in wire.sent if r.get("method") == "turn/start"]) == 1


@pytest.mark.parametrize(
    "result", [{"error": {"code": 429, "message": "private"}}, {"result": {}}, {}]
)
def test_app_server_errors_and_malformed_frames_never_replay(result):
    wire = WireFixture([{"id": 1, **result}])
    client = AppServerClient(wire)
    with pytest.raises(TransportLost):
        client.recover("saved")
    assert len(wire.sent) == 1


def test_orphan_preparation_is_completed_without_a_duplicate_task(tmp_path):
    t = training(tmp_path)
    try:
        original = t.school.core.runs.create

        def fail(*args, **kwargs):
            raise OSError("fixture before Run creation")

        t.school.core.runs.create = fail
        with pytest.raises(OSError):
            t.team.prepare(preparation())
        assert t.team.state()["jobs"][0]["phases"] == []
        t.school.core.runs.create = original
        t.close()
        t = training(tmp_path, session="restart-orphan")
        assert t.team.state()["jobs"][0]["phases"] == []
        job = t.team.prepare(preparation())
        assert len(t.team._requests()) == 1 and len(job["phases"]) == 1
        assert t.team.executors["architect"].adapter.calls == 0
    finally:
        t.close()


def test_no_engineer_dispatch_before_confirmed_plan(tmp_path):
    t = training(tmp_path)
    try:
        t.team.prepare(preparation())
        with pytest.raises(RecoveryRequired):
            t.team.advance({"job_id": "a" * 32, "phase": "implement"})
        assert t.team.executors["engineer"].adapter.calls == 0
        assert len(t.team._requests()) == 1
    finally:
        t.close()


def test_raw_tool_output_quota_becomes_unknown_not_success(tmp_path):
    class Overflow(MockAgentAdapter):
        def run(self, request, emit, cancellation=None):
            emit({"kind": "message", "data": {"text": "a" * 9000}})

    t = training(tmp_path, Overflow)
    try:
        job = run_phase(t, t.team.prepare(preparation()))
        assert job["phases"][0]["status"] == "unknown"
        assert not job["phases"][0]["outputs"]
    finally:
        t.close()


def test_expired_team_permission_never_calls_adapter(tmp_path, monkeypatch):
    t = training(tmp_path)
    try:
        phase = t.team.prepare(preparation())["phases"][0]
        monkeypatch.setattr(
            "ii_pro_core.approvals.utc_now", lambda: "2999-01-01T00:00:00+00:00"
        )
        with pytest.raises(Conflict):
            t.team.decide(decision(phase))
        assert t.team.executors["architect"].adapter.calls == 0
    finally:
        t.close()


def test_provider_rpc_timeout_is_not_retried():
    class TimeoutWire(WireFixture):
        def receive(self, timeout):
            raise TimeoutError("fixture")

    wire = TimeoutWire([])
    with pytest.raises(TransportLost):
        AppServerClient(wire).recover("saved")
    assert len(wire.sent) == 1


def test_thread_identity_change_is_rejected():
    wire = WireFixture([{"id": 1, "result": {"thread": {"id": "different"}}}])
    with pytest.raises(TransportLost):
        AppServerClient(wire).recover("saved")


def test_process_close_never_claims_stop_when_process_still_runs():
    import subprocess

    class Process:
        def wait(self, timeout):
            raise subprocess.TimeoutExpired("fixture", timeout)

    transport = StdioTransport(io.BytesIO(), io.BytesIO())
    transport.process = Process()
    assert transport.close() is False


def test_unknown_team_blocks_prepared_school_run_too(tmp_path):
    from test_school_v03_ui import (
        preparation as lesson_preparation,
        decision as lesson_decision,
    )

    t = training(tmp_path, lambda lab: MockAgentAdapter(lab, fault="disconnect"))
    try:
        lesson = t.prepare(lesson_preparation())
        run_phase(t, t.team.prepare(preparation()))
        with pytest.raises(RecoveryRequired):
            t.decide(lesson_decision(lesson))
        assert t.school.adapter.mock.calls == 0
    finally:
        t.close()


def test_reference_identity_is_checked_without_importing_workspace(
    tmp_path, monkeypatch
):
    from ii_pro_team.workspace import Laboratory

    lab = Laboratory(tmp_path / "lab")
    monkeypatch.setattr(
        "ii_pro_team.workspace.AFTER", "def slug_words(text):\n    return text\n"
    )
    with pytest.raises(Conflict, match="trusted reference"):
        lab.implement("a" * 32)


def test_invalid_large_envelope_allocates_no_workspace_or_task(tmp_path):
    t = training(tmp_path)
    try:
        with pytest.raises(ValidationError):
            t.team.prepare(preparation(text="字" * 1800))
        assert t.team._requests() == []
        assert list(t.team.laboratory.root.iterdir()) == []
    finally:
        t.close()
