import io
import json
import time
from types import SimpleNamespace

import pytest

from ii_pro_core.errors import ValidationError
from ii_pro_team import text
from ii_pro_team.siwc import request
from ii_pro_team.protocol import validate_request


class Response:
    def __init__(self, body, status=200, content_type="text/event-stream"):
        self.body = io.BytesIO(body)
        self.status = status
        self.content_type = content_type
        self.fp = None

    def read(self, n):
        return self.body.read(n)

    def readline(self, n):
        return self.body.readline(n)

    def getheader(self, k, default=""):
        return self.content_type if k == "Content-Type" else default


def transport(monkeypatch, response):
    calls = []

    class Connection:
        def __init__(self, host, timeout):
            assert host == "api.openai.com"
            self.sock = SimpleNamespace(settimeout=lambda timeout: None)
            self.closed = False

        def request(self, *args):
            calls.append(args)

        def connect(self):
            pass

        def getresponse(self):
            return response

        def close(self):
            self.closed = True

    monkeypatch.setattr(text, "HTTPSConnection", Connection)
    return text.ResponsesHTTPS(), calls


def test_fixed_https_sse_supports_multiline_and_never_uses_env_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://must-not-be-contacted.invalid")
    body = b': heartbeat\n\ndata: {"type":"response.created",\ndata: "response":{"id":"fixture"}}\n\ndata: [DONE]\n\n'
    client, calls = transport(monkeypatch, Response(body))
    events = list(
        client.stream({"input": []}, "private-fixture", time.monotonic() + 10)
    )
    assert events == [{"type": "response.created", "response": {"id": "fixture"}}]
    assert len(calls) == 1 and calls[0][0:2] == ("POST", "/v1/responses")
    assert calls[0][3]["Accept"] == "text/event-stream"


@pytest.mark.parametrize(
    "response",
    [
        Response(b"not SSE", content_type="text/html"),
        Response(b'data: {"type":"partial"}'),
        Response(b"x" * 65537 + b"\n"),
        Response(b"data: {bad}\n\n"),
    ],
)
def test_malformed_or_unbounded_stream_never_retries(monkeypatch, response):
    client, calls = transport(monkeypatch, response)
    with pytest.raises(text.TextUncertain):
        list(client.stream({}, "private-fixture", time.monotonic() + 10))
    assert len(calls) == 1


def test_admission_error_is_redacted_and_not_followed_or_retried(monkeypatch):
    client, calls = transport(
        monkeypatch,
        Response(
            json.dumps({"detail": "Bearer PRIVATE_SYNTHETIC_CREDENTIAL"}).encode(),
            status=403,
        ),
    )
    generator = client.stream({}, "fixture", time.monotonic() + 10)
    packet = next(generator)
    assert packet == {
        "type": "transport.rejected",
        "http_status": 403,
        "shape": "detail",
        "code": None,
    }
    with pytest.raises(text.TextUncertain):
        next(generator)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://api.openai.com/v1/models",
        "https://attacker.invalid/x",
        "https://api.openai.com@attacker.invalid/x",
        "https://api.openai.com/v1/models?token=x",
    ],
)
def test_oauth_metadata_transport_refuses_nonfixed_origins_before_network(url):
    with pytest.raises(ValidationError):
        request("GET", url)


@pytest.mark.parametrize(
    "extra",
    [
        {"file": "outside-canary.txt"},
        {"tools": [{"name": "read_file"}]},
        {"cwd": "C:/private"},
        {"previous_response_id": "foreign"},
    ],
)
def test_text_protocol_rejects_files_tools_paths_and_foreign_context(extra):
    value = {
        "version": 2,
        "job_id": "a" * 32,
        "session_id": "b" * 32,
        "role": "architect",
        "phase": "plan",
        "project_id": "gpt-text",
        "text": "plan",
        "inputs": {
            "model": "gpt-6.2-sol",
            "effort": "low",
            "profile_digest": "c" * 64,
            **extra,
        },
    }
    with pytest.raises(ValidationError):
        validate_request(value)
