"""SIWC Responses Text. Model output is inert data; there is no tool broker.

The HTTPS client is trusted Python, not an OS sandbox. No model-selected path,
URL, command, import, callback or tool is dispatched by this adapter.
"""

import codecs
import hashlib
from http.client import HTTPSConnection
import json
from pathlib import Path
from threading import Event, Lock
import time

from ii_pro_core.errors import Conflict
from ii_pro_core.executors import CancellationToken, Executor, ExecutionResult, ProducedArtifact
from ii_pro_core.security import canonical, digest, public_text
from ii_pro_school.adapters import _strict_json
from . import engineering
from .adapters import AgentAdapter
from .protocol import event, validate_request
from .siwc import Vault
from .text_diagnostics import (
    API_ERROR_CODES,
    EVENT_TYPES,
    MESSAGES,
    TransportDiagnostic,
    api_code,
    exception_type,
)

FIRST_PROMPT = "Ты — ведущий архитектор II PRO Studio. В нашей команде будут работать два мощных GPT и локальная Qwen2.5-Coder-7B.\n\nРазработай краткий практический план создания Python-приложения для управления личными задачами.\n\nОпредели структуру проекта, основные функции, критерии готовности и тесты. Распредели работу между GPT Engineer и локальной Qwen7B. Объясни, чему именно Qwen должна научиться при выполнении своего задания.\n\nВерни конкретный структурированный план без выполнения инструментов"
MODEL = "gpt-6.2-sol"
DIAGNOSTIC_PROMPT = "Ответь одной строкой: II PRO Studio — GPT Architect готов к работе"
REJECTION_CODES = API_ERROR_CODES
DEADLINE = 60
MAX_OUTPUT = 16000


def request_body(prompt, model=MODEL):
    public_text(prompt, limit=1800, empty=False)
    return {
        "model": model,
        "reasoning": {"effort": "low"},
        "input": [
            {
                "role": "developer",
                "content": (
                    "You are GPT Architect. Return only the single line requested by the user. No tools, file access or hidden reasoning."
                    if prompt == DIAGNOSTIC_PROMPT
                    else "You are GPT Architect. Return a concise public text plan. No tools, file access or hidden reasoning."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "store": False,
        "stream": True,
        "tools": [],
        "tool_choice": "none",
    }


class TextUncertain(RuntimeError):
    """Never implies the provider stopped generating or did not charge usage."""

    def __init__(self, reason, *, diagnostic=None):
        self.reason = reason if reason in MESSAGES else "adapter_failed"
        self.diagnostic = diagnostic
        super().__init__(MESSAGES[self.reason])


class ResponsesHTTPS:
    def stream(self, payload, token, deadline, *, diagnostic=None):
        facts = diagnostic or TransportDiagnostic()
        connection = None
        try:
            if time.monotonic() >= deadline:
                raise TextUncertain("deadline")
            body = canonical(payload).encode()
            facts.data["stage"] = "connect"
            connection = HTTPSConnection(
                "api.openai.com", timeout=max(0.1, deadline - time.monotonic())
            )
            connection.connect()  # A failure here proves no HTTP request was sent.
            facts.data.update(
                stage="send_request", request_transmission="possibly_sent"
            )
            connection.request(
                "POST",
                "/v1/responses",
                body,
                {
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
            )
            facts.data.update(stage="read_headers", request_transmission="sent")
            response = connection.getresponse()
            facts.headers(response)
            if response.status != 200:
                facts.data["stage"] = "http_error_body"
                raw = response.read(8193)
                facts.data["received_bytes"] += len(raw)
                code, shape = None, "other"
                try:
                    body = _strict_json(raw)
                    shape = (
                        "error"
                        if isinstance(body.get("error"), dict)
                        else "detail"
                        if "detail" in body
                        else "other"
                    )
                    error = body.get("error")
                    code = (
                        api_code(error.get("code")) if isinstance(error, dict) else None
                    )
                except Exception as exc:
                    facts.data["primary_exception_type"] = exception_type(exc)
                facts.data.update(api_error_code=code, error_body_shape=shape)
                yield {
                    "type": "transport.rejected",
                    "http_status": response.status,
                    "shape": shape,
                    "code": code,
                }
                facts.data["stage"] = "http_rejection"
                raise TextUncertain("http_rejected")
            # The official SIWC DevKit also accepts a missing/empty MIME type.
            # Success still requires validated Responses events and completion.
            if facts.data["content_type"] not in {None, "", "text/event-stream"}:
                raise TextUncertain("unexpected_content_type")
            decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
            buffer, frame, count = "", [], 0
            while True:
                facts.data["stage"] = "read_sse"
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TextUncertain("deadline")
                socket = connection.sock or getattr(
                    getattr(response.fp, "raw", None), "_sock", None
                )
                if socket:
                    socket.settimeout(remaining)
                chunk = getattr(response, "read1", response.read)(4096)
                facts.data["received_bytes"] += len(chunk)
                if facts.data["received_bytes"] > 262144:
                    raise TextUncertain("stream_quota")
                facts.data["stage"] = "decode_sse"
                buffer += decoder.decode(chunk, final=not chunk)
                while True:
                    ends = [n for n in (buffer.find("\n"), buffer.find("\r")) if n >= 0]
                    if not ends:
                        break
                    end = min(ends)
                    if buffer[end] == "\r" and end + 1 == len(buffer) and chunk:
                        break  # CRLF may be split across network reads.
                    width = 2 if buffer[end : end + 2] == "\r\n" else 1
                    line, buffer = buffer[:end], buffer[end + width :]
                    if len(line.encode("utf-8")) > 65536:
                        raise TextUncertain("stream_quota")
                    if not line and frame:
                        count += 1
                        facts.data["sse_frames"] = count
                        if count > 256:
                            raise TextUncertain("event_quota")
                        data, frame = "\n".join(frame), []
                        if data != "[DONE]":
                            packet = _strict_json(data.encode("utf-8"))
                            facts.observe(packet)
                            yield packet
                    elif line.startswith("data:"):
                        data = line[5:]
                        frame.append(data[1:] if data.startswith(" ") else data)
                if len(buffer.encode("utf-8")) > 65536:
                    raise TextUncertain("stream_quota")
                if not chunk:
                    break
            if frame or buffer:
                raise TextUncertain("sse_unterminated")
        except TextUncertain as exc:
            facts.fail(
                exc.reason,
                exc
                if facts.data["primary_exception_type"] is None
                and exc.reason != "http_rejected"
                else None,
            )
            exc.diagnostic = facts.snapshot((token,))
            raise
        except Exception as exc:
            reason = (
                "utf8_invalid"
                if isinstance(exc, UnicodeDecodeError)
                else {
                    "prepare_request": "serialize_failed",
                    "connect": "connect_failed",
                    "send_request": "send_failed",
                    "read_headers": "headers_failed",
                    "http_error_body": "error_body_failed",
                    "decode_sse": "sse_json_invalid",
                }.get(facts.data["stage"], "stream_read_failed")
            )
            facts.fail(reason, exc)
            raise TextUncertain(reason, diagnostic=facts.snapshot((token,))) from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception as exc:
                    facts.data["close_exception_type"] = exception_type(exc)


class SiwcTextAdapter(AgentAdapter):
    revision = "siwc-responses-text-v2"

    def __init__(
        self, grant=None, *, transport=None, model=MODEL, mock=False, auth_error=False
    ):
        if mock and (transport is None or isinstance(transport, ResponsesHTTPS)):
            raise Conflict("Mock Text requires an explicit non-live test transport")
        self.grant, self.transport = grant, transport or ResponsesHTTPS()
        self.model, self.mock = model, mock
        self.auth_error = auth_error
        self.current_reference = None

    def configuration(self):
        return {
            "backend": "siwc_text_mock" if self.mock else "siwc_responses",
            "model": self.model,
            "effort": "low",
            "revision": self.revision,
            "authorization_ref": self.grant.reference if self.grant else None,
            "billing": "chatgpt_plan",
            "endpoint": "https://api.openai.com/v1/responses",
            "store": False,
            "stream": True,
            "tools": [],
            "tool_choice": "none",
            "deadline_seconds": DEADLINE,
            "max_public_characters": MAX_OUTPUT,
            "automatic_retries": 0,
            "file_inputs": False,
            "computer_access": False,
            "implementation_sha256": hashlib.sha256(
                Path(__file__).read_bytes()
            ).hexdigest(),
            "dependencies": {
                name: hashlib.sha256(
                    Path(__file__).with_name(name).read_bytes()
                ).hexdigest()
                for name in ("protocol.py", "siwc.py", "text_diagnostics.py")
            },
        }

    def ready(self):
        try:
            return bool(
                self.grant
                and self.grant.ready(self.model)
                and (
                    self.current_reference is None
                    or self.current_reference() == self.grant.reference
                )
            )
        except Exception:
            return False

    def run(self, request, emit, cancellation=None):
        facts = TransportDiagnostic()
        credentials = tuple(
            getattr(self.grant, name, "")
            for name in (
                "access_token",
                "refresh_token",
                "id_token",
            )
        )
        try:
            result = self._run(request, emit, cancellation, facts)
        except Exception as exc:
            if facts.data["termination_reason"] is None:
                reason = (
                    exc.reason if isinstance(exc, TextUncertain) else "adapter_failed"
                )
                facts.fail(reason, exc)
            diagnostic = facts.snapshot(credentials)
            emit(event("status", {"type": "transport.diagnostic", **diagnostic}))
            raise TextUncertain(
                facts.data["termination_reason"], diagnostic=diagnostic
            ) from None
        facts.data.update(
            stage="completed",
            outcome="completed",
            termination_reason="completed",
            safe_message=MESSAGES["completed"],
        )
        diagnostic = facts.snapshot(credentials)
        emit(event("status", {"type": "transport.diagnostic", **diagnostic}))
        result["transport_diagnostic"] = diagnostic
        return result

    def _run(self, request, emit, cancellation, facts):
        value = validate_request(request)
        if value["version"] != 2 or not self.ready():
            raise Conflict(
                "Fresh SIWC plan authorization and account model catalog required"
            )
        body = request_body(value["text"], self.model)
        started = time.monotonic()
        until = started + DEADLINE
        response_id, completed, chunks, used = None, None, [], 0
        published = 0
        credentials = (
            self.grant.access_token,
            self.grant.refresh_token,
            self.grant.id_token,
        )
        real_transport = isinstance(self.transport, ResponsesHTTPS)
        if real_transport:
            stream = self.transport.stream(
                body, self.grant.access_token, until, diagnostic=facts
            )
        else:
            facts.data.update(stage="read_sse", request_transmission="possibly_sent")
            stream = self.transport.stream(body, self.grant.access_token, until)
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
        try:
            for packet in stream:
                if not real_transport:
                    facts.observe(packet)
                facts.data["stage"] = "validate_event"
                used += 1
                if used > 256:
                    raise TextUncertain("event_quota")
                if cancellation and cancellation.requested:
                    raise TextUncertain("cancelled")
                if time.monotonic() >= until:
                    raise TextUncertain("deadline")
                if not isinstance(packet, dict) or not isinstance(
                    packet.get("type"), str
                ):
                    raise TextUncertain("invalid_event")
                kind = packet["type"]
                if kind == "transport.rejected":
                    if real_transport and facts.data["http_status"] in {None, 200}:
                        raise TextUncertain("unknown_event")
                    emit(
                        event(
                            "status",
                            {
                                "type": "transport.rejected",
                                "http_status": facts.data["http_status"],
                                "shape": facts.data["error_body_shape"],
                                "code": api_code(packet.get("code")),
                            },
                        )
                    )
                    continue
                if kind not in EVENT_TYPES:
                    raise TextUncertain("unknown_event")
                if kind in {"response.failed", "response.incomplete", "error"}:
                    failure = (
                        packet
                        if kind == "error"
                        else packet.get("response", {}).get("error", {})
                    )
                    code = failure.get("code") if isinstance(failure, dict) else None
                    facts.data["api_error_code"] = api_code(code)
                    emit(
                        event(
                            "status",
                            {
                                "status": "provider_failed"
                                if kind != "response.incomplete"
                                else "provider_incomplete",
                                "code": api_code(code),
                            },
                        )
                    )
                    raise TextUncertain("provider_failed")
                if kind in {"response.output_item.added", "response.output_item.done"}:
                    if packet.get("item", {}).get("type") not in {
                        "message",
                        "reasoning",
                    }:
                        raise TextUncertain("Unexpected tool item; never dispatched")
                if kind.startswith(
                    (
                        "response.function",
                        "response.custom_tool",
                        "response.web_search",
                        "response.code_interpreter",
                        "response.mcp",
                    )
                ):
                    raise TextUncertain("Unexpected tool activity; never dispatched")
                if kind == "response.created":
                    response_id = packet.get("response", {}).get("id")
                    if (
                        not isinstance(response_id, str)
                        or not response_id
                        or len(response_id) > 200
                    ):
                        raise TextUncertain("Missing provider identity")
                    if any(secret and secret in response_id for secret in credentials):
                        raise TextUncertain(
                            "Credential-like provider identity rejected"
                        )
                    emit(
                        event(
                            "status", {"status": "running", "response_id": response_id}
                        )
                    )
                if kind == "response.output_text.delta":
                    delta = packet.get("delta")
                    public_text(delta, limit=MAX_OUTPUT)
                    chunks.append(delta)
                    if sum(map(len, chunks)) > MAX_OUTPUT:
                        raise TextUncertain("SIWC text quota exceeded")
                    partial = "".join(chunks)
                    if any(secret and secret in partial for secret in credentials):
                        raise TextUncertain(
                            "Credential-like output rejected without logging"
                        )
                    # Withhold any trailing credential prefix, including across
                    # provider chunks, before persisting public provisional text.
                    safe_length = len(partial)
                    for secret in credentials:
                        for size in range(min(len(secret) - 1, len(partial)), 0, -1):
                            if partial.endswith(secret[:size]):
                                safe_length = min(safe_length, len(partial) - size)
                                break
                    # Fixed blocks bound event count and total persisted bytes.
                    while safe_length - published >= 400:
                        emit(
                            event(
                                "message",
                                {
                                    "delta": partial[published : published + 400],
                                    "provisional": True,
                                    "role": value["role"],
                                },
                            )
                        )
                        published += 400
                if kind == "response.completed":
                    completed = packet.get("response", {})
                    break
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()
        facts.data["stage"] = "validate_completed"
        if (
            not completed
            or completed.get("status") != "completed"
            or completed.get("id") != response_id
            or completed.get("model") != self.model
        ):
            raise TextUncertain(
                "completion_invalid"
                if facts.data["response_completed_received"]
                else "missing_completed"
            )
        output = completed.get("output")
        if not isinstance(output, list) or len(output) > 32:
            raise TextUncertain("Invalid SIWC output")
        text = []
        for item in output:
            if item.get("type") == "reasoning":
                continue  # Never retain reasoning content or encrypted reasoning.
            if (
                item.get("type") != "message"
                or item.get("role") != "assistant"
                or item.get("status") != "completed"
            ):
                raise TextUncertain("Unexpected output; no tool dispatcher exists")
            for content in item.get("content", []):
                if content.get("type") != "output_text":
                    raise TextUncertain("Response did not contain plain text")
                text.append(public_text(content.get("text"), limit=MAX_OUTPUT))
        answer = public_text("".join(text), limit=MAX_OUTPUT, empty=False)
        if any(secret and secret in answer for secret in credentials):
            raise TextUncertain("Credential-like output rejected without logging")
        if chunks and "".join(chunks) != answer:
            raise TextUncertain("Stream and completed text differ")
        usage = completed.get("usage")
        metrics = {}
        if isinstance(usage, dict):
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                if key in usage:
                    if type(usage[key]) is not int or usage[key] < 0:
                        raise TextUncertain("Invalid usage")
                    metrics[key] = usage[key]
        emit(event("usage", metrics))
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
        emit(event("status", {"status": "completed", "response_id": response_id}))
        return {
            "version": 1,
            "backend": self.configuration()["backend"],
            "real_inference": not self.mock,
            "response_id": response_id,
            "session_id": value["session_id"],
            "model": self.model,
            "text": answer,
            "request_body": body,
            "usage": metrics,
            "elapsed_seconds": time.monotonic() - started,
            "verified_knowledge": False,
            "computer_access": False,
        }


class TextExecutor(Executor):
    name = "team_text"
    revision = "gpt-text-core-v1"

    def __init__(self, adapter, authorized_run=None):
        self.adapter, self.authorized_run, self.core = adapter, authorized_run, None
        self.project_permit = None
        self._stops, self._stop_lock = {}, Lock()

    @property
    def supports_cancellation(self):
        return getattr(self.adapter, "supports_cancellation", False)

    def permitted(self, run_id, value):
        if value["version"] == 4:
            return bool(self.project_permit and self.project_permit(run_id, value))
        return self.adapter.mock or self.authorized_run == run_id

    def approval_configuration(self):
        return {
            "adapter": self.adapter.configuration(),
            "protocol": "gpt-text-v1",
            "storage": "sealed-sqlite-blobs",
            "computer_access": False,
            "engineering_candidate_validation": "in_memory_patch_and_python_syntax_only",
        }

    def prepare(self, request):
        value = validate_request(json.loads(request.action.parameters_json))
        config = self.adapter.configuration()
        if (
            request.action.kind != "team.turn"
            or value["version"] not in {2, 3, 4}
            or (
                value["version"] in {3, 4}
                and not getattr(self.adapter, "engineering_supported", False)
            )
            or json.loads(request.description) != value
        ):
            raise Conflict("Exact Text action required")
        if {
            k: v for k, v in value["inputs"].items() if k not in {"context", "review", "scope", "deadline_seconds"}
        } != {
            "model": config["model"],
            "effort": config["effort"],
            "profile_digest": digest(config),
        }:
            raise Conflict("Text model, authorization or profile changed")
        if not self.permitted(request.run_id, value):
            raise Conflict("Separate operator permit for one exact Text Run required")
        if not self.adapter.ready():
            raise Conflict("SIWC sign-in or model availability must be checked first")
        if "deadline_seconds" in value["inputs"] and not self.adapter.ready_for_deadline(value["inputs"]["deadline_seconds"]):
            raise Conflict("SIWC authorization is too short for the approved deadline")
        validate_runtime = getattr(self.adapter, "validate_runtime", None)
        if validate_runtime:
            validate_runtime()

    def execute(self, request):
        value = validate_request(json.loads(request.action.parameters_json))
        events = []

        def emit(item):
            checked = event(item["kind"], item["data"])
            if len(events) >= 128 or len(canonical(events + [checked])) > 28000:
                raise Conflict("Text event quota exceeded")
            events.append(checked)
            with self.core.store.transaction() as conn:
                self.core.store.event(
                    conn,
                    "team.agent_event",
                    run_id=request.run_id,
                    task_id=request.task_id,
                    session_id=value["session_id"],
                    role=value["role"],
                    sequence=len(events),
                    agent_event=checked,
                )

        if self.supports_cancellation:
            stopped = Event()
            outer = request.cancellation

            class LocalCancellation(CancellationToken):
                @property
                def requested(self):
                    return super().requested or bool(outer and outer.requested)

            with self._stop_lock:
                self._stops[request.run_id] = stopped
            try:
                result = self.adapter.run(value, emit, cancellation=LocalCancellation(stopped))
            finally:
                with self._stop_lock:
                    self._stops.pop(request.run_id, None)
        else:
            result = self.adapter.run(value, emit)
        engineering_artifacts = ()
        if value["version"] in {3, 4}:
            checked = engineering.assess(value, result["text"])
            engineering_artifacts = (
                ProducedArtifact("engineering.json", canonical(checked).encode()),
            )
        return ExecutionResult(
            tuple(
                ProducedArtifact(name, canonical(content).encode())
                for name, content in (
                    ("request.json", value),
                    ("result.json", result),
                    ("events.json", events),
                    (
                        "shadow-material.json",
                        {
                            **(
                                {
                                    "role": value["role"],
                                    "public_response": result["text"],
                                    "engineering_evidence": checked,
                                    **(
                                        {
                                            "team_evidence": value["inputs"]["context"],
                                            "sealed_review_scope": value["inputs"].get(
                                                "review", value["inputs"].get("scope")
                                            ),
                                        }
                                        if value["phase"] == "review"
                                        else {}
                                    ),
                                }
                                if engineering_artifacts
                                else {}
                            ),
                            "status": "unreviewed",
                            "response_sha256": digest(result),
                            "verified_knowledge": False,
                            "qwen_called": False,
                        },
                    ),
                )
            )
            + engineering_artifacts,
            storage="sqlite",
        )

    def request_stop(self, run_id):
        with self._stop_lock:
            stopped = self._stops.get(run_id)
            if stopped is None:
                return False
            stopped.set()
            return True  # Only a local cancellation signal.


def build_text(root, authorized_run=None, *, auth_dir=None):
    from .devkit import DevkitTextAdapter

    vault = Vault(
        auth_dir if auth_dir is not None else Path(root).parent / "gpt-text-auth"
    )
    try:
        grant = vault.load()
    except Exception:
        # A corrupt/new auth store must not take existing School offline.
        return TextExecutor(DevkitTextAdapter(auth_error=True), authorized_run)
    adapter = DevkitTextAdapter(grant)

    def current_reference():
        current = vault.load()
        return current.reference if current else None

    adapter.current_reference = current_reference
    return TextExecutor(adapter, authorized_run)
