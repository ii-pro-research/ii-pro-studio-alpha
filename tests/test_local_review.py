import json
import time
from hashlib import sha256

import pytest
from test_ollama_adapter import server

from ii_pro_core import Action, Core
from ii_pro_core.errors import ApprovalRequired, RecoveryRequired, ValidationError
from ii_pro_core.executors import SimulatedInterruption
from ii_pro_core.operations import ExecutionPolicy, executor_profile
from ii_pro_local import LocalReviewExecutor
from ii_pro_school.adapters import LoopbackTransport, ModelProfile


def make_executor():
    return LocalReviewExecutor(ModelProfile.build("qwen7b-fast:latest", "a" * 64))


def make_action(executor):
    return Action.build(
        "local.review",
        {
            "profile_digest": executor.approval_configuration()["profile_digest"],
            "prompt": "Review the supplied Python function.",
        },
    )


def test_local_action_requires_approval():
    action = make_action(make_executor())
    assert action.requires_approval
    assert ExecutionPolicy().requires_approval(action)


def test_local_action_rejects_extra_fields():
    with pytest.raises(ValidationError):
        Action.build(
            "local.review",
            {
                "profile_digest": "a" * 64,
                "prompt": "Review code",
                "command": "not allowed",
            },
        )


def test_local_action_rejects_bad_digest():
    with pytest.raises(ValidationError):
        Action.build(
            "local.review", {"profile_digest": "invalid", "prompt": "Review code"}
        )


def test_executor_has_versioned_configuration():
    executor = make_executor()
    profile = executor_profile(executor)

    assert profile["name"] == "local_review"
    assert profile["configuration"]["mode"] == "text_only"
    assert profile["configuration"]["automatic_tools"] is False


def test_local_run_can_be_prepared(tmp_path, monkeypatch):
    calls = []

    def blocked(*args, **kwargs):
        calls.append("network")
        raise AssertionError("Unexpected model request")

    monkeypatch.setattr(LoopbackTransport, "request", blocked)

    executor = make_executor()

    with Core(tmp_path / "state", executors=[executor]) as core:
        task = core.tasks.create(
            "Local review", executor=executor.name, idempotency_key="local-task"
        )

        core.tasks.transition(task.id, "planned")

        run = core.runs.create(
            task.id, action=make_action(executor), idempotency_key="local-run"
        )

        approval = core.approvals.request(run.id, ttl_seconds=1800)
        with pytest.raises(ApprovalRequired):
            core.runs.execute(run.id, approval_id=approval.id)

        assert calls == []


@pytest.mark.parametrize(
    "case", ["ok", "digest", "profile", "incomplete", "oversize", "tools", "crash"]
)
def test_local_execution_and_recovery(tmp_path, monkeypatch, case):
    executor = make_executor()
    calls = []
    answer = "raise RuntimeError('must remain text')"

    def request(self, route, payload=None, **kwargs):
        calls.append(route)
        if route == "version":
            return {"version": "fixture"}
        if route == "tags":
            return {
                "models": [
                    {
                        "name": "qwen7b-fast:latest",
                        "digest": ("b" if case == "digest" else "a") * 64,
                    }
                ]
            }
        if route == "show":
            return {}
        assert route == "chat" and payload["stream"] is False
        assert "tools" not in payload
        if case == "crash":
            raise SimulatedInterruption()
        message = {
            "role": "assistant",
            "content": "x" * 8001 if case == "oversize" else answer,
        }
        if case == "tools":
            message["tool_calls"] = [{"function": {"name": "ignored"}}]
        return {
            "model": "qwen7b-fast:latest",
            "message": message,
            "done": True,
            "done_reason": "length" if case == "incomplete" else "stop",
        }

    monkeypatch.setattr(LoopbackTransport, "request", request)
    with Core(tmp_path, executors=[executor]) as core:
        task = core.tasks.create(
            "Review", executor=executor.name, idempotency_key="task"
        )
        core.tasks.transition(task.id, "planned")
        action = make_action(executor)
        if case == "profile":
            action = Action.build(
                "local.review", {"profile_digest": "b" * 64, "prompt": "Review"}
            )
        run = core.runs.create(task.id, action=action, idempotency_key="run")
        permit = core.approvals.request(run.id)
        core.approvals.decide(permit.id, allow=True)
        assert not calls
        if case == "crash":
            with pytest.raises(SimulatedInterruption):
                core.runs.execute(run.id, approval_id=permit.id)
        else:
            result = core.runs.execute(run.id, approval_id=permit.id)
            if case == "ok":
                assert result.status == "succeeded"
                blobs = {
                    a.relative_path.rsplit("/", 1)[-1]: core.artifacts.read(a.id)
                    for a in core.artifacts.list(run.id)
                }
                assert blobs["response.txt"].decode() == answer
                assert json.loads(blobs["receipt.json"])["code_executed"] is False
            elif case in {"digest", "profile"}:
                assert (
                    result.status == "failed" and result.effect_status == "not_started"
                )
            else:
                assert result.status == "unknown"
                records = {a.relative_path.rsplit("/", 1)[-1]: core.artifacts.read(a.id)
                           for a in core.artifacts.list(run.id)}
                assert json.loads(records["diagnostic.json"])["classification"] == {
                    "incomplete": "generation_limit", "oversize": "invalid_content", "tools": "invalid_envelope",
                }[case]
    with Core(tmp_path, executors=[executor]) as core:
        if case in {"crash", "tools", "oversize", "incomplete"}:
            assert core.runs.get(run.id).status == "unknown"
            with pytest.raises(RecoveryRequired):
                core.runs.execute(run.id, approval_id=permit.id)
    assert calls.count("chat") == (0 if case in {"digest", "profile"} else 1)


@pytest.mark.parametrize(("case", "classification"), [
    ("ok", "completed"), ("budget", "generation_limit"), ("missing_reason", "unconfirmed_result"),
    ("partial_json", "incomplete_json"), ("bad_json", "invalid_json"), ("timeout", "timeout"),
    ("dropped", "transport_error"), ("sensitive", "invalid_content"),
])
def test_local_seals_safe_wire_evidence_even_after_failure(tmp_path, case, classification):
    secret = "sk-" + "s" * 32
    data = {"model": "qwen7b-fast:latest", "done": True, "done_reason": "stop",
            "message": {"role": "assistant", "content": "original answer\n"}, "eval_count": 384,
            "prompt_eval_count": 221, "eval_duration": 25, "load_duration": 6, "total_duration": 33}
    if case == "budget":
        data["done_reason"] = "length"
    elif case == "missing_reason":
        data.pop("done_reason")
    elif case == "sensitive":
        data["message"]["content"] = secret
        data["prompt_eval_duration"] = secret
        data["ignored_secret"] = secret
    body = {"partial_json": b'{"done":', "bad_json": b'{"done":@}'}.get(case, json.dumps(data).encode())

    def reply(handler):
        if handler.path != "/api/chat":
            value = {"/api/version": {"version": "mock"}, "/api/tags": {
                "models": [{"name": "qwen7b-fast:latest", "digest": "a" * 64}]}, "/api/show": {}}[handler.path]
            raw = json.dumps(value).encode()
        elif case == "timeout":
            time.sleep(.25)
            return
        elif case == "dropped":
            return
        else:
            raw = body
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    with server(reply) as (transport, calls):
        executor = LocalReviewExecutor(ModelProfile.build("qwen7b-fast:latest", "a" * 64,
                                       endpoint=f"http://127.0.0.1:{transport.port}", deadline_seconds=.12))
        with Core(tmp_path, executors=[executor]) as core:
            task = core.tasks.create("Review", executor=executor.name, idempotency_key="wire-task-1")
            core.tasks.transition(task.id, "planned")
            run = core.runs.create(task.id, action=make_action(executor), idempotency_key="wire-1")
            approval = core.approvals.request(run.id)
            core.approvals.decide(approval.id, allow=True)
            out = core.runs.execute(run.id, approval_id=approval.id)
            assert out.status == ("succeeded" if case == "ok" else "unknown")
            records = {a.relative_path.rsplit("/", 1)[-1]: core.artifacts.read(a.id) for a in core.artifacts.list(run.id)}
            facts = json.loads(records["diagnostic.json"])
            assert facts["classification"] == classification and facts["request_id"] == run.id
            assert facts["num_predict"] == 384
            if case not in {"dropped", "timeout"}:
                assert facts["http_status"] == 200 and facts["response_sha256"] == sha256(body).hexdigest()
                assert facts["response_bytes"] == len(body)
            else:
                assert facts["http_status"] == "unavailable"
            if case in {"ok", "budget", "missing_reason", "sensitive"}:
                assert facts["eval_count"] == 384 and facts["prompt_eval_count"] == 221
                assert facts["prompt_eval_duration"] == "unavailable"
            if case == "missing_reason":
                assert facts["done"] is True and facts["done_reason"] == "unavailable"
            if case == "sensitive":
                assert "response.txt" not in records
                assert all(secret.encode() not in data for data in records.values())
                assert all(secret not in row[0] for row in core.store.all("SELECT payload_json FROM events"))
            if case != "ok":
                with pytest.raises(RecoveryRequired):
                    core.runs.execute(run.id, approval_id=approval.id)
        assert calls.count("/api/chat") == 1
