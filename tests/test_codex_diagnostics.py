import hashlib
import json
from pathlib import Path

import pytest

from ii_pro_core.errors import RecoveryRequired, ValidationError
from ii_pro_team.app_server import TransportLost
from ii_pro_team.diagnostics import (
    CliIdentity,
    DiagnosticTransport,
    child_environment,
    arguments,
)
from test_gpt_team import training


CONFIG = {
    "sandbox_mode": "read-only",
    "approval_policy": "on-request",
    "approvals_reviewer": "user",
    "forced_login_method": "chatgpt",
    "mcp_servers": {},
    "apps": {"_default": {"enabled": False}},
    "agents": {"enabled": False},
    "web_search": "disabled",
    "features": {
        key: False
        for key in (
            "shell_tool",
            "unified_exec",
            "apply_patch_freeform",
            "js_repl",
            "code_mode",
        )
    },
}


class MockServer:
    def __init__(self, *, fault=None, config=None, authenticated=True):
        self.sent, self.frames = [], []
        self.fault, self.config, self.authenticated = (
            fault,
            config or CONFIG,
            authenticated,
        )
        self.closed = False

    def send(self, frame):
        self.sent.append(frame)
        method = frame.get("method")
        if method == "initialized":
            return
        if method == self.fault:
            self.frames.append(
                {
                    "id": frame["id"],
                    "error": {"code": 429, "message": "private backend secret=fixture"},
                }
            )
            return
        result = {
            "initialize": {
                "userAgent": "fixture",
                "codexHome": "C:/private/auth",
                "platformOs": "windows",
            },
            "config/read": {"config": self.config},
            "account/read": {
                "account": {
                    "type": "chatgpt",
                    "email": "never-store@example.invalid",
                    "token": "secret=fixture",
                }
                if self.authenticated
                else None,
                "requiresOpenaiAuth": True,
            },
            "model/list": {
                "data": [
                    {
                        "model": "fixture-gpt",
                        "isDefault": True,
                        "description": "secret=fixture",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low", "description": "private"}
                        ],
                    }
                ],
                "nextCursor": None,
            },
        }[method]
        self.frames.append({"id": frame["id"], "result": result})

    def receive(self, timeout):
        if not self.frames:
            raise TransportLost("Mock disconnected")
        return self.frames.pop(0)

    def close(self):
        self.closed = True
        return True


@pytest.fixture
def diagnostic(tmp_path):
    t = training(tmp_path)
    binary = tmp_path / "developer-codex.exe"
    binary.write_bytes(b"fixed mock transport; never executed")
    executor = t.diagnostics.executor
    executor.cli = CliIdentity(
        str(binary),
        hashlib.sha256(binary.read_bytes()).hexdigest(),
        "0.171.0-alpha.13",
        "0.171.0-alpha.13",
    )
    server = MockServer()
    executor.transport_factory = lambda cli, cwd: server
    yield t, server
    t.close()


def test_complete_diagnostic_is_core_run_and_sealed_redacted_artifact(diagnostic):
    t, server = diagnostic
    run = t.diagnostics.run("d" * 32)
    assert run.status == "succeeded" and run.execution_stopped == 1
    state = t.diagnostics.state()
    assert (
        state["auth"] == "confirmed"
        and state["connection"] == "closed"
        and state["stop_confirmed"]
    )
    assert state["models"][0]["access_verified"] is False
    assert (
        all(state["restrictions"].values())
        and state["turns"] == state["agent_sessions"] == 0
    )
    assert server.closed and t.school.core.active_run_ids == ()
    assert [r["method"] for r in server.sent] == [
        "initialize",
        "initialized",
        "config/read",
        "account/read",
        "model/list",
    ]
    output = json.dumps(state)
    assert (
        "secret=fixture" not in output
        and "never-store" not in output
        and "C:/private" not in output
    )
    all_events = json.dumps([e.payload for e in t.school.core.events()])
    assert "secret=fixture" not in all_events and "never-store" not in all_events
    t.school.core.artifacts.validate_run(run.id, require_any=True)
    assert len(t.school.core.artifacts.list(run.id)) == 1


def test_explicit_request_id_is_never_executed_twice(diagnostic):
    t, server = diagnostic
    run = t.diagnostics.run("d" * 32)
    calls = len(server.sent)
    assert t.diagnostics.run("d" * 32).id == run.id
    assert len(server.sent) == calls


@pytest.mark.parametrize(
    "method",
    [
        "thread/start",
        "turn/start",
        "thread/resume",
        "turn/interrupt",
        "process/spawn",
        "config/value/write",
        "account/login/start",
        "account/logout",
    ],
)
def test_wire_gate_cannot_start_agent_or_change_configuration(method):
    server = MockServer()
    gate = DiagnosticTransport(server, Path("fixture"))
    with pytest.raises(ValidationError):
        gate.send({"id": 1, "method": method, "params": {}})
    assert server.sent == []


def test_gate_also_blocks_grants_refresh_and_unapproved_parameters():
    server = MockServer()
    gate = DiagnosticTransport(server, "fixed")
    for frame in (
        {"id": 1, "result": {"decision": "accept"}},
        {"id": 1, "method": "account/read", "params": {"refreshToken": True}},
        {
            "id": 1,
            "method": "config/read",
            "params": {"includeLayers": True, "cwd": "fixed"},
        },
        {
            "id": 1,
            "method": "model/list",
            "params": {"limit": 20, "includeHidden": False, "modelProvider": "other"},
        },
    ):
        with pytest.raises(ValidationError):
            gate.send(frame)
    assert not server.sent


def test_permission_requests_are_declined_on_real_wire_shape(diagnostic):
    t, server = diagnostic
    server.frames.append(
        {
            "id": "permission",
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "do-not-run"},
        }
    )
    # Authority responses are handled by a separate mock dispatch branch.
    original = server.send

    def send(frame):
        if "method" not in frame:
            server.sent.append(frame)
        else:
            original(frame)

    server.send = send
    assert t.diagnostics.run("d" * 32).status == "succeeded"
    assert {"id": "permission", "result": {"decision": "decline"}} in server.sent
    assert any(
        e.kind == "codex.diagnostic_authority_denied" for e in t.school.core.events()
    )


def test_effective_profile_drift_closes_without_auth_or_catalog(diagnostic):
    t, server = diagnostic
    server.config = {**CONFIG, "sandbox_mode": "danger-full-access"}
    assert t.diagnostics.run("d" * 32).status == "unknown"
    state = t.diagnostics.state()
    assert state["last_error"] == "Conflict" and state["stop_confirmed"]
    assert all(f["method"] not in {"account/read", "model/list"} for f in server.sent)


@pytest.mark.parametrize(
    "fault", ["initialize", "config/read", "account/read", "model/list"]
)
def test_rpc_errors_preserve_unknown_and_never_reconnect(diagnostic, fault):
    t, server = diagnostic
    server.fault = fault
    assert t.diagnostics.run("d" * 32).status == "unknown"
    assert server.closed
    calls = len(server.sent)
    with pytest.raises(RecoveryRequired):
        t.diagnostics.run("e" * 32)
    assert len(server.sent) == calls
    assert "secret=fixture" not in json.dumps(t.diagnostics.state())


def test_missing_login_is_honest_no_model_request(diagnostic):
    t, server = diagnostic
    server.authenticated = False
    assert t.diagnostics.run("d" * 32).status == "succeeded"
    assert t.diagnostics.state()["auth"] == "login_required"
    assert not any(f["method"] == "model/list" for f in server.sent)


def test_child_environment_never_inherits_provider_or_ui_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "never-copy")
    monkeypatch.setenv("II_PRO_SESSION", "never-copy")
    monkeypatch.setenv("CODEX_HOME", "managed-home")
    env = child_environment()
    assert "OPENAI_API_KEY" not in env and "II_PRO_SESSION" not in env
    assert env["CODEX_HOME"] == "managed-home"


def test_no_discovery_or_launch_when_only_reading_ui(tmp_path, monkeypatch):
    def forbidden():
        raise AssertionError("Read-only UI must not launch Codex")

    monkeypatch.setattr("ii_pro_team.diagnostics.discover_cli", forbidden)
    t = training(tmp_path)
    try:
        assert t.diagnostics.state()["connection"] == "not_checked"
        assert not t.school.store.all(
            "SELECT 1 FROM runs WHERE executor='codex_diagnostics'"
        )
    finally:
        t.close()


def test_mcp_inventory_child_is_closed_before_disabled_child_and_auth(diagnostic):
    t, _ = diagnostic
    inventory = MockServer(
        config={
            **CONFIG,
            "mcp_servers": {
                "existing_mcp": {"enabled": True, "url": "secret=never-store"}
            },
        }
    )
    hardened = MockServer(
        config={**CONFIG, "mcp_servers": {"existing_mcp": {"enabled": False}}}
    )
    calls = []

    def factory(cli, cwd, disabled_mcp=()):
        calls.append(disabled_mcp)
        if not disabled_mcp:
            return inventory
        assert inventory.closed and disabled_mcp == ("existing_mcp",)
        return hardened

    t.diagnostics.executor.transport_factory = factory
    assert t.diagnostics.run("d" * 32).status == "succeeded"
    assert len(calls) == 2 and hardened.closed
    assert [r["method"] for r in inventory.sent] == [
        "initialize",
        "initialized",
        "config/read",
    ]
    state = t.diagnostics.state()
    assert state["disabled_mcp_count"] == 1 and state["restrictions"]["no_mcp_servers"]
    assert "existing_mcp" not in json.dumps(state) and "never-store" not in json.dumps(
        state
    )


def test_unexpected_agent_activity_fails_closed_before_auth(diagnostic):
    t, server = diagnostic
    server.frames.append(
        {"method": "item/started", "params": {"item": {"type": "commandExecution"}}}
    )
    assert t.diagnostics.run("d" * 32).status == "unknown"
    assert t.diagnostics.state()["unexpected_agent_activity"] is True
    assert server.closed


def test_manual_metadata_reconciliation_requires_real_stop_proof(diagnostic):
    t, server = diagnostic
    server.fault = "account/read"
    run = t.diagnostics.run("d" * 32)
    # A fixture with no OS process has no authoritative process exit code.
    with pytest.raises(RecoveryRequired):
        t.diagnostics.reconcile_closed(run.id)
    assert t.school.core.runs.get(run.id).status == "unknown"


def test_mcp_overrides_are_validated_bare_config_keys_not_shell_or_quoted_names():
    assert "mcp_servers.existing_mcp.enabled=false" in arguments(
        "codex", disabled_mcp=("existing_mcp",)
    )
    for name in ("x.y", "x; command", "../path", '"quoted"'):
        with pytest.raises(ValidationError):
            arguments("codex", disabled_mcp=(name,))


def test_late_owned_exit_releases_lease_but_needs_explicit_verification(diagnostic):
    import threading
    import time

    t, server = diagnostic
    exited = threading.Event()

    class Process:
        def poll(self):
            return 0 if exited.is_set() else None

        def wait(self):
            assert exited.wait(5)
            return 0

    server.process = Process()
    server.close = lambda: False
    run = t.diagnostics.run("d" * 32)
    try:
        assert run.status == "unknown" and run.id in t.school.core.active_run_ids
        assert t.diagnostics.state()["connection"] == "unconfirmed"
        with pytest.raises(RecoveryRequired):
            t.diagnostics.reconcile_closed(run.id)
        exited.set()
        for _ in range(100):
            if not t.school.core.active_run_ids:
                break
            time.sleep(0.01)
        assert t.school.core.runs.get(run.id).status == "unknown"
        assert (
            t.diagnostics.reconcile_closed(run.id).effect_status == "proven_not_applied"
        )
        assert t.school.core.runs.get(run.id).status == "failed"
        restored = t.diagnostics.state()
        assert restored["stop_confirmed"] and restored["stop_reconciled"]
        assert restored["connection"] == "closed"
    finally:
        exited.set()


def test_manual_reconciliation_preserves_failed_history_and_does_not_reconnect(
    diagnostic,
):
    t, server = diagnostic

    class Process:
        def poll(self):
            return 1

    server.process = Process()
    server.fault = "account/read"
    run = t.diagnostics.run("d" * 32)
    calls = len(server.sent)
    assert t.diagnostics.reconcile_closed(run.id).status == "failed"
    assert len(server.sent) == calls
    assert t.school.core.runs.get(run.id).reconciliation_json


def test_metadata_verifier_cannot_resolve_school_or_worker_runs(diagnostic):
    from test_school_v03_ui import preparation

    t, _ = diagnostic
    lesson = t.prepare(preparation())
    with pytest.raises(RecoveryRequired):
        t.diagnostics.reconcile_closed(lesson["run_id"])
    assert t.school.core.runs.get(lesson["run_id"]).status == "created"


def test_api_is_read_only_and_session_protected_for_diagnostics(diagnostic):
    import threading
    from ii_pro_ui.server import LocalServer
    from test_school_v03_ui import request

    t, server = diagnostic
    t.diagnostics.run("d" * 32)

    class Observer:
        def state(self):
            return {"read_only": True}

    api = LocalServer(0, Observer(), token="capability")
    api.training = t
    thread = threading.Thread(target=api.serve_forever)
    thread.start()
    try:
        calls = len(server.sent)
        for _ in range(3):
            code, state = request(api, "/api/team/codex")
            assert code == 200 and state["auth"] == "confirmed" and state["turns"] == 0
        assert (
            request(api, "/api/team/codex", headers={"Authorization": None})[0] == 401
        )
        for path in (
            "/api/team/codex",
            "/api/team/codex/connect",
            "/api/team/codex/start",
            "/api/team/codex/reconcile",
        ):
            assert request(api, path, {"model": "forbidden"})[0] == 405
        assert len(server.sent) == calls
    finally:
        api.shutdown()
        thread.join()
        api.training = None
        api.server_close()


def test_missing_cli_is_not_started_and_status_has_no_fake_auth(
    diagnostic, monkeypatch
):
    import shutil

    t, server = diagnostic
    original = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda key: None if key == "codex" else original(key)
    )
    t.diagnostics.executor.cli = None
    run = t.diagnostics.run("d" * 32)
    assert run.status == "failed" and run.effect_status == "not_started"
    state = t.diagnostics.state()
    assert state["cli_installed"] is False and state["auth"] == "not_checked"
    assert state["models"] == [] and not server.sent
