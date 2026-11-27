"""Metadata queries must not turn catalog visibility into execution authority."""

import json

import pytest

from ii_pro_core.errors import ValidationError
from ii_pro_team.app_server import AppServerClient, TransportLost
from ii_pro_team.diagnostics import DiagnosticTransport, permission_observation
from test_codex_diagnostics import MockServer, diagnostic as _diagnostic

diagnostic = _diagnostic


class PermissionServer(MockServer):
    def __init__(self):
        super().__init__()
        self.profiles = [
            {"id": ":read-only", "allowed": True},
            {
                "id": "private-project-profile",
                "allowed": False,
                "description": "C:/private secret=fixture",
            },
        ]

    def send(self, frame):
        method = frame.get("method")
        results = {
            "permissionProfile/list": {"data": self.profiles, "nextCursor": None},
            "configRequirements/read": {"requirements": {"private": "secret=fixture"}},
            "windowsSandbox/readiness": {"status": "ready"},
        }
        if method in results:
            self.sent.append(frame)
            self.frames.append({"id": frame["id"], "result": results[method]})
        else:
            super().send(frame)


def test_opt_in_probe_uses_same_core_and_sealed_artifact(diagnostic):
    t, _ = diagnostic
    server = PermissionServer()
    executor = t.diagnostics.executor
    executor.probe_permissions = True
    executor.transport_factory = lambda cli, cwd: server
    run = t.diagnostics.run("c" * 32)
    assert run.status == "succeeded" and run.execution_stopped == 1
    state = t.diagnostics.state()
    probe = state["permission_probe"]
    assert probe["windows_sandbox"] == "ready"
    assert probe["profiles"][0]["allowed"] and probe["selected_profile"] is None
    assert not probe["text_ready"]
    assert all(p["enforcement_verified"] is False for p in probe["profiles"])
    assert server.sent[0]["params"]["capabilities"]["experimentalApi"] is True
    text = json.dumps(state)
    assert "private-project-profile" not in text and "secret=fixture" not in text
    assert state["agent_sessions"] == state["turns"] == 0
    assert not any(
        f.get("method", "").startswith(("thread/", "turn/", "process/"))
        for f in server.sent
    )
    t.school.core.artifacts.validate_run(run.id, require_any=True)
    calls = len(server.sent)
    assert t.diagnostics.run("c" * 32).id == run.id
    assert len(server.sent) == calls


@pytest.mark.parametrize(
    "method",
    ["permissionProfile/list", "configRequirements/read", "windowsSandbox/readiness"],
)
def test_old_metadata_gate_does_not_gain_new_capabilities(method):
    server = PermissionServer()
    with pytest.raises(ValidationError):
        DiagnosticTransport(server, "fixed").send(
            {"id": 1, "method": method, "params": {}}
        )
    assert server.sent == []


@pytest.mark.parametrize(
    "method,params",
    [
        ("permissionProfile/list", {"cwd": "outside", "limit": 20}),
        ("permissionProfile/list", {"cwd": "fixed", "limit": 100}),
        ("permissionProfile/list", {"cwd": "fixed", "limit": 20, "cursor": ""}),
        ("windowsSandbox/readiness", {"setup": True}),
        ("windowsSandbox/setupStart", {"mode": "elevated"}),
        (
            "config/value/write",
            {"keyPath": "default_permissions", "value": ":danger-full-access"},
        ),
        ("thread/start", {"permissions": ":read-only"}),
        ("turn/start", {"threadId": "never"}),
        (
            "initialize",
            {
                "clientInfo": {"name": "ii_pro_studio", "version": "0.1.0"},
                "capabilities": {"experimentalApi": False},
            },
        ),
    ],
)
def test_permission_gate_rejects_scope_drift_setup_and_turns(method, params):
    server = PermissionServer()
    with pytest.raises(ValidationError):
        DiagnosticTransport(server, "fixed", probe_permissions=True).send(
            {"id": 1, "method": method, "params": params}
        )
    assert server.sent == []


@pytest.mark.parametrize(
    "entry",
    [
        {"id": ":read-only", "allowed": 1},
        {"allowed": True},
        {"id": "x" * 121, "allowed": True},
    ],
)
def test_malformed_catalog_is_not_a_verified_profile(entry):
    server = PermissionServer()
    server.profiles = [entry]
    client = AppServerClient(
        DiagnosticTransport(server, "fixed", probe_permissions=True)
    )
    with pytest.raises(TransportLost):
        permission_observation(client, "fixed")


def test_probe_errors_remain_unknown_without_automatic_reconnect(diagnostic):
    t, _ = diagnostic
    server = PermissionServer()
    original = server.send

    def send(frame):
        if frame.get("method") == "permissionProfile/list":
            server.sent.append(frame)
            server.frames.append(
                {
                    "id": frame["id"],
                    "error": {"code": -32601, "message": "unsupported secret=fixture"},
                }
            )
        else:
            original(frame)

    server.send = send
    executor = t.diagnostics.executor
    executor.probe_permissions = True
    executor.transport_factory = lambda cli, cwd: server
    run = t.diagnostics.run("c" * 32)
    assert run.status == "unknown" and server.closed
    state = t.diagnostics.state()
    assert state["auth"] == "confirmed" and state["last_error"] == "TransportLost"
    assert "secret=fixture" not in json.dumps(state)
    assert (
        len([f for f in server.sent if f.get("method") == "permissionProfile/list"])
        == 1
    )
    assert state["agent_sessions"] == state["turns"] == 0
