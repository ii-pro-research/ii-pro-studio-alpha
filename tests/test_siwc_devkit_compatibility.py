"""Official SIWC DevKit MIME tolerance, with strict completion validation."""

import io
import json
from http.client import HTTPResponse

import pytest

from ii_pro_team.text_diagnostics import TransportDiagnostic
from test_text_diagnostics import Response, execute, frame, successful


@pytest.mark.parametrize(
    "header,normalized,present",
    [
        ("text/event-stream", "text/event-stream", True),
        (None, None, False),
        ("", "", True),
        ("text/event-stream; charset=utf-8", "text/event-stream", True),
        ("  TeXt/EvEnT-StReAm ; charset=UTF-8  ", "text/event-stream", True),
    ],
)
def test_devkit_accepts_sse_with_optional_normalized_mime(
    tmp_path, monkeypatch, header, normalized, present
):
    packets = successful("Фиксированный Mock-ответ")
    phase, facts, calls = execute(
        tmp_path,
        monkeypatch,
        Response([frame(p) for p in packets], content_type=header),
    )
    assert phase["status"] == "succeeded" and len(calls) == 1
    assert facts["content_type"] == normalized
    assert facts["content_type_present"] is present
    assert facts["response_created_received"] and facts["response_completed_received"]
    assert facts["sse_events"] == 3 and facts["termination_reason"] == "completed"
    assert phase["outputs"]["result.json"]["text"] == "Фиксированный Mock-ответ"


@pytest.mark.parametrize(
    "header,normalized",
    [
        ("TEXT/HTML; charset=utf-8", "text/html"),
        (" application/json ", "application/json"),
        ("text/plain", "text/plain"),
        (
            "Application/X-SIWC-Diagnostic; private=must-not-log",
            "application/x-siwc-diagnostic",
        ),
        ("not a MIME private-message", "invalid"),
        ("text/event-stream, text/html", "invalid"),
    ],
)
def test_explicit_incompatible_mime_rejected_even_with_valid_sse(
    tmp_path, monkeypatch, header, normalized
):
    phase, facts, calls = execute(
        tmp_path,
        monkeypatch,
        Response([frame(p) for p in successful()], content_type=header),
    )
    assert phase["status"] == "unknown" and not phase["outputs"]
    assert len(calls) == 1 and facts["received_bytes"] == 0
    assert facts["content_type_present"] is True
    assert facts["content_type"] == normalized
    assert facts["termination_reason"] == "unexpected_content_type"
    assert "must-not-log" not in json.dumps(phase)
    assert "private-message" not in json.dumps(phase)


@pytest.mark.parametrize(
    "chunks,reason",
    [
        ([b"<html>not SSE</html>"], "sse_unterminated"),
        ([b'{"type":"response.completed"}'], "sse_unterminated"),
        ([], "missing_completed"),
        ([b"<html>not SSE</html>\n\n"], "missing_completed"),
        ([frame(successful()[0])], "missing_completed"),
        (
            [frame(successful()[0]), ConnectionResetError("private-error")],
            "stream_read_failed",
        ),
        ([frame(successful()[0]), frame(successful()[1])], "missing_completed"),
    ],
)
def test_missing_mime_never_substitutes_for_completed(
    tmp_path, monkeypatch, chunks, reason
):
    phase, facts, calls = execute(
        tmp_path, monkeypatch, Response(chunks, content_type=None)
    )
    assert phase["status"] == "unknown" and not phase["outputs"]
    assert len(calls) == 1 and not facts["response_completed_received"]
    assert facts["termination_reason"] == reason
    assert "private-error" not in json.dumps(phase)


@pytest.mark.parametrize("kind", ["response.failed", "response.incomplete", "error"])
def test_devkit_failure_events_never_complete_without_mime(tmp_path, monkeypatch, kind):
    error = {"code": "server_error", "message": "private-provider-message"}
    packet = (
        {"type": kind, **error}
        if kind == "error"
        else {"type": kind, "response": {"error": error}}
    )
    phase, facts, calls = execute(
        tmp_path,
        monkeypatch,
        Response([frame(successful()[0]), frame(packet)], content_type=None),
    )
    assert phase["status"] == "unknown" and not phase["outputs"] and len(calls) == 1
    assert facts["last_event_type"] == kind
    assert facts["api_error_code"] == "server_error"
    assert facts["termination_reason"] == "provider_failed"
    assert not facts["response_completed_received"]
    assert "private-provider-message" not in json.dumps(phase)


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirect_is_rejected_without_following_or_retry(tmp_path, monkeypatch, status):
    phase, facts, calls = execute(
        tmp_path,
        monkeypatch,
        Response([b""], status=status, content_type=None),
    )
    assert phase["status"] == "unknown" and len(calls) == 1
    assert facts["http_status"] == status
    assert facts["termination_reason"] == "http_rejected"


def test_stdlib_http_response_does_not_synthesize_content_type():
    class Socket:
        def makefile(self, *args):
            return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")

    response = HTTPResponse(Socket())
    response.begin()
    try:
        assert response.getheader("Content-Type") is None
        # The alternative email.message convenience API does invent this default.
        assert response.headers.get_content_type() == "text/plain"
        facts = TransportDiagnostic()
        facts.headers(response)
        assert facts.data["content_type"] is None
        assert facts.data["content_type_present"] is False
    finally:
        response.close()
