import json
import time
from types import SimpleNamespace

import pytest

from ii_pro_core.errors import Conflict, RecoveryRequired
from ii_pro_team import text
from ii_pro_team.devkit import BRIDGE, DevkitTextAdapter, NodeBridge
from ii_pro_ui.training import Training
from test_gpt_text import grant
from test_gpt_team import decision, wait


def require_devkit():
    if not (BRIDGE / "vendor").exists():
        pytest.skip("Official DevKit transport omitted from the public Alpha")


class MockBridge(NodeBridge):
    mock = True

    def __init__(self, mode="success"):
        require_devkit()
        self.mode = mode
        super().__init__()
        self.entry = BRIDGE / "test/mock-runner.mjs"
        assert self.available

    def environment(self):
        return {**super().environment(), "II_PRO_TEST_CASE": self.mode}


def owner(tmp_path, mode="success"):
    require_devkit()
    studio = Training(
        tmp_path / "state",
        "devkit-local-session",
        include_ollama=True,
        include_team=True,
        text_adapter_factory=lambda: DevkitTextAdapter(
            grant(), bridge=MockBridge(mode), mock=True
        ),
    )
    studio._open()
    return studio


def prepare(studio):
    return studio.team.prepare(
        {
            "request_id": "d" * 32,
            "project_id": "gpt-text",
            "text": text.DIAGNOSTIC_PROMPT,
        }
    )["phases"][0]


def test_official_node_stream_flows_through_existing_core_and_artifacts(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        text.ResponsesHTTPS,
        "stream",
        lambda *a, **k: pytest.fail("Legacy transport reached"),
    )
    studio = owner(tmp_path)
    try:
        phase = prepare(studio)
        assert phase["status"] == "created" and not phase["events"]
        spec = studio.school.core.approvals.describe(phase["approval"]["id"])
        assert spec["action"]["parameters"]["inputs"]["effort"] == "sdk_default"
        assert spec["executor"]["configuration"]["adapter"]["effort"] == "sdk_default"
        studio.team.decide(decision(phase))
        phase = wait(studio, "d" * 32)["phases"][0]
        assert phase["status"] == "succeeded"
        result = phase["outputs"]["result.json"]
        assert (
            result["text"] == "Developer Mock answer"
            and result["real_inference"] is False
        )
        assert result["usage"] is None and result["request_id"] is None
        assert result["transport_diagnostic"]["response_completed_received"] is True
        assert result["transport_diagnostic"]["sse_events"] is None
        assert result["transport_diagnostic"]["content_type_present"] is None
        assert "NOT PUBLIC" not in json.dumps(phase)
        assert grant().access_token not in json.dumps(phase)
        assert phase["outputs"]["shadow-material.json"]["qwen_called"] is False
        assert (
            studio.team.decide(decision(phase))["phases"][0]["run_id"]
            == phase["run_id"]
        )
    finally:
        studio.close()


@pytest.mark.parametrize("mode", ["http", "eof", "secret"])
def test_devkit_failures_preserve_unknown_and_never_retry(tmp_path, mode):
    studio = owner(tmp_path, mode)
    try:
        phase = prepare(studio)
        studio.team.decide(decision(phase))
        phase = wait(studio, "d" * 32)["phases"][0]
        assert phase["status"] == "unknown" and not phase["outputs"]
        assert phase["approval"]["status"] == "consumed"
        assert (
            len(
                studio.school.store.all(
                    "SELECT 1 FROM events WHERE kind='run.started' AND run_id=?",
                    (phase["run_id"],),
                )
            )
            == 1
        )
        with pytest.raises(RecoveryRequired):
            studio.team.decide(decision(phase))
        assert grant().access_token not in json.dumps(phase)
    finally:
        studio.close()


def test_official_bridge_cancel_is_local_only_and_closes_owned_child():
    cancellation = SimpleNamespace(requested=False)
    stream = MockBridge("hang").stream(
        "fixture", grant().access_token, "a" * 32, cancellation=cancellation
    )
    assert next(stream)["type"] == "started"
    cancellation.requested = True
    with pytest.raises(text.TextUncertain) as caught:
        next(stream)
    assert caught.value.reason == "cancelled"


def test_team_stop_keeps_cloud_effect_unknown_and_preserves_consumed_approval(tmp_path):
    studio = owner(tmp_path, "hang")
    try:
        phase = prepare(studio)
        studio.team.decide(decision(phase))
        core = studio.team.core
        run_id = phase["run_id"]
        until = time.monotonic() + 8
        while time.monotonic() < until and not core.store.all(
            "SELECT 1 FROM events WHERE run_id=? AND kind='team.agent_event'", (run_id,)
        ):
            time.sleep(0.02)
        studio.team.stop({"run_id": run_id})
        stopped = wait(studio, "d" * 32)["phases"][0]
        assert stopped["status"] == "unknown" and stopped["approval"]["status"] == "consumed"
        assert not core.runs.get(run_id).execution_stopped and not stopped["outputs"]
        events = core.store.all(
            "SELECT payload_json FROM events WHERE run_id=? AND kind='team.agent_event'", (run_id,)
        )
        diagnostic = json.loads(events[-1][0])["agent_event"]["data"]
        assert diagnostic["termination_reason"] == "cancelled"
        assert not diagnostic["response_completed_received"]
        with pytest.raises(RecoveryRequired):
            studio.team.decide(decision(phase))
    finally:
        studio.close()


def test_official_bridge_deadline_never_reports_completion():
    packets = list(
        MockBridge("hang").stream(
            "fixture", grant().access_token, "a" * 32, seconds=0.5
        )
    )
    assert [packet["type"] for packet in packets] == ["started", "error"]
    assert packets[-1]["code"] == "deadline"


def test_default_adapter_is_devkit_without_legacy_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(text.Vault, "load", lambda self: grant())
    executor = text.build_text(tmp_path / "workspace")
    assert isinstance(executor.adapter, DevkitTextAdapter)
    assert executor.adapter.configuration()["backend"] == "siwc_devkit"
    assert executor.authorized_run is None
    assert executor.adapter.configuration()["effort"] == "sdk_default"


def test_bridge_environment_excludes_ambient_credentials_and_node_injection(
    monkeypatch,
):
    monkeypatch.setenv("NODE_OPTIONS", "--import untrusted.mjs")
    monkeypatch.setenv("OPENAI_API_KEY", "untrusted")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://foreign.invalid")
    environment = NodeBridge.environment()
    assert (
        not {"NODE_OPTIONS", "OPENAI_API_KEY", "OPENAI_BASE_URL"} & environment.keys()
    )


def test_legacy_approval_cannot_be_used_for_official_devkit(tmp_path):
    studio = owner(tmp_path)
    try:
        phase = prepare(studio)
        with pytest.raises(Conflict):
            studio.team.decide({**decision(phase), "spec_digest": "a" * 64})
        assert studio.team.job("d" * 32)["phases"][0]["status"] == "created"
    finally:
        studio.close()


def test_sdk_change_invalidates_previously_prepared_legacy_operation(tmp_path):
    require_devkit()
    from test_gpt_text import (
        MockHTTP,
        studio as legacy_studio,
        prepare as legacy_prepare,
    )

    client = MockHTTP()
    studio = legacy_studio(tmp_path, client)
    try:
        phase = legacy_prepare(studio)["phases"][0]
        studio.team.text_executor.adapter = DevkitTextAdapter(
            grant(), bridge=MockBridge(), mock=True
        )
        with pytest.raises(Conflict, match="Operation changed"):
            studio.team.decide(decision(phase))
        phase = studio.team.job("a" * 32)["phases"][0]
        assert phase["status"] == "created" and not phase["outputs"]
        assert client.calls == []
    finally:
        studio.close()


def test_missing_node_never_selects_legacy_transport(monkeypatch):
    monkeypatch.setattr("ii_pro_team.devkit.shutil.which", lambda name: None)
    adapter = DevkitTextAdapter(grant())
    assert not adapter.ready()
    assert adapter.configuration()["backend"] == "siwc_devkit"


def test_official_source_snapshot_is_unmodified():
    from ii_pro_team.devkit import sha, REVISION

    require_devkit()
    provenance = json.loads((BRIDGE / "vendor/PROVENANCE.json").read_text())
    assert provenance["revision"] == REVISION and provenance["modified"] is False
    for upstream, expected in provenance["files_sha256"].items():
        local = (
            "local/" + upstream.removeprefix("packages/local/")
            if upstream.startswith("packages/local/")
            else upstream
        )
        assert sha(BRIDGE / "vendor" / local) == expected


def test_many_valid_sse_deltas_fit_inside_local_bridge_budget():
    packets = list(
        MockBridge("many_chunks").stream("fixture", grant().access_token, "a" * 32)
    )
    assert packets[0] == {"version": 1, "type": "started"}
    assert packets[-1] == {"version": 1, "type": "completed", "text": "abcd" * 180}
    assert "".join(packet["text"] for packet in packets[1:-1]) == packets[-1]["text"]
    assert len(packets) <= 128


def test_many_valid_sse_deltas_without_completion_still_fail():
    packets = list(
        MockBridge("many_chunks_eof").stream("fixture", grant().access_token, "a" * 32)
    )
    assert packets[-1]["type"] == "error"
    assert packets[-1]["code"] == "stream_interrupted"
    assert not any(packet["type"] == "completed" for packet in packets)
    assert "".join(packet["text"] for packet in packets[1:-1]) == "abcd" * 180

def test_project_engineer_bridge_applies_bound_deadline_and_checks_grant(monkeypatch):
    from dataclasses import replace
    from time import time

    adapter = DevkitTextAdapter(grant(), bridge=MockBridge(), mock=True)
    calls = []

    def stream(prompt, token, request_id, *, role, cancellation, seconds=60):
        calls.append((role, seconds))
        yield {"type": "started"}
        yield {"type": "delta", "text": "fixture"}
        yield {"type": "completed", "text": "fixture"}

    monkeypatch.setattr(adapter.bridge, "stream", stream)
    previous = {"edit_protocol": "fragments-v1", "abandoned_proposal": "d" * 32}
    ctx = {"snapshot": "e" * 64, "source": {"app.py": "pass\n"}, "allowed": ["app.py"],
           "acceptance": "Preserve behavior", "previous": previous}
    requested = {"version": 4, "job_id": "a" * 32, "session_id": "b" * 32, "role": "engineer",
                 "phase": "implement", "project_id": "task-manager-filter", "text": "fixture",
                 "inputs": {"model": "gpt-6.2-sol", "effort": "sdk_default", "profile_digest": "f" * 64,
                            "context": ctx, "scope": "a" * 64, "deadline_seconds": 120}}
    result = adapter.run(requested, lambda _: None)
    assert result["deadline_seconds"] == 120
    assert calls == [("project_engineer_fragments", 120)]
    adapter.grant = replace(adapter.grant, expires_at=time() + 75)
    assert adapter.ready() and not adapter.ready_for_deadline(120)
    with pytest.raises(text.TextUncertain):
        adapter.run(requested, lambda _: None)
    assert len(calls) == 1
