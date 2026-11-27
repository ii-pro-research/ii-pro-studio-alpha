"""Full text-only School route against a finite local fake Ollama HTTP server."""

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import threading
import time

import pytest

from ii_pro_core.errors import Conflict, RecoveryRequired, ValidationError
from ii_pro_school.adapters import ModelProfile
from ii_pro_ui.training import Training, UIQwenStudent, REAL_PROFILE, REAL_ID, MODEL_ID
from test_school_v03_ui import decision, preparation, wait


RAW = "```python\r\ndef sum_positive(values):\r\n    return sum(x for x in values if x > 0)\r\n```\r\nОригинал.\n"


class OllamaFixture(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.calls = []
        self.chat_error = None
        self.delay = 0
        self.missing = False
        self.drift = False
        self.complete = True
        self.raw = RAW
        super().__init__(("127.0.0.1", 0), FakeHandler)


class FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        payload = json.loads(body) if body else None
        self.server.calls.append((self.path, payload))
        code = 200
        if self.path == "/api/version":
            result = {"version": "TEST-CLIENT"}
        elif self.path == "/api/tags":
            result = {
                "models": []
                if self.server.missing
                else [
                    {
                        "name": REAL_PROFILE.model,
                        "digest": "0" * 64
                        if self.server.drift
                        else REAL_PROFILE.expected_digest,
                    }
                ]
            }
        elif self.path == "/api/show":
            result = {
                "template": "fixture",
                "system": "fixture",
                "parameters": "fixture",
            }
        elif self.path == "/api/chat":
            time.sleep(self.server.delay)
            if self.server.chat_error:
                code = 503
                result = {"error": "synthetic Ollama failure"}
            else:
                result = {
                    "model": REAL_PROFILE.model,
                    "message": {"content": self.server.raw},
                    "done": True,
                    "done_reason": "stop" if self.server.complete else "length",
                    "total_duration": 200000000,
                    "prompt_eval_count": 120,
                    "eval_count": 48,
                    "eval_duration": 100000000,
                }
        else:
            raise AssertionError("Unexpected route")
        data = json.dumps(result, ensure_ascii=False).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


@pytest.fixture
def ollama():
    server = OllamaFixture()
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    yield server
    server.shutdown()
    thread.join()
    server.server_close()


def factory(ollama, *, timeout=1):
    profile = ModelProfile.build(
        REAL_PROFILE.model,
        REAL_PROFILE.expected_digest,
        endpoint=f"http://127.0.0.1:{ollama.server_port}",
        deadline_seconds=timeout,
    )
    return lambda: UIQwenStudent(profile)


def real_preparation(key="b"):
    return {**preparation(key), "model_id": REAL_ID}


def authorized(tmp_path, ollama, *, timeout=1):
    path = tmp_path / "training"
    build = factory(ollama, timeout=timeout)
    t = Training(path, "session-a", include_ollama=True, real_adapter_factory=build)
    a = t.prepare(real_preparation())
    t.close()
    t = Training(
        path,
        "session-b",
        include_ollama=True,
        real_adapter_factory=build,
        authorized_real_run=a["run_id"],
    )
    return t, t.prepare(real_preparation())


def test_two_modes_one_core_distinct_profiles_and_no_probe_on_read(tmp_path, ollama):
    with_training = Training(
        tmp_path / "training",
        "session",
        include_ollama=True,
        real_adapter_factory=factory(ollama),
    )
    t = with_training
    try:
        state = t.state()
        assert len(state["models"]) == 2 and state["mode"] == "mock_and_ollama"
        mock = t.prepare(preparation())
        real = t.prepare(real_preparation())
        assert mock["executor"] != real["executor"]
        assert mock["profile"] != real["profile"]
        assert not mock["real_inference"] and real["real_inference"]
        assert t.schools[MODEL_ID].core is t.schools[REAL_ID].core
        assert t.schools[REAL_ID].store is t.school.store
        assert not ollama.calls
        assert real["dispatch_allowed"] is False and real["response"] is None
    finally:
        t.close()


def test_real_decision_needs_separate_host_permission_not_only_browser_approval(
    tmp_path, ollama
):
    t = Training(
        tmp_path / "training",
        "session",
        include_ollama=True,
        real_adapter_factory=factory(ollama),
    )
    try:
        a = t.prepare(real_preparation())
        with pytest.raises(Conflict):
            t.decide(decision(a))
        assert not ollama.calls
        assert t._attempt(a["run_id"])["approval"]["status"] == "pending"
        assert t.decide(decision(a, False))["approval"]["status"] == "rejected"
    finally:
        t.close()


def test_one_approved_request_preserves_raw_statistics_and_sealed_artifacts(
    tmp_path, ollama
):
    t, a = authorized(tmp_path, ollama)
    try:
        assert a["dispatch_allowed"]
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda _: t.decide(decision(a)), range(16)))
        result = wait(t)
        assert result["status"] == "succeeded" and result["real_inference"]
        assert result["response"] == RAW
        assert result["response_sha256"] == hashlib.sha256(RAW.encode()).hexdigest()
        assert (
            result["statistics"]["eval_count"] == 48 and result["elapsed_seconds"] >= 0
        )
        assert result["assessment"]["functional_status"] == "not_executed"
        assert len([c for c in ollama.calls if c[0] == "/api/chat"]) == 1
        chat = next(c[1] for c in ollama.calls if c[0] == "/api/chat")
        assert (
            chat["options"] == REAL_PROFILE.options
            and chat["messages"][1]["content"] == a["prompt"]
        )
        assert "tools" not in chat and chat["stream"] is False
        assert t.school.store.one("SELECT COUNT(*) FROM artifact_blobs")[0] == 2
        assert t.school.store.one("SELECT COUNT(*) FROM artifact_seals")[0] == 1
        t.decide(decision(a))
        t.prepare(real_preparation())
        assert len([c for c in ollama.calls if c[0] == "/api/chat"]) == 1
    finally:
        t.close()


@pytest.mark.parametrize("kind", ["model", "options", "deadline"])
def test_changed_profile_after_approval_cannot_start(tmp_path, ollama, kind):
    t, a = authorized(tmp_path, ollama)
    try:
        adapter = t.schools[REAL_ID].adapter
        old = adapter.profile
        opts = old.options
        if kind == "options":
            opts["seed"] = 43
        adapter._profile = ModelProfile.build(
            "another:latest" if kind == "model" else old.model,
            old.expected_digest,
            options=opts,
            endpoint=old.endpoint,
            deadline_seconds=0.5 if kind == "deadline" else old.deadline_seconds,
        )
        with pytest.raises(Conflict):
            t.decide(decision(a))
        assert not ollama.calls
    finally:
        t.close()


@pytest.mark.parametrize(
    "change",
    [
        {"model": "arbitrary"},
        {"options": {"seed": 43}},
        {"command": "cmd"},
        {"endpoint": "http://other"},
    ],
)
def test_browser_cannot_supply_parameters_or_new_transport(tmp_path, ollama, change):
    t = Training(
        tmp_path / "training",
        "session",
        include_ollama=True,
        real_adapter_factory=factory(ollama),
    )
    try:
        with pytest.raises(ValidationError):
            t.prepare({**real_preparation(), **change})
        assert t.school is None and not ollama.calls
    finally:
        t.close()


@pytest.mark.parametrize("fault", ["missing", "drift"])
def test_unavailable_or_changed_installed_model_fails_before_chat(
    tmp_path, ollama, fault
):
    t, a = authorized(tmp_path, ollama)
    try:
        setattr(ollama, fault, True)
        t.decide(decision(a))
        result = wait(t)
        assert result["status"] == "failed" and result["effect_status"] == "not_started"
        assert not any(c[0] == "/api/chat" for c in ollama.calls)
        assert result["diagnostics"] == [
            "model_missing" if fault == "missing" else "model_drift"
        ]
    finally:
        t.close()


@pytest.mark.parametrize("fault", ["error", "timeout", "incomplete"])
def test_uncertain_reply_never_retries_and_blocks_both_modes(tmp_path, ollama, fault):
    t, a = authorized(tmp_path, ollama, timeout=0.05 if fault == "timeout" else 1)
    try:
        ollama.chat_error = fault == "error"
        ollama.delay = 0.2 if fault == "timeout" else 0
        ollama.complete = fault != "incomplete"
        t.decide(decision(a))
        result = wait(t)
        assert result["status"] == "unknown" and result["assessment"] is None
        assert result["diagnostics"] == [
            {
                "error": "http_503",
                "timeout": "timeout",
                "incomplete": "incomplete_response",
            }[fault]
        ]
        if fault == "incomplete":
            assert result["response"] == RAW
        with pytest.raises(RecoveryRequired):
            t.decide(decision(a))
        with pytest.raises(RecoveryRequired):
            t.prepare(preparation("c"))
        assert len([c for c in ollama.calls if c[0] == "/api/chat"]) == 1
    finally:
        t.close()


def test_real_history_restart_does_not_resend_and_unconfirmed_run_cannot_use_permit(
    tmp_path, ollama
):
    t, a = authorized(tmp_path, ollama)
    t.decide(decision(a))
    result = wait(t)
    second = t.prepare(real_preparation("c"))
    with pytest.raises(Conflict):
        t.decide(decision(second))
    t.close()
    t = Training(
        tmp_path / "training",
        "session-c",
        include_ollama=True,
        real_adapter_factory=factory(ollama),
    )
    try:
        history = t.state()["attempts"]
        assert (
            next(x for x in history if x["run_id"] == a["run_id"])["response_sha256"]
            == result["response_sha256"]
        )
        assert (
            next(x for x in history if x["run_id"] == second["run_id"])["status"]
            == "created"
        )
        assert len([c for c in ollama.calls if c[0] == "/api/chat"]) == 1
        assert t.school.adapter.mock.calls == 0
    finally:
        t.close()


def test_proposed_code_never_executes_on_host(tmp_path, ollama):
    marker = tmp_path / "forbidden-execution.txt"
    ollama.raw = f"```python\nopen({str(marker)!r}, 'w').write('executed')\n```"
    t, a = authorized(tmp_path, ollama)
    try:
        t.decide(decision(a))
        result = wait(t)
        assert result["status"] == "succeeded" and not marker.exists()
        assert result["assessment"]["score_awarded"] < 45
        assert t.school.functional is None and t.schools[REAL_ID].functional is None
        assert not any("worker" in x for x in t.school.core.runs.executors)
    finally:
        t.close()


def test_same_request_key_cannot_change_executor(tmp_path, ollama):
    t = Training(
        tmp_path / "training",
        "session",
        include_ollama=True,
        real_adapter_factory=factory(ollama),
    )
    try:
        a = t.prepare(preparation())
        with pytest.raises(Conflict):
            t.prepare(real_preparation("a"))
        assert len(t.state()["attempts"]) == 1 and not ollama.calls
        assert t._attempt(a["run_id"])["real_inference"] is False
    finally:
        t.close()


def test_service_unavailable_before_chat_is_ordinary_failure(
    tmp_path, ollama, monkeypatch
):
    from ii_pro_school.adapters import LoopbackTransport, StudentFailure

    t, a = authorized(tmp_path, ollama)

    def unavailable(*args, **kwargs):
        raise StudentFailure("unavailable")

    monkeypatch.setattr(LoopbackTransport, "request", unavailable)
    try:
        t.decide(decision(a))
        result = wait(t)
        assert result["status"] == "failed" and result["diagnostics"] == ["unavailable"]
        assert result["effect_status"] == "not_started" and not ollama.calls
    finally:
        t.close()


def test_unknown_survives_restart_without_metadata_or_chat(tmp_path, ollama):
    t, a = authorized(tmp_path, ollama)
    ollama.complete = False
    t.decide(decision(a))
    result = wait(t)
    assert result["status"] == "unknown"
    calls = len(ollama.calls)
    t.close()
    t = Training(
        tmp_path / "training",
        "session-c",
        include_ollama=True,
        real_adapter_factory=factory(ollama),
        authorized_real_run=a["run_id"],
    )
    try:
        saved = t.state()["attempts"][0]
        assert saved["status"] == "unknown" and saved["response"] == RAW
        with pytest.raises(RecoveryRequired):
            t.decide(decision(a))
        assert len(ollama.calls) == calls
    finally:
        t.close()


def test_reader_marks_mock_and_real_even_before_response(tmp_path, ollama):
    from ii_pro_ui.reader import Source, read_source

    t = Training(
        tmp_path / "training",
        "session",
        include_ollama=True,
        real_adapter_factory=factory(ollama),
    )
    try:
        mock = t.prepare(preparation())
        real = t.prepare(real_preparation())
        read = read_source(Source("ui", "UI", t.root), tmp_path / "cache")
        runs = {r["id"]: r for r in read["runs"]}
        assert runs[mock["run_id"]]["backend"] == "MockExecutor"
        assert runs[real["run_id"]]["backend"] == "Ollama Qwen7B"
        assert runs[real["run_id"]]["real_inference"]
        assert not any(a["raw_answer"] for a in read["attempts"])
        assert not ollama.calls
    finally:
        t.close()


def test_real_post_is_rejected_without_host_permit_despite_valid_session(tmp_path):
    import http.client
    from ii_pro_ui.server import LocalServer

    server = LocalServer(0, object(), token="test")
    server.enable_training(tmp_path / "state")
    thread = threading.Thread(target=server.serve_forever)
    thread.start()

    def post(path, body):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request(
            "POST",
            path,
            json.dumps(body),
            {
                "Authorization": "Bearer test",
                "Origin": server.origin,
                "Sec-Fetch-Site": "same-origin",
                "Content-Type": "application/json",
                "X-CSRF-Token": server.csrf,
            },
        )
        r = conn.getresponse()
        status = r.status
        data = json.loads(r.read())
        conn.close()
        return status, data

    try:
        status, a = post("/api/school/prepare", real_preparation())
        assert status == 200 and a["status"] == "created"
        assert post("/api/school/decision", decision(a))[0] == 409
        assert post("/api/school/enable-real", {"run_id": a["run_id"]})[0] == 405
        assert a["dispatch_allowed"] is False
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
