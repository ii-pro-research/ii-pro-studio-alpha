"""Replaceable student backends. Model output is data, never executable code."""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import re
import socket
import threading
import time
from abc import abstractmethod
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from ii_pro_core.errors import ValidationError
from ii_pro_core.executors import ExecutionResult, Executor, ProducedArtifact
from ii_pro_core.security import canonical, digest

ADAPTER_REVISION = "school-ollama-v1"
MAX_REPLY_BYTES = 65536


class StudentFailure(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)  # stable diagnostic only, no raw server errors


@dataclass(frozen=True)
class ModelProfile:
    model: str
    expected_digest: str
    options_json: str
    endpoint: str = "http://127.0.0.1:11434"
    deadline_seconds: float = 180.0
    adapter_revision: str = ADAPTER_REVISION

    def __post_init__(self):
        url = urlsplit(self.endpoint)
        if (url.scheme != "http" or url.hostname != "127.0.0.1" or url.username or url.password
                or url.path or url.query or url.fragment or not url.port or not 1 <= url.port <= 65535):
            raise ValidationError("Only explicit loopback Ollama endpoints are allowed")
        if not re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,120}", self.model):
            raise ValidationError("Invalid model tag")
        if not re.fullmatch(r"[a-f0-9]{64}", self.expected_digest):
            raise ValidationError("Model digest must be pinned")
        opts = json.loads(self.options_json)
        if not isinstance(opts, dict) or set(opts) != {"num_ctx", "num_predict", "temperature", "seed", "num_thread"}:
            raise ValidationError("Invalid generation options")
        for key, low, high in (("num_ctx", 512, 2048), ("num_predict", 1, 512),
                               ("seed", 0, 2147483647), ("num_thread", 1, 2)):
            if type(opts[key]) is not int or not low <= opts[key] <= high:
                raise ValidationError("Generation budget exceeded")
        temp = opts["temperature"]
        if type(temp) not in (int, float) or not math.isfinite(temp) or not 0 <= temp <= 1:
            raise ValidationError("Invalid temperature")
        if type(self.deadline_seconds) not in (int, float) or not 0 < self.deadline_seconds <= 180:
            raise ValidationError("Invalid student deadline")
        if self.adapter_revision != ADAPTER_REVISION:
            raise ValidationError("Unsupported adapter revision")
        object.__setattr__(self, "options_json", canonical(opts))

    @classmethod
    def build(cls, model: str, expected_digest: str, **kwargs):
        options = kwargs.pop("options", {"num_ctx": 2048, "num_predict": 384,
                                         "temperature": 0.1, "seed": 42, "num_thread": 2})
        return cls(model, expected_digest, canonical(options), **kwargs)

    @property
    def options(self):
        return json.loads(self.options_json)

    @property
    def id(self):
        return digest(asdict(self))


@dataclass(frozen=True)
class StudentReply:
    text: str
    receipt: dict
    complete: bool = True


class StudentAdapter(Executor):
    name = "school_student"
    revision = ADAPTER_REVISION
    supports_cancellation = False  # closing HTTP is not a backend stop receipt

    def approval_configuration(self):
        return asdict(self.profile)

    def __init__(self, profile: ModelProfile):
        self._profile = profile
        self.capture = None
        self.capture_diagnostic = None
        self.diagnostics: dict[str, str] = {}
        self.snapshots: dict[str, dict] = {}

    @property
    def profile(self):
        return self._profile

    @abstractmethod
    def probe(self) -> dict:
        """Read-only metadata; must never generate or download a model."""

    @abstractmethod
    def respond(self, prompt: str) -> StudentReply:
        """One request only; no automatic retries or tool execution."""

    def prepare(self, request):
        if request.action.kind != "school.propose" or self.capture is None:
            raise ValidationError("A School-bound proposal action is required")
        params = json.loads(request.action.parameters_json)
        if params["profile_digest"] != self.profile.id:
            raise ValidationError("Student profile mismatch")
        try:
            self.snapshots[request.run_id] = self.probe()
        except StudentFailure as exc:
            self.diagnostics[request.run_id] = exc.code
            if self.capture_diagnostic:
                self.capture_diagnostic(request.run_id, exc.code, in_prepare=True)
            raise

    def execute(self, request):
        params = json.loads(request.action.parameters_json)
        try:
            reply = self.respond(params["prompt"])
            if not isinstance(reply.text, str) or len(reply.text.encode("utf-8")) > MAX_REPLY_BYTES:
                raise StudentFailure("invalid_answer")
            receipt = {**reply.receipt, "complete": reply.complete,
                       "run_id": request.run_id, "task_id": request.task_id,
                       "profile": asdict(self.profile), "profile_digest": self.profile.id,
                       "request_parameters": params, "prompt_sha256": hashlib.sha256(params["prompt"].encode()).hexdigest(),
                       "snapshot": self.snapshots[request.run_id]}
            # Durable capture precedes Core artifact import. If import/commit
            # fails, the original bytes survive and the Run stays unresolved.
            self.capture(request.run_id, reply.text.encode("utf-8"), receipt)
            if not reply.complete:
                raise StudentFailure("incomplete_response")
            return ExecutionResult((ProducedArtifact("response.txt", reply.text.encode("utf-8")),
                                    ProducedArtifact("receipt.json", canonical(receipt).encode("utf-8"))))
        except StudentFailure as exc:
            self.diagnostics[request.run_id] = exc.code
            if self.capture_diagnostic:
                self.capture_diagnostic(request.run_id, exc.code, in_prepare=False)
            raise
        finally:
            self.snapshots.pop(request.run_id, None)


def _strict_json(raw: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("Invalid JSON constant")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)


class LoopbackTransport:
    """No proxy, DNS, redirects, retries or arbitrary routes. Total deadline.

    A watchdog shuts down our own socket, not the Ollama process/request.
    """
    def __init__(self, endpoint: str):
        url = urlsplit(endpoint)
        if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or url.path or url.query or url.fragment or url.username or url.password:
            raise ValidationError("Invalid loopback endpoint")
        self._port = url.port

    @property
    def port(self):
        return self._port

    def request(self, route: str, payload=None, *, timeout: float = 8, observation=None):
        if route not in {"version", "tags", "show", "ps", "chat"}:
            raise ValidationError("Unsupported Ollama route")
        if type(timeout) not in (int, float) or not 0 < timeout <= 180:
            raise ValidationError("Invalid HTTP deadline")
        started = time.monotonic()
        expired, sockets = threading.Event(), []
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=min(2, timeout))
        received, wire_sha = 0, hashlib.sha256()
        complete = False
        def expire():
            expired.set()
            for sock in sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            connection.connect()
            sockets.append(connection.sock)
            if expired.is_set():
                raise StudentFailure("timeout")
            connection.sock.settimeout(max(.001, timeout - (time.monotonic() - started)))
            body = None if payload is None else canonical(payload).encode("utf-8")
            connection.request("GET" if body is None else "POST", "/api/" + route, body=body,
                               headers={"Content-Type": "application/json", "Connection": "close"})
            response = connection.getresponse()
            if observation is not None:
                observation["http_status"] = response.status
            if response.status != 200:
                raise StudentFailure(f"http_{response.status}")
            if response.getheader("Content-Length") and int(response.getheader("Content-Length")) > MAX_REPLY_BYTES:
                raise StudentFailure("response_too_large")
            chunks, size = [], 0
            while True:
                if expired.is_set() or time.monotonic() - started >= timeout:
                    raise StudentFailure("timeout")
                chunk = response.read1(min(4096, MAX_REPLY_BYTES + 1 - size))
                if not chunk:
                    break
                if observation is not None:
                    received += len(chunk)
                    wire_sha.update(chunk)
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_REPLY_BYTES:
                    raise StudentFailure("response_too_large")
            if expired.is_set():
                raise StudentFailure("timeout")
            complete = observation is None or response.length in (0, None)
            if observation is not None and not complete:
                raise StudentFailure("unavailable")
            raw = b"".join(chunks)
            try:
                data = _strict_json(raw)
            except json.JSONDecodeError as error:
                if observation is not None:
                    observation["response_json"] = json_error_kind(error)
                raise
            if not isinstance(data, dict):
                raise ValueError("JSON object expected")
            return data
        except StudentFailure:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise StudentFailure("timeout" if expired.is_set() or isinstance(exc, TimeoutError) else "unavailable") from None
        except (ValueError, UnicodeError, RecursionError):
            raise StudentFailure("invalid_json") from None
        finally:
            if observation is not None and "http_status" in observation:
                observation.update(response_bytes=received, response_complete=complete)
                if received or complete:
                    observation["response_sha256"] = wire_sha.hexdigest()
            connection.close()
            timer.cancel()
            timer.join(timeout=1)


def json_error_kind(error):
    """Report an unfinished JSON prefix only when the decoder points to its end."""
    suffix = error.doc[error.pos:].strip()
    if (not suffix or error.msg.startswith("Unterminated string")
            or error.msg == "Expecting value" and any(word.startswith(suffix) for word in ("true", "false", "null"))):
        return "incomplete_json"
    return "invalid_json"


class OllamaStudentAdapter(StudentAdapter):
    revision = ADAPTER_REVISION

    def approval_configuration(self):
        if type(self.transport) is not LoopbackTransport or self.transport.port != urlsplit(self.profile.endpoint).port:
            raise ValidationError("Protected Ollama requires its declared immutable loopback transport")
        return {"model_profile": asdict(self.profile), "transport_port": self.transport.port,
                "transport_revision": "loopback-http-v1"}

    def __init__(self, profile: ModelProfile, *, transport=None):
        super().__init__(profile)
        self._transport = transport or LoopbackTransport(profile.endpoint)

    @property
    def transport(self):
        return self._transport

    def probe(self):
        version = self.transport.request("version")
        tags = self.transport.request("tags").get("models", [])
        if not isinstance(version.get("version"), str) or not isinstance(tags, list):
            raise StudentFailure("invalid_metadata")
        model = next((m for m in tags if isinstance(m, dict) and m.get("name") == self.profile.model), None)
        if model is None:
            raise StudentFailure("model_missing")
        if model.get("digest") != self.profile.expected_digest:
            raise StudentFailure("model_drift")
        show = self.transport.request("show", {"model": self.profile.model})
        if not isinstance(show.get("template", ""), str) or not isinstance(show.get("system", ""), str):
            raise StudentFailure("invalid_metadata")
        # Small allowlist; do not persist licenses, paths, auth/config dumps.
        return {"server_version": version.get("version"), "model": self.profile.model,
                "digest": model["digest"], "details": show.get("details"),
                "saved_parameters": show.get("parameters"),
                "template_sha256": hashlib.sha256(show.get("template", "").encode()).hexdigest(),
                "system_sha256": hashlib.sha256(show.get("system", "").encode()).hexdigest()}

    def respond(self, prompt):
        payload = {"model": self.profile.model, "stream": False, "keep_alive": "30s",
                   "messages": [{"role": "system", "content": "You are a Python student. Return a proposal only; never execute code or call tools."},
                                {"role": "user", "content": prompt}], "options": self.profile.options}
        started = time.monotonic()
        data = self.transport.request("chat", payload, timeout=self.profile.deadline_seconds)
        message = data.get("message")
        if (data.get("model") != self.profile.model or not isinstance(message, dict)
                or not isinstance(message.get("content"), str)):
            raise StudentFailure("invalid_response")
        text = message["content"]  # NEVER strip, normalize newlines, or rewrite
        complete = (data.get("done") is True and data.get("done_reason") == "stop"
                    and not message.get("tool_calls") and bool(text.strip()))
        metrics = {}
        for key in ("total_duration", "load_duration", "prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration"):
            if key in data:
                if type(data[key]) is not int or data[key] < 0:
                    raise StudentFailure("invalid_statistics")
                metrics[key] = data[key]
        return StudentReply(text, {"backend": "ollama", "elapsed_seconds": time.monotonic() - started,
                                   "done_reason": data.get("done_reason"), "statistics": metrics,
                                   "request_sha256": digest(payload), "response_sha256": digest(data),
                                   "keep_alive": "30s"}, complete)
