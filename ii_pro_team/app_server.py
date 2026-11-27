"""Bounded stdio JSON-RPC client. Not a Windows security boundary.

No process is started at import, in the UI, or by the mock composition. A real
launcher requires separate approval and a reviewed execution environment.
"""

import json
import queue
import subprocess
import threading
import time

from ii_pro_core.errors import Conflict, ValidationError
from ii_pro_school.adapters import _strict_json
from .protocol import ROLE_INSTRUCTIONS, event
from .adapters import AgentAdapter

MAX_LINE = 65536
METHODS = {
    "initialize",
    "account/read",
    "model/list",
    "config/read",
    "permissionProfile/list",
    "configRequirements/read",
    "windowsSandbox/readiness",
    "thread/start",
    "thread/resume",
    "thread/read",
    "turn/start",
    "turn/interrupt",
}


class TransportLost(RuntimeError):
    pass


class StdioTransport:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.queue = queue.Queue(maxsize=64)
        self.closed = threading.Event()
        self.reader_thread = threading.Thread(target=self._read, daemon=True)
        self.reader_thread.start()

    @classmethod
    def launch(cls, executable, cwd, *, approved=False, isolation_verified=False):
        if not approved or not isolation_verified:
            raise Conflict(
                "A separately approved and verified agent environment is required"
            )
        # Trusted composition supplies the executable/cwd, never HTTP/model input.
        process = subprocess.Popen(
            [str(executable), "app-server", "--stdio"],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        transport = cls(process.stdout, process.stdin)
        transport.process = process
        return transport

    def _read(self):
        try:
            while not self.closed.is_set():
                line = self.reader.readline(MAX_LINE + 1)
                if not line or len(line) > MAX_LINE or not line.endswith(b"\n"):
                    break
                self.queue.put_nowait(_strict_json(line))
        except (ValueError, UnicodeError, queue.Full, OSError):
            pass
        finally:
            self.closed.set()

    def send(self, value):
        data = (json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n").encode()
        if len(data) > MAX_LINE or self.closed.is_set():
            raise TransportLost(
                "App-server transport unavailable or request exceeds quota"
            )
        try:
            self.writer.write(data)
            self.writer.flush()
        except (OSError, ValueError) as exc:
            raise TransportLost("App-server write was not confirmed") from exc

    def receive(self, timeout):
        until = time.monotonic() + timeout
        while True:
            try:
                return self.queue.get(
                    timeout=min(0.05, max(0.001, until - time.monotonic()))
                )
            except queue.Empty:
                if self.closed.is_set():
                    raise TransportLost("App-server disconnected") from None
                if time.monotonic() >= until:
                    raise TimeoutError("App-server receive deadline") from None

    def close(self):
        self.closed.set()
        try:
            self.writer.close()  # EOF requests shutdown; no terminate/kill fallback.
        except (OSError, ValueError):
            pass
        process = getattr(self, "process", None)
        if process is not None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                return False  # An open pipe is not proof of process termination.
        self.reader_thread.join(timeout=0.2)
        if not self.reader_thread.is_alive():
            self.reader.close()
        return True


class AppServerClient:
    def __init__(self, transport, *, deadline=30):
        self.transport = transport
        self.deadline = deadline
        self.sequence = 0
        self.emit = lambda item: None
        self.notifications = []
        self.lock = threading.RLock()

    def _route(self, value):
        if not isinstance(value, dict):
            raise TransportLost("Invalid app-server frame")
        method, params = value.get("method"), value.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            raise TransportLost("Unexpected app-server envelope")
        if "id" in value:
            # There is deliberately no automatic acceptance or grant session.
            self.emit(
                event(
                    "approval_request",
                    {
                        "method": method,
                        "decision": "blocked",
                        "request_id": str(value["id"]),
                    },
                )
            )
            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            }:
                self.transport.send(
                    {"id": value["id"], "result": {"decision": "decline"}}
                )
            else:
                self.transport.send(
                    {
                        "id": value["id"],
                        "error": {
                            "code": -32601,
                            "message": "Unsupported authority request",
                        },
                    }
                )
            return
        if len(self.notifications) >= 256:
            raise TransportLost("Agent notification quota exceeded")
        self.notifications.append((method, params))

    def call(self, method, params):
        if method not in METHODS:
            raise ValidationError("App-server method is not allowlisted")
        with self.lock:
            self.sequence += 1
            sequence = self.sequence
            self.transport.send({"id": sequence, "method": method, "params": params})
            end = time.monotonic() + self.deadline
            while time.monotonic() < end:
                try:
                    value = self.transport.receive(max(0.001, end - time.monotonic()))
                except TimeoutError:
                    raise TransportLost("App-server request outcome unknown") from None
                if not isinstance(value, dict):
                    raise TransportLost("Invalid response")
                if "method" not in value and value.get("id") == sequence:
                    if "error" in value:
                        # Provider diagnostics may contain secrets; retain category only.
                        raise TransportLost(
                            "App-server returned an error (including limits/auth)"
                        )
                    if "result" not in value:
                        raise TransportLost("Missing app-server result")
                    result = value["result"]
                    if not isinstance(result, dict):
                        raise TransportLost("Invalid app-server result type")
                    if method in {
                        "thread/start",
                        "thread/resume",
                        "thread/read",
                        "turn/start",
                    }:
                        field = "turn" if method == "turn/start" else "thread"
                        if (
                            not isinstance(result.get(field), dict)
                            or not isinstance(result[field].get("id"), str)
                            or not result[field]["id"]
                        ):
                            raise TransportLost("Missing session/turn identity")
                        if (
                            method in {"thread/resume", "thread/read"}
                            and result[field]["id"] != params["threadId"]
                        ):
                            raise TransportLost("Recovered thread identity mismatch")
                    return result
                self._route(value)
            raise TransportLost("App-server request outcome unknown")

    def initialize(self, *, experimental=False):
        if type(experimental) is not bool:
            raise ValidationError("Explicit experimental capability required")
        result = self.call(
            "initialize",
            {
                "clientInfo": {"name": "ii_pro_studio", "version": "0.1.0"},
                "capabilities": {"experimentalApi": experimental},
            },
        )
        self.transport.send({"method": "initialized", "params": {}})
        return result

    def auth_status(self):
        result = self.call("account/read", {"refreshToken": False})
        account = result.get("account") or {}
        return {
            "authenticated": bool(account),
            "mode": account.get("type", "unknown"),
            "requires_auth": result.get("requiresOpenaiAuth"),
            "connected": True,
        }

    def recover(self, thread_id):
        # Read-only observation. Never re-send a turn whose outcome is unknown.
        return self.call("thread/read", {"threadId": thread_id, "includeTurns": True})


class CodexAgentAdapter(AgentAdapter):
    mode = "codex_app_server"
    revision = "codex-app-server-jsonrpc-v1"

    def __init__(self, client, cwd, model, *, isolation_verified=False):
        self.client, self.cwd, self.model = client, str(cwd), model
        self.isolation_verified = isolation_verified

    def configuration(self):
        return {
            "backend": self.mode,
            "model": self.model,
            "sandbox": "read-only",
            "approval_policy": "on-request",
            "tools": "all authority requests blocked",
            "isolation_verified": self.isolation_verified,
        }

    def run(self, request, emit, cancellation=None):
        if not self.isolation_verified:
            raise Conflict("Native Codex has not been verified as an isolated executor")
        client = self.client
        with client.lock:
            client.emit = emit
            previous = request["inputs"].get("provider_thread_id")
            params = {
                "cwd": self.cwd,
                "model": self.model,
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "sandbox": "read-only",
                "developerInstructions": ROLE_INSTRUCTIONS[request["role"]],
            }
            if previous:
                params["threadId"] = previous
            reply = client.call("thread/resume" if previous else "thread/start", params)
            thread_id = reply["thread"]["id"]
            emit(
                event(
                    "session",
                    {
                        "session_id": request["session_id"],
                        "provider_thread_id": thread_id,
                        "role": request["role"],
                    },
                )
            )
            turn = client.call(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": request["text"]
                            + "\n"
                            + json.dumps(request["inputs"], ensure_ascii=False),
                        }
                    ],
                },
            )
            turn_id = turn["turn"]["id"]
            end, count, text, interrupted = (
                time.monotonic() + client.deadline,
                0,
                [],
                False,
            )
            while time.monotonic() < end:
                if cancellation and cancellation.requested and not interrupted:
                    client.call(
                        "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
                    )
                    interrupted = True
                if not client.notifications:
                    try:
                        client._route(
                            client.transport.receive(
                                min(0.1, max(0.001, end - time.monotonic()))
                            )
                        )
                    except TimeoutError:
                        continue
                if not client.notifications:
                    continue
                method, data = client.notifications.pop(0)
                if (
                    data.get("threadId") != thread_id
                    or data.get("turnId", turn_id) != turn_id
                ):
                    continue
                count += 1
                if count > 256:
                    raise TransportLost("Turn event quota exceeded")
                # No reasoning items/deltas or hidden chain of thought are stored.
                if method == "item/agentMessage/delta":
                    delta = data.get("delta", "")
                    if (
                        not isinstance(delta, str)
                        or sum(len(t) for t in text) + len(delta) > 16000
                    ):
                        raise TransportLost("Agent text exceeds quota")
                    text.append(delta)
                    emit(
                        event(
                            "message", {"text": delta, "provider_thread_id": thread_id}
                        )
                    )
                elif method in {"item/started", "item/completed"}:
                    item = data.get("item", {})
                    if (
                        method == "item/completed"
                        and item.get("type") == "agentMessage"
                        and not text
                    ):
                        final = item.get("text", "")
                        if not isinstance(final, str) or len(final) > 16000:
                            raise TransportLost("Agent message exceeds quota")
                        text.append(final)
                        emit(
                            event(
                                "message",
                                {"text": final, "provider_thread_id": thread_id},
                            )
                        )
                    if item.get("type") in {
                        "commandExecution",
                        "fileChange",
                        "mcpToolCall",
                    }:
                        emit(
                            event(
                                "tool",
                                {
                                    "type": item["type"],
                                    "id": item.get("id"),
                                    "status": item.get("status", "observed"),
                                },
                            )
                        )
                elif method == "turn/completed":
                    if (
                        data.get("turn", {}).get("id") != turn_id
                        or data["turn"].get("status") != "completed"
                    ):
                        raise TransportLost(
                            "Interrupted or failed turn; effects require verification"
                        )
                    return {
                        "text": "".join(text),
                        "provider_thread_id": thread_id,
                        "turn_id": turn_id,
                        "backend": self.mode,
                        "real_inference": True,
                    }
            raise TransportLost("Turn deadline exceeded; do not replay")
