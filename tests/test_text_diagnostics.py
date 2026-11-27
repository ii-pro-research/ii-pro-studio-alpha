import json
import time
from http.client import RemoteDisconnected

import pytest

from ii_pro_team import text
from ii_pro_team.text_diagnostics import TransportDiagnostic
from ii_pro_ui.training import Training
from test_gpt_text import grant, prepare
from test_gpt_team import decision, wait


def frame(packet):
    return b"data: " + json.dumps(packet, ensure_ascii=False).encode() + b"\r\n\r\n"


def successful(text_value="План"):
    return [
        {"type": "response.created", "response": {"id": "resp_diagnostic"}},
        {"type": "response.output_text.delta", "delta": text_value},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_diagnostic",
                "model": text.MODEL,
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": text_value}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            },
        },
    ]


class Response:
    def __init__(
        self,
        chunks,
        *,
        status=200,
        content_type="text/event-stream; charset=utf-8",
        request_id="req_mock_123",
    ):
        self.chunks = list(chunks)
        self.status, self.content_type, self.request_id = (
            status,
            content_type,
            request_id,
        )
        self.fp = None

    def read1(self, size):
        if not self.chunks:
            return b""
        value = self.chunks.pop(0)
        if isinstance(value, Exception):
            raise value
        if len(value) > size:
            self.chunks.insert(0, value[size:])
            return value[:size]
        return value

    def read(self, size):
        return self.read1(size)

    def getheader(self, name, default=""):
        return {"content-type": self.content_type, "x-request-id": self.request_id}.get(
            name.lower(), default
        )


def wire(monkeypatch, response, *, failure=None):
    calls, connections = [], []

    class Connection:
        sock = None

        def __init__(self, host, timeout):
            assert host == "api.openai.com"
            self.closed = False
            connections.append(self)

        def connect(self):
            if failure == "connect":
                raise ConnectionRefusedError("Bearer DO_NOT_LOG_CONNECTION_SECRET")

        def request(self, method, path, body, headers):
            calls.append((method, path, json.loads(body)))
            if failure == "send":
                raise BrokenPipeError("DO_NOT_LOG_PARTIAL_SEND_SECRET")

        def getresponse(self):
            if failure == "headers":
                raise RemoteDisconnected("DO_NOT_LOG_HEADER_SECRET")
            return response

        def close(self):
            self.closed = True
            if failure == "close":
                raise OSError("DO_NOT_LOG_CLOSE_SECRET")

    monkeypatch.setattr(text, "HTTPSConnection", Connection)
    return calls, connections


def execute(tmp_path, monkeypatch, response, *, failure=None):
    calls, connections = wire(monkeypatch, response, failure=failure)
    studio = Training(
        tmp_path / "state",
        "mock-transport-session",
        include_ollama=True,
        include_team=True,
        text_adapter_factory=lambda: text.SiwcTextAdapter(grant()),
    )
    studio._open()
    try:
        phase = prepare(studio)["phases"][0]
        studio.team.text_executor.authorized_run = phase["run_id"]
        studio.team.decide(decision(phase))
        phase = wait(studio)["phases"][0]
        facts = [
            e["agent_event"]["data"]
            for e in phase["events"]
            if e["agent_event"]["data"].get("type") == "transport.diagnostic"
        ]
        assert len(facts) == 1
        assert len(calls) <= 1 and all(c.closed for c in connections)
        return phase, facts[0], calls
    finally:
        studio.close()


def test_success_and_utf8_frames_split_across_network_blocks(tmp_path, monkeypatch):
    raw = b"\xef\xbb\xbf: heartbeat\r\n\r\n" + b"".join(frame(p) for p in successful())
    phase, facts, calls = execute(
        tmp_path, monkeypatch, Response([raw[i : i + 1] for i in range(len(raw))])
    )
    assert phase["status"] == "succeeded" and len(calls) == 1
    assert (
        facts["outcome"] == "completed" and facts["termination_reason"] == "completed"
    )
    assert facts["received_bytes"] == len(raw) and facts["sse_events"] == 3
    assert facts["response_created_received"] and facts["response_completed_received"]
    assert facts["last_event_type"] == "response.completed"
    assert facts["request_id"] == "req_mock_123"
    assert facts["http_status"] == 200 and facts["content_type"] == "text/event-stream"
    assert (
        phase["outputs"]["result.json"]["transport_diagnostic"]["outcome"]
        == "completed"
    )


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_http_rejection_retains_facts_without_provider_message(
    tmp_path, monkeypatch, status
):
    code = (
        "subscription_sharing_usage_limit_exceeded"
        if status == 429
        else "invalid_request_error"
    )
    raw = json.dumps(
        {"error": {"code": code, "message": "Bearer DO_NOT_LOG_PROVIDER_SECRET"}}
    ).encode()
    phase, facts, calls = execute(
        tmp_path,
        monkeypatch,
        Response([raw], status=status, content_type="application/json"),
    )
    assert phase["status"] == "unknown" and len(calls) == 1
    assert facts["http_status"] == status and facts["api_error_code"] == code
    assert facts["outcome"] == "confirmed_http_rejection"
    assert (
        facts["stage"] == "http_rejection"
        and facts["termination_reason"] == "http_rejected"
    )
    assert facts["received_bytes"] == len(raw) and facts["sse_events"] == 0
    assert (
        not facts["response_created_received"]
        and not facts["response_completed_received"]
    )
    assert "DO_NOT_LOG_PROVIDER_SECRET" not in json.dumps(phase)


@pytest.mark.parametrize(
    "failure,primary,outcome",
    [
        ("connect", "ConnectionRefusedError", "before_send"),
        ("send", "BrokenPipeError", "uncertain_after_send"),
        ("headers", "RemoteDisconnected", "uncertain_after_send"),
    ],
)
def test_primary_exception_stage_and_send_boundary_survive(
    tmp_path, monkeypatch, failure, primary, outcome
):
    phase, facts, calls = execute(tmp_path, monkeypatch, Response([]), failure=failure)
    assert phase["status"] == "unknown"  # Core remains conservative after execute.
    assert len(calls) == (0 if failure == "connect" else 1)
    assert facts["primary_exception_type"] == primary and facts["outcome"] == outcome
    assert (
        facts["termination_reason"]
        == {
            "connect": "connect_failed",
            "send": "send_failed",
            "headers": "headers_failed",
        }[failure]
    )
    assert "DO_NOT_LOG" not in json.dumps(phase)


@pytest.mark.parametrize(
    "case,reason,primary",
    [
        ("content_type", "unexpected_content_type", "TextUncertain"),
        ("disconnect", "stream_read_failed", "ConnectionResetError"),
        ("json", "sse_json_invalid", "JSONDecodeError"),
        ("unknown_type", "unknown_event", "TextUncertain"),
        ("no_completed", "missing_completed", "TextUncertain"),
        ("utf8", "utf8_invalid", "UnicodeDecodeError"),
        ("unterminated", "sse_unterminated", "TextUncertain"),
    ],
)
def test_sse_failure_reason_is_preserved_in_studio_dto(
    tmp_path, monkeypatch, case, reason, primary
):
    chunks = [
        frame(successful()[0]),
        frame({"type": "response.output_text.delta", "delta": "A" * 500}),
    ]
    content_type = "text/event-stream"
    if case == "content_type":
        content_type = "text/html"
    elif case == "disconnect":
        chunks.append(ConnectionResetError("Bearer DO_NOT_LOG_NETWORK_SECRET"))
    elif case == "json":
        chunks.append(b"data: {broken}\n\n")
    elif case == "unknown_type":
        chunks.append(frame({"type": "response.future_private_event"}))
    elif case == "utf8":
        chunks.append(b"data: \xff\n\n")
    elif case == "unterminated":
        chunks.append(b'data: {"type":"error"}')
    phase, facts, calls = execute(
        tmp_path, monkeypatch, Response(chunks, content_type=content_type)
    )
    assert phase["status"] == "unknown" and len(calls) == 1
    assert (
        facts["termination_reason"] == reason
        and facts["primary_exception_type"] == primary
    )
    assert facts["outcome"] == "uncertain_after_send" and facts["http_status"] == 200
    assert not facts["response_completed_received"]
    assert not phase["outputs"] and "DO_NOT_LOG_NETWORK_SECRET" not in json.dumps(phase)
    if case != "content_type":
        assert facts["response_created_received"]
        assert any(e["agent_event"]["data"].get("provisional") for e in phase["events"])


def test_secrets_in_headers_error_fields_and_unknown_event_are_not_retained(
    tmp_path, monkeypatch
):
    secret = grant().access_token
    raw = json.dumps(
        {
            "detail": "Bearer " + secret,
            "error": {"code": grant().refresh_token, "message": grant().id_token},
        }
    ).encode()
    phase, facts, _ = execute(
        tmp_path,
        monkeypatch,
        Response(
            [raw],
            status=403,
            content_type="application/json; Authorization=Bearer " + secret,
            request_id="req_" + secret,
        ),
    )
    dumped = json.dumps(phase)
    assert all(
        s not in dumped
        for s in (secret, grant().refresh_token, grant().id_token, "Authorization")
    )
    assert (
        facts["request_id"] == "[скрыто]" and facts["api_error_code"] == "unrecognized"
    )


def test_serialize_failure_is_before_any_connection(monkeypatch):
    calls, connections = wire(monkeypatch, Response([]))
    with pytest.raises(text.TextUncertain) as caught:
        list(
            text.ResponsesHTTPS().stream(
                {"bad": object()}, "synthetic", time.monotonic() + 5
            )
        )
    facts = caught.value.diagnostic
    assert not calls and not connections
    assert facts["primary_exception_type"] == "ValidationError"
    assert (
        facts["termination_reason"] == "serialize_failed"
        and facts["outcome"] == "before_send"
    )


def test_diagnostic_instances_are_not_shared_between_attempts():
    first, second = TransportDiagnostic(), TransportDiagnostic()
    first.data["received_bytes"] = 10
    assert second.snapshot()["received_bytes"] == 0


@pytest.mark.parametrize(
    "cancelled,reason", [(False, "event_quota"), (True, "cancelled")]
)
def test_wait_termination_never_mislabels_event_quota_or_cancel_as_deadline(
    cancelled, reason
):
    from types import SimpleNamespace

    class Stream:
        def stream(self, *args):
            for _ in range(257):
                yield {"type": "response.in_progress"}

    adapter = text.SiwcTextAdapter(grant(), transport=Stream(), mock=True)
    request = {
        "version": 2,
        "job_id": "a" * 32,
        "session_id": "b" * 32,
        "role": "architect",
        "phase": "plan",
        "project_id": "gpt-text",
        "text": "plan",
        "inputs": {"model": text.MODEL, "effort": "low", "profile_digest": "c" * 64},
    }
    events = []
    with pytest.raises(text.TextUncertain):
        adapter.run(request, events.append, SimpleNamespace(requested=cancelled))
    assert events[-1]["data"]["termination_reason"] == reason


def test_os_error_number_survives_without_exception_message(tmp_path, monkeypatch):
    phase, facts, _ = execute(
        tmp_path,
        monkeypatch,
        Response([ConnectionResetError(10054, "Bearer PRIVATE_OS_MESSAGE")]),
    )
    assert phase["status"] == "unknown"
    assert facts["primary_exception_type"] == "ConnectionResetError"
    assert facts["os_error_number"] == 10054
    assert "PRIVATE_OS_MESSAGE" not in json.dumps(phase)


def test_close_error_never_masks_original_decode_failure(tmp_path, monkeypatch):
    phase, facts, _ = execute(
        tmp_path, monkeypatch, Response([b"data: \xff\n\n"]), failure="close"
    )
    assert phase["status"] == "unknown"
    assert facts["primary_exception_type"] == "UnicodeDecodeError"
    assert facts["close_exception_type"] == "OSError"
    assert facts["termination_reason"] == "utf8_invalid"
    assert "DO_NOT_LOG_CLOSE_SECRET" not in json.dumps(phase)


def test_restart_keeps_failure_diagnostic_and_never_dispatches_again(
    tmp_path, monkeypatch
):
    from ii_pro_core.errors import RecoveryRequired

    phase, facts, calls = execute(
        tmp_path, monkeypatch, Response([]), failure="headers"
    )
    studio = Training(
        tmp_path / "state",
        "new-session",
        include_ollama=True,
        include_team=True,
        text_adapter_factory=lambda: text.SiwcTextAdapter(grant()),
    )
    try:
        studio._open()
        restored = studio.team.job("a" * 32)["phases"][0]
        assert restored["status"] == "unknown"
        recorded = [
            e["agent_event"]["data"]
            for e in restored["events"]
            if e["agent_event"]["data"].get("type") == "transport.diagnostic"
        ]
        assert recorded == [facts]
        with pytest.raises(RecoveryRequired):
            studio.team.decide(decision(phase))
        assert len(calls) == 1
    finally:
        studio.close()


def test_completed_received_does_not_imply_validated_success(tmp_path, monkeypatch):
    packets = successful()
    packets[-1]["response"]["id"] = "wrong-response-identity"
    phase, facts, _ = execute(
        tmp_path, monkeypatch, Response([frame(p) for p in packets])
    )
    assert phase["status"] == "unknown" and not phase["outputs"]
    assert facts["response_completed_received"] is True
    assert facts["termination_reason"] == "completion_invalid"
    assert facts["outcome"] != "completed"


def test_unknown_event_name_containing_credentials_is_not_logged(tmp_path, monkeypatch):
    phase, facts, _ = execute(
        tmp_path, monkeypatch, Response([frame({"type": grant().id_token})])
    )
    assert phase["status"] == "unknown"
    assert facts["last_event_type"] == "unrecognized"
    assert facts["termination_reason"] == "unknown_event"
    assert grant().id_token not in json.dumps(phase)


@pytest.mark.parametrize("code", [None, "server_error", {"secret": "private-fixture"}])
def test_top_level_error_event_preserves_safe_api_code_without_secondary_crash(
    tmp_path, monkeypatch, code
):
    phase, facts, _ = execute(
        tmp_path,
        monkeypatch,
        Response(
            [frame({"type": "error", "code": code, "message": grant().access_token})]
        ),
    )
    assert phase["status"] == "unknown"
    assert facts["termination_reason"] == "provider_failed"
    assert facts["primary_exception_type"] == "TextUncertain"
    assert facts["api_error_code"] == (
        "unrecognized" if isinstance(code, dict) else code
    )
    assert grant().access_token not in json.dumps(phase)
