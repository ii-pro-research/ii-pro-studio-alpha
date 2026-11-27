"""Real Core/School persistence with mock execution, no Ollama or containers."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
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
from ii_pro_school.adapters import StudentFailure
from ii_pro_ui.server import LocalServer
from ii_pro_ui.server import Handler
from ii_pro_ui.training import Training, MockSchoolStudent, MODEL_ID


def preparation(key="a", lesson="bug-positive-sum"):
    return {"request_id": key * 32, "lesson_id": lesson, "model_id": MODEL_ID}


def decision(attempt, allow=True):
    return {
        "run_id": attempt["run_id"],
        "approval_id": attempt["approval"]["id"],
        "spec_digest": attempt["approval"]["spec_digest"],
        "allow": allow,
    }


def wait(training):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with training.lock:
            if not training.threads:
                return training.state()["attempts"][0]
        time.sleep(0.005)
    pytest.fail("Mock execution did not finish within five seconds")


@pytest.fixture
def training(tmp_path):
    t = Training(tmp_path / "training", "session-a")
    yield t
    t.close()


def test_catalog_is_practice_only_and_nothing_is_created_by_read(training):
    state = training.state()
    assert len(state["lessons"]) == 3 and not training.root.exists()
    assert all(x["kind"] == "practice" for x in state["lessons"])
    assert not state["real_inference_enabled"] and not state["code_execution_enabled"]
    assert state["attempts"] == []


def test_prepare_requires_exact_approval_then_preserves_raw_response(training):
    a = training.prepare(preparation())
    school = training.school
    assert a["status"] == "created" and school.adapter.mock.calls == 0
    with pytest.raises(ApprovalRequired):
        school.execute(a["run_id"])
    training.decide(decision(a))
    result = wait(training)
    assert result["status"] == "succeeded" and school.adapter.mock.calls == 1
    assert (
        hashlib.sha256(result["response"].encode()).hexdigest()
        == result["response_sha256"]
    )
    assert result["assessment"]["method"] == "ast_only_v1"
    assert result["assessment"]["functional_status"] == "not_executed"
    assert result["assessment"]["score_available"] == 45
    assert result["backend"] == "MockExecutor" and not result["real_inference"]
    assert len(result["artifacts"]) == 2
    assert school.functional is None and set(school.core.runs.executors) == {
        "school_student"
    }


def test_parallel_prepare_and_clicks_dispatch_only_once(training):
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: training.prepare(preparation()), range(16)))
    assert len({a["run_id"] for a in results}) == 1
    a = results[0]
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda _: training.decide(decision(a)), range(16)))
    wait(training)
    assert training.school.adapter.mock.calls == 1
    assert len(training.state()["attempts"]) == 1
    assert len(training.school.store.all("SELECT * FROM tasks")) == 1


def test_changed_lesson_cannot_reuse_idempotency_key(training):
    training.prepare(preparation())
    with pytest.raises(Conflict):
        training.prepare(preparation(lesson="write-clamp"))
    assert len(training.state()["attempts"]) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"model_id": "qwen7b-fast:latest"},
        {"lesson_id": "exam-even-sum"},
        {"request_id": "bad"},
        {"lesson_id": ["bug-positive-sum"]},
        {"command": "arbitrary"},
        {"endpoint": "http://external"},
    ],
)
def test_prepare_rejects_unregistered_fields_and_real_inference(training, change):
    with pytest.raises(ValidationError):
        training.prepare({**preparation(), **change})
    assert training.school is None


@pytest.mark.parametrize("field", ["run_id", "approval_id", "spec_digest"])
def test_confirmation_identity_mismatch_never_dispatches(training, field):
    a = training.prepare(preparation())
    bad = decision(a)
    bad[field] = "0" * len(bad[field])
    from ii_pro_core.errors import CoreError

    with pytest.raises(CoreError):
        training.decide(bad)
    assert training.school.adapter.mock.calls == 0


def test_reject_and_explicit_fresh_request(training):
    a = training.prepare(preparation())
    assert training.decide(decision(a, False))["approval"]["status"] == "rejected"
    with pytest.raises(Conflict):
        training.decide(decision(a))
    new = training.prepare(preparation())
    assert new["run_id"] == a["run_id"] and new["approval"]["id"] != a["approval"]["id"]
    training.decide(decision(new))
    assert wait(training)["status"] == "succeeded"


def test_restart_preserves_response_history_and_never_reexecutes(tmp_path):
    path = tmp_path / "training"
    t = Training(path, "first")
    a = t.prepare(preparation())
    t.decide(decision(a))
    result = wait(t)
    t.close()
    t = Training(path, "second")
    try:
        assert t.state()["attempts"][0]["response_sha256"] == result["response_sha256"]
        assert t.prepare(preparation())["run_id"] == a["run_id"]
        t.decide(decision(a))
        assert t.school.adapter.mock.calls == 0
    finally:
        t.close()


def test_prior_browser_session_approval_cannot_be_used_after_restart(tmp_path):
    path = tmp_path / "training"
    t = Training(path, "first")
    a = t.prepare(preparation())
    t.close()
    t = Training(path, "second")
    try:
        with pytest.raises(Conflict):
            t.decide(decision(a))
        new = t.prepare(preparation())
        assert new["approval"]["id"] != a["approval"]["id"]
        training_state = t.state()
        assert len(training_state["attempts"]) == 1
        t.decide(decision(new))
        assert wait(t)["status"] == "succeeded"
    finally:
        t.close()


def test_offline_preflight_is_failed_not_started_and_not_retried(training, monkeypatch):
    a = training.prepare(preparation())

    def offline():
        raise StudentFailure("ollama_unavailable")

    monkeypatch.setattr(training.school.adapter, "probe", offline)
    training.decide(decision(a))
    result = wait(training)
    assert result["status"] == "failed" and result["effect_status"] == "not_started"
    assert training.prepare(preparation())["run_id"] == a["run_id"]
    training.decide(decision(a))
    assert training.school.adapter.mock.calls == 0
    assert any(
        e.kind == "school.adapter_diagnostic" for e in training.school.core.events()
    )


class AfterCaptureError(MockSchoolStudent):
    revision = "ui-test-post-capture-error"

    def approval_configuration(self):
        return super().approval_configuration()

    def execute(self, request):
        super().execute(request)
        raise RuntimeError("after effect")


def test_post_response_error_is_unknown_and_blocks_new_sessions(tmp_path):
    t = Training(tmp_path / "training", "first", adapter_factory=AfterCaptureError)
    a = t.prepare(preparation())
    t.decide(decision(a))
    result = wait(t)
    assert result["status"] == "unknown" and result["response"]
    assert result["assessment"] is None
    with pytest.raises(RecoveryRequired):
        t.decide(decision(a))
    with pytest.raises(RecoveryRequired):
        t.prepare(preparation("b"))
    t.close()
    t = Training(tmp_path / "training", "second", adapter_factory=AfterCaptureError)
    try:
        assert t.state()["attempts"][0]["status"] == "unknown"
        assert t.school.adapter.mock.calls == 0
        with pytest.raises(RecoveryRequired):
            t.prepare(preparation("c"))
    finally:
        t.close()


def test_unknown_also_blocks_previously_prepared_other_lesson(tmp_path):
    t = Training(tmp_path / "training", "first", adapter_factory=AfterCaptureError)
    try:
        first = t.prepare(preparation())
        second = t.prepare(preparation("b", "write-clamp"))
        t.decide(decision(first))
        wait(t)
        with pytest.raises(RecoveryRequired):
            t.decide(decision(second))
        assert t.school.adapter.mock.calls == 1
        assert t._attempt(second["run_id"])["status"] == "created"
    finally:
        t.close()


def test_interrupted_durable_run_recovers_without_dispatch(tmp_path):
    t = Training(tmp_path / "training", "first")
    a = t.prepare(preparation())
    # Persisted crash fixture; no actual external process is started or killed.
    with t.school.store.transaction() as conn:
        conn.execute(
            "UPDATE runs SET status='running',effect_status='unknown',execution_stopped=0 WHERE id=?",
            (a["run_id"],),
        )
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (a["task_id"],))
    t.close()
    t = Training(tmp_path / "training", "second")
    try:
        assert t.state()["attempts"][0]["status"] == "unknown"
        assert t.school.adapter.mock.calls == 0
    finally:
        t.close()


class BlockingStudent(MockSchoolStudent):
    revision = "ui-test-coordination"

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def approval_configuration(self):
        return super().approval_configuration()

    def execute(self, request):
        self.entered.set()
        assert self.release.wait(5)
        return super().execute(request)


def test_progress_is_core_state_and_close_retains_active_ownership(tmp_path):
    t = Training(tmp_path / "training", "session", adapter_factory=BlockingStudent)
    a = t.prepare(preparation())
    adapter = t.school.adapter
    t.decide(decision(a))
    assert adapter.entered.wait(2)
    closer = threading.Thread(target=t.close)
    try:
        assert t.state()["attempts"][0]["status"] == "running"
        closer.start()
        # Closing waits for the real active handle; another Core cannot own it.
        from ii_pro_core.errors import CoreBusy
        from ii_pro_core import Core

        with pytest.raises(CoreBusy):
            Core(t.root)
        assert closer.is_alive()
    finally:
        adapter.release.set()
        closer.join(5)
    assert not closer.is_alive() and t.school.core.state == "closed"


def test_revoked_and_expired_approval_do_not_execute(training):
    a = training.prepare(preparation())
    training.school.core.approvals.revoke(a["approval"]["id"])
    with pytest.raises(Conflict):
        training.decide(decision(a))
    new = training.prepare(preparation())
    # Wall clock is controlled rather than mutating immutable approval rows.
    from unittest.mock import patch

    with patch(
        "ii_pro_core.approvals.utc_now", return_value="2999-01-01T00:00:00+00:00"
    ):
        with pytest.raises(Conflict):
            training.decide(decision(new))
    assert training.school.adapter.mock.calls == 0


def test_session_quota_rejects_new_keys_but_allows_observation(training):
    a = training.prepare(preparation())
    # Existing School API sets up the bounded quota fixture, no executions.
    for i in range(99):
        training.school.create_session("ui-mock", session_id=f"{i:032x}")
    with pytest.raises(Conflict):
        training.prepare(preparation("b"))
    assert training.prepare(preparation())["run_id"] == a["run_id"]


@pytest.fixture
def api(tmp_path):
    class Observer:
        def state(self):
            return {"read_only": True}

    server = LocalServer(0, Observer(), token="capability")
    server.enable_training(tmp_path / "training")
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    yield server
    server.shutdown()
    thread.join()
    server.server_close()


def request(api, path, body=None, *, headers=None, raw=None):
    h = {
        "Authorization": "Bearer capability",
        "Origin": api.origin,
        "Sec-Fetch-Site": "same-origin",
        "X-CSRF-Token": api.csrf,
        "Content-Type": "application/json",
    }
    if headers:
        for k, v in headers.items():
            if v is None:
                h.pop(k, None)
            else:
                h[k] = v
    conn = http.client.HTTPConnection("127.0.0.1", api.server_port, timeout=5)
    conn.request(
        "GET" if body is None and raw is None else "POST",
        path,
        body=raw if raw is not None else json.dumps(body) if body is not None else None,
        headers=h,
    )
    response = conn.getresponse()
    data = json.loads(response.read())
    code = response.status
    conn.close()
    return code, data


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": None},
        {"Authorization": "Bearer wrong"},
        {"Origin": None},
        {"Origin": "null"},
        {"Origin": "https://evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": None},
        {"X-CSRF-Token": None},
        {"X-CSRF-Token": "wrong"},
        {"Host": "evil.example"},
    ],
)
def test_writes_need_session_origin_and_csrf(api, headers):
    assert request(api, "/api/school/prepare", preparation(), headers=headers)[0] == 403
    assert api.training.school is None


@pytest.mark.parametrize(
    "raw",
    [b"[]", b'{"request_id":1,"request_id":2}', b'{"x":NaN}', b"{bad", b"x" * 4097],
)
def test_malformed_write_cannot_prepare(api, raw):
    assert request(api, "/api/school/prepare", raw=raw)[0] == 400
    assert api.training.school is None


def test_http_full_mock_flow_and_get_read_only(api):
    code, state = request(api, "/api/school/training")
    assert code == 200 and state["attempts"] == []
    assert request(api, "/api/session")[1]["csrf"] == api.csrf
    code, a = request(api, "/api/school/prepare", preparation())
    assert code == 200 and a["approval"]["status"] == "pending"
    assert request(api, "/api/school/decision", decision(a))[0] == 200
    result = wait(api.training)
    files = [
        p
        for p in api.training.root.rglob("*")
        if p.is_file() and p.name != "owner.lock"
    ]
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    for _ in range(3):
        assert (
            request(api, "/api/school/training")[1]["attempts"][0]["response"]
            == result["response"]
        )
    assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    assert request(api, "/api/school/execute-code", {})[0] == 405
    assert request(api, "/api/school/enable-real", {})[0] == 405


@pytest.mark.parametrize(
    "error",
    [ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError],
)
def test_abandoned_browser_poll_is_not_replied_to_twice(error):
    handler = object.__new__(Handler)
    calls = []

    def disconnected(*args):
        calls.append(args)
        raise error()

    handler.send_response = disconnected
    handler.reply(200, {"status": "unchanged"})
    assert handler.close_connection and len(calls) == 1
