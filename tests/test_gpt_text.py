import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ii_pro_core.errors import Conflict, RecoveryRequired, ValidationError
from ii_pro_team.siwc import Grant, PLAN_SCOPE
from ii_pro_team.text import FIRST_PROMPT, MODEL, SiwcTextAdapter
from ii_pro_ui.training import Training
from test_gpt_team import decision, wait


def grant():
    return Grant(
        "oaiapp_fixture",
        "subject",
        "urn:uuid:fixture",
        "fresh-grant",
        time.time() + 3600,
        (PLAN_SCOPE, "resource.invoke"),
        (MODEL,),
        "fixture-access-credential",
        "fixture-refresh-credential",
        "fixture-id-credential",
    )


class MockHTTP:
    def __init__(self, fault=None):
        self.calls, self.fault, self.closed = [], fault, False
        self.tool_path = "outside-canary.txt"

    def stream(self, body, token, deadline):
        self.calls.append(body)
        try:
            yield {"type": "response.created", "response": {"id": "resp_fixture"}}
            if self.fault == "timeout":
                raise TimeoutError("fixture only")
            if self.fault == "failed":
                yield {
                    "type": "response.failed",
                    "response": {"error": {"message": "never store credential"}},
                }
                return
            if self.fault == "incomplete":
                yield {"type": "response.incomplete"}
                return
            if self.fault == "lost":
                return
            if self.fault == "tool":
                yield {
                    "type": "response.output_item.added",
                    "item": {
                        "type": "function_call",
                        "name": "read_file",
                        "arguments": self.tool_path,
                    },
                }
                return
            text = "План разработчика для Mock; не ответ настоящего GPT."
            if self.fault == "leak":
                text = "fixture-access-credential"
            yield {
                "type": "response.output_text.delta",
                "delta": "x" * 16001 if self.fault == "oversize" else text,
            }
            yield {
                "type": "response.reasoning_text.delta",
                "delta": "PRIVATE_REASONING_FIXTURE",
            }
            output = [
                {
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            ]
            if self.fault == "completed_tool":
                output.append({"type": "function_call", "name": "execute"})
            yield {
                "type": "response.completed",
                "response": {
                    "id": "wrong" if self.fault == "identity" else "resp_fixture",
                    "status": "completed",
                    "model": MODEL,
                    "output": output,
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 18,
                        "total_tokens": 30,
                        "private": "do not save",
                    },
                },
            }
        finally:
            self.closed = True


def studio(tmp_path, wire, *, session="one", credential=None):
    t = Training(
        tmp_path / "state",
        session,
        include_ollama=True,
        include_team=True,
        text_adapter_factory=lambda: SiwcTextAdapter(
            credential or grant(), transport=wire, mock=True
        ),
    )
    t._open()
    return t


def prepare(t, key="a"):
    return t.team.prepare(
        {"request_id": key * 32, "project_id": "gpt-text", "text": FIRST_PROMPT}
    )


def test_text_full_route_frozen_approval_output_and_shadow(tmp_path):
    wire = MockHTTP()
    t = studio(tmp_path, wire)
    try:
        job = prepare(t)
        phase = job["phases"][0]
        assert phase["status"] == "created" and wire.calls == []
        t.team.decide(decision(phase))
        job = wait(t)
        p = job["phases"][0]
        assert p["status"] == "succeeded" and p["execution_stopped"]
        result = p["outputs"]["result.json"]
        assert not result["real_inference"] and result["usage"]["total_tokens"] == 30
        assert not result["computer_access"]
        assert p["outputs"]["shadow-material.json"]["verified_knowledge"] is False
        assert len(wire.calls) == 1 and wire.closed
        body = wire.calls[0]
        assert body["store"] is False and body["stream"] is True
        assert body["tools"] == [] and body["tool_choice"] == "none"
        assert set(body) == {
            "model",
            "reasoning",
            "input",
            "store",
            "stream",
            "tools",
            "tool_choice",
        }
        assert all(m["role"] != "system" for m in body["input"])
        assert body["input"][1]["content"] == FIRST_PROMPT
        from pathlib import Path

        proposal = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "tests/fixtures/architect-request-body.json"
            ).read_text(encoding="utf-8")
        )
        assert proposal == body
        assert not (t.team.laboratory.root / job["job_id"]).exists()
        dumped = json.dumps([e.payload for e in t.school.core.events()]) + json.dumps(
            p["outputs"]
        )
        for hidden in (
            "fixture-access-credential",
            "fixture-refresh-credential",
            "fixture-id-credential",
            "PRIVATE_REASONING_FIXTURE",
        ):
            assert hidden not in dumped
        t.school.core.artifacts.validate_run(p["run_id"], require_any=True)
        from ii_pro_ui.reader import Source, read_source

        observed = read_source(Source("text", "Text", t.root), tmp_path / "snapshots")
        observed_run = next(r for r in observed["runs"] if r["id"] == p["run_id"])
        assert (
            observed_run["text_generation"]
            and observed_run["backend"] == "GPT Text · Mock"
        )
        assert observed_run["real_inference"] is False
        t.team.decide(decision(phase))
        assert len(wire.calls) == 1
        with pytest.raises(Conflict):
            t.team.advance({"job_id": job["job_id"], "phase": "implement"})
    finally:
        t.close()


def test_parallel_prepare_and_double_confirm_dispatch_exactly_once(tmp_path):
    wire = MockHTTP()
    t = studio(tmp_path, wire)
    try:
        with ThreadPoolExecutor(4) as pool:
            jobs = list(pool.map(lambda _: prepare(t), range(4)))
        assert len({j["phases"][0]["run_id"] for j in jobs}) == 1
        body = decision(jobs[-1]["phases"][0])
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda _: t.team.decide(body), range(4)))
        assert wait(t)["phases"][0]["status"] == "succeeded"
        assert len(wire.calls) == 1
    finally:
        t.close()


@pytest.mark.parametrize(
    "fault",
    [
        "timeout",
        "failed",
        "incomplete",
        "lost",
        "tool",
        "oversize",
        "identity",
        "completed_tool",
    ],
)
def test_ambiguous_and_unexpected_results_are_unknown_never_replayed(tmp_path, fault):
    wire = MockHTTP(fault)
    t = studio(tmp_path, wire)
    try:
        phase = prepare(t)["phases"][0]
        t.team.decide(decision(phase))
        assert wait(t)["phases"][0]["status"] == "unknown"
        with pytest.raises(RecoveryRequired):
            prepare(t, "b")
        assert len(wire.calls) == 1 and wire.closed
    finally:
        t.close()
    t = studio(tmp_path, wire, session="restart")
    try:
        job = t.team.job("a" * 32)
        assert job["phases"][0]["status"] == "unknown" and len(wire.calls) == 1
    finally:
        t.close()


def test_revocation_denial_and_session_restart_require_new_approval(tmp_path):
    wire = MockHTTP()
    t = studio(tmp_path, wire)
    phase = prepare(t)["phases"][0]
    t.team.revoke({k: v for k, v in decision(phase).items() if k != "allow"})
    with pytest.raises(Conflict):
        t.team.decide(decision(phase))
    t.close()
    t = studio(tmp_path, wire, session="new")
    try:
        with pytest.raises(Conflict):
            t.team.decide(decision(phase))
        fresh = prepare(t)["phases"][0]
        assert fresh["approval"]["id"] != phase["approval"]["id"]
        assert fresh["run_id"] == phase["run_id"]
        t.team.decide(decision(fresh, False))
        assert wire.calls == []
    finally:
        t.close()


def test_grant_model_and_prompt_mutation_cannot_reuse_permission(tmp_path):
    wire = MockHTTP()
    t = studio(tmp_path, wire)
    try:
        phase = prepare(t)["phases"][0]
        t.team.text_executor.adapter.model = "changed-model"
        with pytest.raises(Conflict):
            t.team.decide(decision(phase))
        assert wire.calls == []
        with pytest.raises(ValidationError):
            t.team.prepare(
                {
                    "request_id": "b" * 32,
                    "project_id": "gpt-text",
                    "text": "another prompt",
                }
            )
    finally:
        t.close()


def test_real_transport_cannot_use_mock_bypass_and_no_exact_host_permit(tmp_path):
    with pytest.raises(Conflict):
        SiwcTextAdapter(grant(), mock=True)
    wire = MockHTTP()
    t = studio(tmp_path, wire)
    try:
        t.team.text_executor.adapter.mock = False
        p = prepare(t)["phases"][0]
        with pytest.raises(Conflict):
            t.team.decide(decision(p))
        assert (
            wire.calls == []
            and t.school.core.approvals.get(p["approval"]["id"]).status == "pending"
        )
    finally:
        t.close()


def test_text_has_no_path_or_tool_dispatch_for_outside_canary(tmp_path):
    canary = tmp_path / "outside-canary.txt"
    canary.write_text("PRIVATE_CANARY_VALUE")
    wire = MockHTTP("tool")
    wire.tool_path = str(canary)
    t = studio(tmp_path, wire)
    try:
        p = prepare(t)["phases"][0]
        t.team.decide(decision(p))
        j = wait(t)
        assert j["phases"][0]["status"] == "unknown"
        assert "PRIVATE_CANARY_VALUE" not in json.dumps(j) + json.dumps(wire.calls)
        assert canary.read_text() == "PRIVATE_CANARY_VALUE"
    finally:
        t.close()


def test_new_oauth_grant_invalidates_old_approval_even_for_same_account(tmp_path):
    from dataclasses import replace

    wire = MockHTTP()
    t = studio(tmp_path, wire)
    try:
        p = prepare(t)["phases"][0]
        before = t.team.job("a" * 32)["text_profile"]
        adapter = t.team.text_executor.adapter
        adapter.grant = replace(adapter.grant, grant_id="new-independent-consent")
        assert t.team.job("a" * 32)["text_profile"] == before
        with pytest.raises(Conflict):
            t.team.decide(decision(p))
        assert wire.calls == []
    finally:
        t.close()


def test_lab_approval_cannot_authorize_text_or_reuse_its_job_identity(tmp_path):
    from test_gpt_team import preparation

    wire = MockHTTP()
    t = studio(tmp_path, wire)
    try:
        lab = t.team.prepare(preparation("b"))
        p = prepare(t)["phases"][0]
        body = decision(p)
        body["approval_id"] = lab["phases"][0]["approval"]["id"]
        with pytest.raises(Conflict):
            t.team.decide(body)
        with pytest.raises(Conflict):
            t.team.prepare(preparation("a"))
        assert wire.calls == []
    finally:
        t.close()


def test_corrupt_studio_auth_store_does_not_take_existing_school_offline(tmp_path):
    root = tmp_path / "gpt-text-auth"
    root.mkdir()
    (root / "active.dpapi").write_bytes(b"broken synthetic data")
    t = Training(tmp_path / "state", "session", include_ollama=True, include_team=True)
    try:
        t._open()
        state = t.team.state()
        assert state["text"]["auth_error"] and not state["text"]["auth_ready"]
        assert t.school.core.state == "running"
    finally:
        t.close()


def test_replaced_or_removed_studio_credentials_disable_cached_generation(tmp_path):
    adapter = SiwcTextAdapter(grant(), transport=MockHTTP(), mock=True)
    adapter.current_reference = lambda: adapter.grant.reference
    assert adapter.ready()
    adapter.current_reference = lambda: "different-new-grant"
    assert not adapter.ready()
    adapter.current_reference = lambda: None
    assert not adapter.ready()


def test_credential_echo_is_never_saved_as_response_or_event(tmp_path):
    wire = MockHTTP("leak")
    t = studio(tmp_path, wire)
    try:
        p = prepare(t)["phases"][0]
        t.team.decide(decision(p))
        job = wait(t)
        assert job["phases"][0]["status"] == "unknown"
        data = json.dumps(job) + json.dumps([e.payload for e in t.school.core.events()])
        assert "fixture-access-credential" not in data and len(wire.calls) == 1
    finally:
        t.close()
