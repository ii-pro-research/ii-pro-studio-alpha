"""Official SIWC transport in a trusted, one-shot Node child. No SSE here."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import shutil
import subprocess
import threading
import time

from ii_pro_core.errors import Conflict
from ii_pro_core.security import public_text
from ii_pro_school.adapters import _strict_json
from . import engineering, project_contract
from .adapters import AgentAdapter
from .protocol import event, validate_request
from .text import DEADLINE, MAX_OUTPUT, MODEL, TextUncertain
from .text_diagnostics import MESSAGES, TransportDiagnostic, api_code

BRIDGE = Path(__file__).with_name("devkit_bridge")
REVISION = "0a36fefeb913055c8c7a1a29b63d82b96b2e841a"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class NodeBridge:
    """Fixed executable/entry point, anonymous pipes, bounded public packets."""

    mock = False

    def __init__(self):
        selected = shutil.which("node")
        self.node = Path(selected).resolve() if selected else None
        self.entry = BRIDGE / "bridge.mjs"
        self.available = False
        self.node_version = None
        self.node_sha256 = sha(self.node) if self.node else None
        try:
            version = (
                subprocess.run(
                    [str(self.node), "--version"],
                    capture_output=True,
                    timeout=5,
                    env=self.environment(),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=True,
                )
                .stdout.decode("ascii")
                .strip()
            )
            parts = tuple(map(int, version.removeprefix("v").split(".")))
            if parts < (22, 12, 0):
                raise Conflict("Node 22.12 or newer required")
            self.node_version = version
            self.verify()
            self.available = True
        except Exception:
            self.available = False  # Never fall back to legacy ResponsesHTTPS.

    @staticmethod
    def environment():
        return {
            k: os.environ[k]
            for k in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")
            if k in os.environ
        }

    def configuration(self):
        return {
            "component": "OpenAI SIWC packages/local/src/responses.ts",
            "upstream_revision": REVISION,
            "openai": "7.21.0",
            "node_version": self.node_version,
            "node_sha256": self.node_sha256,
            "manifest_sha256": sha(BRIDGE / "runtime-manifest.json")
            if (BRIDGE / "runtime-manifest.json").is_file()
            else None,
            "entry_sha256": sha(self.entry) if self.entry.is_file() else None,
        }

    def verify(self):
        value = _strict_json((BRIDGE / "runtime-manifest.json").read_bytes())
        if (
            value["revision"] != REVISION
            or not self.node
            or sha(self.node) != self.node_sha256
        ):
            raise Conflict("DevKit runtime identity changed")
        for name, expected in value["files"].items():
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in name:
                raise Conflict("Invalid runtime inventory path")
            path = BRIDGE / name
            path.resolve().relative_to(BRIDGE.resolve())
            if path.is_symlink() or sha(path) != expected:
                raise Conflict("DevKit runtime inventory mismatch")

    def stream(
        self,
        prompt,
        token,
        request_id,
        *,
        role="diagnostic",
        cancellation=None,
        seconds=DEADLINE,
    ):
        self.verify()
        # Node owns the inference deadline. Leave bounded room for process
        # startup, IPC and its terminal packet before Python cancels the bridge.
        until = time.monotonic() + seconds + 5
        process = subprocess.Popen(
            [str(self.node), str(self.entry)],
            cwd=BRIDGE,
            env=self.environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            shell=False,
        )
        packets = queue.Queue()

        def read():
            used = 0
            try:
                while True:
                    raw = process.stdout.readline(65537)
                    if not raw:
                        break
                    used += len(raw)
                    if len(raw) > 65536 or used > 131072:
                        raise ValueError("Bridge output quota")
                    packets.put((True, _strict_json(raw)))
            except Exception:
                packets.put((False, None))
            finally:
                packets.put((True, None))

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            envelope = {
                "version": 1,
                "operation": "text.generate",
                "model": MODEL,
                "request_id": request_id,
                "prompt": prompt,
                "role": role,
                "access_token": token,
                "deadline_ms": int(seconds * 1000),
            }
            raw = json.dumps(envelope, ensure_ascii=False).encode("utf-8") + b"\n"
            if len(raw) > 32768:
                raise Conflict("Bridge input quota")
            process.stdin.write(raw)
            process.stdin.flush()
            terminal, started, count = False, False, 0
            while True:
                if cancellation and cancellation.requested:
                    raise TextUncertain("cancelled")
                if time.monotonic() >= until:
                    raise TextUncertain("deadline")
                try:
                    valid, packet = packets.get(timeout=0.05)
                except queue.Empty:
                    continue
                if not valid:
                    raise TextUncertain("devkit_failed")
                if packet is None:
                    break
                count += 1
                if count > 128:
                    raise TextUncertain("bridge_packet_quota")
                if terminal or packet.get("version") != 1:
                    raise TextUncertain("invalid_event")
                kind = packet.get("type")
                allowed = {
                    "started": {"version", "type"},
                    "delta": {"version", "type", "text"},
                    "completed": {"version", "type", "text"},
                    "error": {"version", "type", "code", "http_status", "request_id"},
                }
                if (
                    kind not in allowed
                    or set(packet) != allowed[kind]
                    or (not started and kind not in {"started", "error"})
                ):
                    raise TextUncertain("invalid_event")
                if kind == "started":
                    if started:
                        raise TextUncertain("invalid_event")
                    started = True
                if kind in {"delta", "completed"} and not isinstance(
                    packet["text"], str
                ):
                    raise TextUncertain("invalid_event")
                if kind == "error" and (
                    not isinstance(packet["code"], str)
                    or len(packet["code"]) > 100
                    or (
                        packet["http_status"] is not None
                        and (
                            type(packet["http_status"]) is not int
                            or not 100 <= packet["http_status"] <= 599
                        )
                    )
                    or (
                        packet["request_id"] is not None
                        and (
                            not isinstance(packet["request_id"], str)
                            or len(packet["request_id"]) > 200
                        )
                    )
                ):
                    raise TextUncertain("invalid_event")
                terminal = kind in {"completed", "error"}
                yield packet
            if (
                process.wait(timeout=max(0.1, until - time.monotonic())) != 0
                or not terminal
            ):
                raise TextUncertain("devkit_failed")
        finally:
            if process.poll() is None:
                try:
                    process.stdin.write(b'{"version":1,"operation":"cancel"}\n')
                    process.stdin.flush()
                    process.wait(timeout=2)
                except Exception:
                    # Only this owned bridge child; cannot prove remote stop.
                    process.terminate()
                    process.wait(timeout=2)
            try:
                process.stdin.close()
            except OSError:
                pass
            reader.join(2)
            process.stdout.close()


class DevkitTextAdapter(AgentAdapter):
    revision = "siwc-official-devkit-v2"
    engineering_supported = True
    supports_cancellation = True

    def __init__(self, grant=None, *, bridge=None, mock=False, auth_error=False):
        self.grant = grant
        self.bridge = bridge or NodeBridge()
        if mock != self.bridge.mock:
            raise Conflict("Explicit Mock bridge identity required")
        self.model, self.mock, self.auth_error = MODEL, mock, auth_error
        self.current_reference = None

    def configuration(self):
        return {
            "backend": "siwc_text_mock" if self.mock else "siwc_devkit",
            "model": self.model,
            "effort": "sdk_default",
            "effort_description": "Стандарт SDK; reasoning effort не задаётся",
            "revision": self.revision,
            "billing": "chatgpt_plan",
            "authorization_ref": self.grant.reference if self.grant else None,
            "endpoint": "https://api.openai.com/v1/responses",
            "store": False,
            "stream": True,
            "tools": [],
            "file_inputs": False,
            "computer_access": False,
            "automatic_retries": 0,
            "redirects": "error",
            "deadline_seconds": DEADLINE,
            "extended_engineer_deadline_seconds": project_contract.EXTENDED_ENGINEER_DEADLINE,
            "sort_engineer_deadline_seconds": project_contract.SORT_ENGINEER_DEADLINE,
            "sort_engineer_text_idle_seconds": project_contract.SORT_ENGINEER_IDLE,
            "bridge_watchdog_grace_seconds": 5,
            "max_public_characters": MAX_OUTPUT,
            "credential_transport": "private_child_stdin_only",
            "usage": "unavailable",
            "request_id": "unavailable",
            "implementation_sha256": sha(Path(__file__)),
            "dependencies": {
                name: sha(Path(__file__).with_name(name))
                for name in (
                    "text.py",
                    "protocol.py",
                    "siwc.py",
                    "text_diagnostics.py",
                    "engineering.py",
                    "project_contract.py",
                )
            },
            "bridge": self.bridge.configuration(),
        }

    def ready(self):
        try:
            return bool(
                self.bridge.available
                and self.grant
                and self.grant.ready(self.model)
                and (
                    self.current_reference is None
                    or self.current_reference() == self.grant.reference
                )
            )
        except Exception:
            return False

    def validate_runtime(self):
        self.bridge.verify()

    def ready_for_deadline(self, seconds):
        return self.ready() and self.grant.expires_at > time.time() + seconds + 5

    def run(self, request, emit, cancellation=None):
        facts = TransportDiagnostic()
        # The official transport returns text, not these wire observations.
        facts.data.update(
            content_type_present=None,
            received_bytes=None,
            sse_frames=None,
            sse_events=None,
            response_created_received=None,
            response_completed_received=False,
        )
        started = time.monotonic()
        secrets = tuple(
            getattr(self.grant, key, "")
            for key in ("access_token", "refresh_token", "id_token")
        )
        stream = None
        try:
            value = validate_request(request)
            seconds = value["inputs"].get("deadline_seconds", DEADLINE)
            if seconds == project_contract.SORT_ENGINEER_DEADLINE:
                facts.data.update(
                    deadline_seconds=seconds,
                    text_idle_seconds=project_contract.SORT_ENGINEER_IDLE,
                    bridge_text_packets=0,
                    first_bridge_text_seconds=None,
                    last_bridge_text_seconds=None,
                )
            if (
                value["version"] not in {2, 3, 4}
                or value["inputs"]["effort"] != "sdk_default"
                or not self.ready()
                or ("deadline_seconds" in value["inputs"] and not self.ready_for_deadline(seconds))
            ):
                raise Conflict("Fresh DevKit authorization and profile required")
            emit(
                event(
                    "session",
                    {
                        "session_id": value["session_id"],
                        "role": value["role"],
                        "backend": self.configuration()["backend"],
                    },
                )
            )
            # Once the child may receive its envelope, lack of bridge telemetry
            # cannot prove that an HTTP request was not sent.
            facts.data.update(
                stage="devkit_bridge", request_transmission="possibly_sent"
            )
            prompt, role = engineering.wire(value)
            instructions = project_contract.INSTRUCTIONS if value["version"] == 4 else engineering.INSTRUCTIONS
            stream = self.bridge.stream(
                prompt,
                self.grant.access_token,
                value["session_id"],
                role=role,
                cancellation=cancellation,
                **({"seconds": seconds} if seconds != DEADLINE else {}),
            )
            text, answer, published = "", None, 0
            for packet in stream:
                kind = packet["type"]
                if kind == "started":
                    facts.data.update(
                        stage="devkit_stream", request_transmission="possibly_sent"
                    )
                elif kind in {"delta", "completed"}:
                    part = public_text(packet["text"], limit=MAX_OUTPUT)
                    if kind == "delta":
                        text += part
                        if part and "bridge_text_packets" in facts.data:
                            since_start = round(time.monotonic() - started, 6)
                            if facts.data["bridge_text_packets"] == 0:
                                facts.data["first_bridge_text_seconds"] = since_start
                            facts.data["last_bridge_text_seconds"] = since_start
                            facts.data["bridge_text_packets"] += 1
                    else:
                        if not part or part != text:
                            raise TextUncertain("completion_invalid")
                        answer = part
                    if len(text) > MAX_OUTPUT or any(
                        secret and secret in text for secret in secrets
                    ):
                        raise TextUncertain("devkit_failed")
                    safe = len(text)
                    for secret in secrets:
                        for n in range(min(len(secret) - 1, len(text)), 0, -1):
                            if text.endswith(secret[:n]):
                                safe = min(safe, len(text) - n)
                                break
                    if kind == "delta":
                        while safe - published >= 400:
                            emit(
                                event(
                                    "message",
                                    {
                                        "delta": text[published : published + 400],
                                        "provisional": True,
                                        "role": value["role"],
                                    },
                                )
                            )
                            published += 400
                elif kind == "error":
                    facts.data.update(
                        api_error_code=api_code(packet["code"]),
                        http_status=packet["http_status"],
                        request_id=packet["request_id"],
                    )
                    facts.data["sdk_error_code"] = (
                        packet["code"]
                        if packet["code"]
                        in {
                            "deadline",
                            "text_idle",
                            "invalid_stream",
                            "stream_interrupted",
                            "cancelled",
                            "response_incomplete",
                            "output_quota",
                            "network_error",
                            "bridge_error",
                        }
                        else api_code(packet["code"])
                    )
                    reason = packet["code"] if packet["code"] in {"deadline", "text_idle", "cancelled"} else "devkit_failed"
                    raise TextUncertain(reason)
            if answer is None:
                raise TextUncertain("missing_completed")
            facts.data.update(
                stage="completed",
                outcome="completed",
                termination_reason="completed",
                safe_message=MESSAGES["completed"],
                response_completed_received=True,
            )
            diagnostic = facts.snapshot(secrets)
            emit(event("status", {"type": "transport.diagnostic", **diagnostic}))
            emit(
                event(
                    "message",
                    {
                        "text": answer[:1000],
                        "preview_only": len(answer) > 1000,
                        "role": value["role"],
                    },
                )
            )
            return {
                "version": 1,
                "backend": self.configuration()["backend"],
                "real_inference": not self.mock,
                "session_id": value["session_id"],
                "model": self.model,
                "model_identity": "requested_model",
                "text": answer,
                "usage": None,
                "request_id": None,
                "response_id": None,
                "completion_evidence": "official_streamResponse_resolved_after_response.completed",
                "request_body": {
                    "model": self.model,
                    "input": [{"role": "user", "content": prompt}],
                    "instructions": instructions[role],
                    "store": False,
                    "stream": True,
                },
                "elapsed_seconds": time.monotonic() - started,
                **({"deadline_seconds": seconds} if seconds != DEADLINE else {}),
                "transport_diagnostic": diagnostic,
                "verified_knowledge": False,
                "computer_access": False,
            }
        except Exception as exc:
            reason = exc.reason if isinstance(exc, TextUncertain) else "devkit_failed"
            facts.fail(reason, exc)
            if facts.data.get("sdk_error_code"):
                facts.data["safe_message"] += (
                    " Код SDK: " + facts.data["sdk_error_code"]
                )
            diagnostic = facts.snapshot(secrets)
            emit(event("status", {"type": "transport.diagnostic", **diagnostic}))
            raise TextUncertain(reason, diagnostic=diagnostic) from None
        finally:
            if stream is not None:
                stream.close()
