import copy
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ii_pro_core.errors import ValidationError
from ii_pro_school import ModelProfile, OllamaStudentAdapter
from ii_pro_school.adapters import LoopbackTransport, StudentFailure


def profile(**kwargs):
    return ModelProfile.build("qwen7b-fast:latest", "a" * 64, **kwargs)


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.responses = {
            "version": {"version": "test-server"},
            "tags": {"models": [{"name": "qwen7b-fast:latest", "digest": "a" * 64}]},
            "show": {"template": "template", "system": "system", "parameters": "num_gpu 24", "details": {"parameter_size": "7.6B"}},
            "chat": {"model": "qwen7b-fast:latest", "message": {"content": "  original\r\ntext\n"},
                     "done": True, "done_reason": "stop", "eval_count": 12, "total_duration": 1000000},
        }

    def request(self, route, payload=None, **kwargs):
        self.calls.append((route, copy.deepcopy(payload), kwargs))
        value = self.responses[route]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


def test_metadata_probe_never_generates_or_downloads_and_response_is_exact():
    transport = FakeTransport()
    adapter = OllamaStudentAdapter(profile(), transport=transport)
    snapshot = adapter.probe()
    assert [c[0] for c in transport.calls] == ["version", "tags", "show"]
    assert snapshot["digest"] == "a" * 64 and snapshot["saved_parameters"] == "num_gpu 24"
    reply = adapter.respond("One lesson only")
    assert reply.text == "  original\r\ntext\n" and reply.complete
    assert reply.receipt["statistics"]["eval_count"] == 12
    payload = transport.calls[-1][1]
    assert payload["options"] == profile().options
    assert payload["stream"] is False and payload["keep_alive"] == "30s"
    assert "tools" not in payload and adapter.supports_cancellation is False
    assert reply.receipt["elapsed_seconds"] >= 0


@pytest.mark.parametrize("mutation,code", [
    (lambda t: t.responses.__setitem__("version", StudentFailure("unavailable")), "unavailable"),
    (lambda t: t.responses.__setitem__("tags", {"models": []}), "model_missing"),
    (lambda t: t.responses["tags"]["models"][0].__setitem__("digest", "b" * 64), "model_drift"),
    (lambda t: t.responses.__setitem__("tags", {"models": {}}), "invalid_metadata"),
])
def test_probe_fails_closed(mutation, code):
    transport = FakeTransport()
    mutation(transport)
    with pytest.raises(StudentFailure) as exc:
        OllamaStudentAdapter(profile(), transport=transport).probe()
    assert exc.value.code == code
    assert not any(c[0] == "chat" for c in transport.calls)


@pytest.mark.parametrize("mutation,code", [
    (lambda d: d.__setitem__("model", "other"), "invalid_response"),
    (lambda d: d.__setitem__("message", {}), "invalid_response"),
    (lambda d: d.__setitem__("eval_count", True), "invalid_statistics"),
    (lambda d: d.__setitem__("total_duration", -1), "invalid_statistics"),
])
def test_bad_response_protocol_never_becomes_confirmed(mutation, code):
    transport = FakeTransport()
    mutation(transport.responses["chat"])
    with pytest.raises(StudentFailure) as exc:
        OllamaStudentAdapter(profile(), transport=transport).respond("lesson")
    assert exc.value.code == code and len(transport.calls) == 1


@pytest.mark.parametrize("mutation", [lambda d: d.__setitem__("done", False),
                                     lambda d: d.__setitem__("done_reason", "length"),
                                     lambda d: d["message"].__setitem__("content", ""),
                                     lambda d: d["message"].__setitem__("tool_calls", [{"function": "never_execute"}])])
def test_incomplete_empty_or_tool_response_is_retained_as_unconfirmed(mutation):
    transport = FakeTransport()
    mutation(transport.responses["chat"])
    reply = OllamaStudentAdapter(profile(), transport=transport).respond("lesson")
    assert reply.complete is False and len(transport.calls) == 1


@pytest.mark.parametrize("endpoint", ["http://localhost:11434", "http://10.0.0.1:11434", "https://127.0.0.1:11434",
                                      "http://127.0.0.1:11434/path", "http://user:pass@127.0.0.1:11434",
                                      "http://127.0.0.1:11434?escape=1"])
def test_endpoint_is_explicit_loopback_only(endpoint):
    with pytest.raises(ValidationError):
        profile(endpoint=endpoint)


@pytest.mark.parametrize("key,value", [("num_predict", 513), ("num_ctx", 32768), ("num_thread", 8),
                                       ("seed", True), ("temperature", float("nan")), ("extra", 1)])
def test_generation_budgets_are_validated_and_immutable(key, value):
    opts = profile().options
    opts[key] = value
    with pytest.raises(ValidationError):
        profile(options=opts)


def test_options_are_copied_and_profile_hash_binds_generation():
    opts = profile().options
    model = profile(options=opts)
    opts["seed"] = 100
    assert model.options["seed"] == 42
    assert model.id != profile(options=opts).id


@contextmanager
def server(reply):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            if self.headers.get("Content-Length"):
                self.rfile.read(int(self.headers["Content-Length"]))
            reply(self)
        do_POST = do_GET
        def log_message(self, *_):
            pass
    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    instance.daemon_threads = True
    thread = threading.Thread(target=lambda: instance.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    try:
        yield LoopbackTransport(f"http://127.0.0.1:{instance.server_port}"), calls
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=2)


def test_real_http_transport_ignores_proxy_environment(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    def reply(h):
        h.send_response(200)
        h.end_headers()
        h.wfile.write(b'{"version":"local"}')
    with server(reply) as (transport, calls):
        assert transport.request("version") == {"version": "local"}
        assert calls == ["/api/version"]


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"unfinished":', b'[]', b'\xff'])
def test_real_http_malformed_json_is_rejected(raw):
    def reply(h):
        h.send_response(200)
        h.end_headers()
        h.wfile.write(raw)
    with server(reply) as (transport, calls):
        with pytest.raises(StudentFailure) as exc:
            transport.request("chat", {"prompt": "fixture"})
        assert exc.value.code == "invalid_json" and len(calls) == 1


@pytest.mark.parametrize("status", [302, 404, 500])
def test_http_errors_and_redirects_are_not_retried_or_followed(status):
    def reply(h):
        h.send_response(status)
        h.send_header("Location", "http://127.0.0.1:1/forbidden")
        h.end_headers()
    with server(reply) as (transport, calls):
        with pytest.raises(StudentFailure) as exc:
            transport.request("chat", {})
        assert exc.value.code == f"http_{status}" and len(calls) == 1


@pytest.mark.parametrize("length_header", [False, True])
def test_oversized_http_response_is_bounded(length_header):
    def reply(h):
        h.send_response(200)
        if length_header:
            h.send_header("Content-Length", "1000000")
        h.end_headers()
        try:
            h.wfile.write(b"x" * 70000)
        except OSError:
            pass
    with server(reply) as (transport, _):
        with pytest.raises(StudentFailure) as exc:
            transport.request("chat", {})
        assert exc.value.code == "response_too_large"


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_total_deadline_handles_slow_headers_or_dripping_body(phase):
    def reply(h):
        try:
            if phase == "headers":
                time.sleep(.2)
                h.send_response(200)
                h.end_headers()
                return
            h.send_response(200)
            h.end_headers()
            for _ in range(20):
                h.wfile.write(b" ")
                h.wfile.flush()
                time.sleep(.02)
        except OSError:
            pass
    with server(reply) as (transport, calls):
        started = time.monotonic()
        with pytest.raises(StudentFailure) as exc:
            transport.request("chat", {}, timeout=.06)
        assert exc.value.code == "timeout" and time.monotonic() - started < .8
        assert len(calls) == 1


def test_transport_has_no_arbitrary_routes():
    transport = LoopbackTransport("http://127.0.0.1:11434")
    with pytest.raises(ValidationError):
        transport.request("pull", {"model": "never-download"})
