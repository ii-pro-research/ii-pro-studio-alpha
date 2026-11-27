import json
import threading
import time

from test_gpt_text import MockHTTP, prepare, studio
from test_gpt_team import decision, wait


class StreamingHTTP(MockHTTP):
    def __init__(self, parts, *, release=None, failure=False):
        super().__init__()
        self.parts, self.release, self.failure = parts, release, failure

    def stream(self, body, token, deadline):
        self.calls.append(body)
        try:
            yield {"type": "response.created", "response": {"id": "resp_stream"}}
            for part in self.parts:
                yield {"type": "response.output_text.delta", "delta": part}
                if self.release:
                    assert self.release.wait(5)
            if self.failure:
                yield {
                    "type": "response.failed",
                    "response": {
                        "error": {
                            "code": "subscription_sharing_usage_limit_exceeded",
                            "message": "private provider message",
                        }
                    },
                }
                return
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_stream",
                    "model": "gpt-6.2-sol",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {"type": "output_text", "text": "".join(self.parts)}
                            ],
                        }
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
                },
            }
        finally:
            self.closed = True


def test_provisional_text_is_visible_before_completed_and_not_shadow_knowledge(
    tmp_path,
):
    release = threading.Event()
    wire = StreamingHTTP(["П" * 500], release=release)
    t = studio(tmp_path, wire)
    try:
        phase = prepare(t)["phases"][0]
        t.team.decide(decision(phase))
        until = time.monotonic() + 4
        while time.monotonic() < until:
            current = t.team.job("a" * 32)["phases"][0]
            if any(
                e["agent_event"]["data"].get("provisional") for e in current["events"]
            ):
                break
            time.sleep(0.01)
        assert current["status"] == "running" and not current["outputs"]
        provisional = [
            e["agent_event"]["data"]["delta"]
            for e in current["events"]
            if e["agent_event"]["data"].get("provisional")
        ]
        assert "".join(provisional) == "П" * 400
        release.set()
        result = wait(t)["phases"][0]
        assert result["status"] == "succeeded"
        assert result["outputs"]["result.json"]["text"] == "П" * 500
        assert result["outputs"]["shadow-material.json"]["verified_knowledge"] is False
        assert len(wire.calls) == 1
    finally:
        release.set()
        t.close()


def test_large_stream_remains_within_existing_event_quotas(tmp_path):
    wire = StreamingHTTP(["A" * 15900])
    t = studio(tmp_path, wire)
    try:
        phase = prepare(t)["phases"][0]
        t.team.decide(decision(phase))
        result = wait(t)["phases"][0]
        assert result["status"] == "succeeded"
        assert len(result["outputs"]["result.json"]["text"]) == 15900
        assert len(result["events"]) < 128
    finally:
        t.close()


def test_credential_split_across_chunks_is_never_persisted(tmp_path):
    wire = StreamingHTTP(["A" * 400 + "fixture-access-", "credential"])
    t = studio(tmp_path, wire)
    try:
        phase = prepare(t)["phases"][0]
        t.team.decide(decision(phase))
        result = wait(t)["phases"][0]
        assert result["status"] == "unknown" and not result["outputs"]
        assert "fixture-access" not in json.dumps(result["events"])
        assert len(wire.calls) == 1
    finally:
        t.close()


def test_provider_limit_keeps_partial_text_provisional_and_does_not_retry(tmp_path):
    wire = StreamingHTTP(["A" * 500], failure=True)
    t = studio(tmp_path, wire)
    try:
        phase = prepare(t)["phases"][0]
        t.team.decide(decision(phase))
        result = wait(t)["phases"][0]
        assert result["status"] == "unknown" and not result["outputs"]
        events = json.dumps(result["events"])
        assert "subscription_sharing_usage_limit_exceeded" in events
        assert "private provider message" not in events
        assert len(wire.calls) == 1
    finally:
        t.close()
