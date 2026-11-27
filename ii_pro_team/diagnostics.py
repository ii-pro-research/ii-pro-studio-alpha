"""One explicitly requested metadata-only App Server connection.

Uses the existing Core executor/lifetime/Artifact interfaces. No browser starts
processes, no agent thread or turn is permitted on this transport, and no raw
provider packet/configuration/credential is saved.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

from ii_pro_core.errors import Conflict, RecoveryRequired, ValidationError
from ii_pro_core.executors import Executor, ExecutionResult, ProducedArtifact
from ii_pro_core.models import EffectStatus
from ii_pro_core.reconciliation import Reconciler, ReconciliationReceipt
from ii_pro_core.security import (
    Action,
    canonical,
    digest,
    no_links,
    public_text,
    safe_root,
)
from .app_server import AppServerClient, StdioTransport, TransportLost
from .protocol import identity

DIAGNOSTIC_METHODS = frozenset(
    {"initialize", "initialized", "config/read", "account/read", "model/list"}
)
PERMISSION_METHODS = frozenset(
    {"permissionProfile/list", "configRequirements/read", "windowsSandbox/readiness"}
)
OVERRIDES = (
    'sandbox_mode="read-only"',
    'approval_policy="on-request"',
    'approvals_reviewer="user"',
    'forced_login_method="chatgpt"',
    "mcp_servers={}",
    "apps._default.enabled=false",
    "agents.enabled=false",
    "features.shell_tool=false",
    "features.unified_exec=false",
    "features.apply_patch_freeform=false",
    "features.js_repl=false",
    "features.code_mode=false",
    'web_search="disabled"',
)
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-.][a-zA-Z0-9]+)*")


@dataclass(frozen=True)
class CliIdentity:
    executable: str
    sha256: str
    version: str
    startup_version: str


def child_environment():
    # The official runtime owns its managed login. Do not copy/read auth files
    # or pass OPENAI_API_KEY, browser tokens, tools, or our UI session capability.
    names = (
        "PATH",
        "SystemRoot",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "HOMEDRIVE",
        "HOMEPATH",
        "CODEX_HOME",
    )
    return {key: os.environ[key] for key in names if key in os.environ}


def arguments(executable, *, preflight=False, disabled_mcp=()):
    args = [
        str(executable),
        "app-server",
        "--strict-config",
        "--print-startup-config" if preflight else "--stdio",
    ]
    for override in OVERRIDES:
        args += ["-c", override]
    for name in disabled_mcp:
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", name):
            raise ValidationError("Unsafe MCP configuration key")
        # Codex -c splits dotted keys itself; validated bare TOML keys avoid
        # turning quote characters into a different MCP server name.
        args += ["-c", "mcp_servers." + name + ".enabled=false"]
    return args


def discover_cli():
    selected = shutil.which("codex")
    if not selected:
        return None
    path = Path(selected).resolve(strict=True)
    no_links(path)
    options = dict(
        capture_output=True,
        timeout=15,
        shell=False,
        env=child_environment(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    result = subprocess.run([str(path), "--version"], **options)
    match = re.fullmatch(rb"codex-cli ([a-zA-Z0-9.\-]+)\s*", result.stdout)
    if result.returncode or not match:
        raise ValidationError("Unsupported Codex CLI identity")
    version = match[1].decode("ascii")
    if not VERSION.fullmatch(version):
        raise ValidationError("Invalid Codex version")
    startup = subprocess.run(arguments(path, preflight=True), **options)
    if startup.returncode or len(startup.stdout) > 65536:
        raise ValidationError("Codex startup profile validation failed")
    data = json.loads(startup.stdout)
    startup_version = data.get("appServerVersion")
    if startup_version != version:
        raise ValidationError("App Server / CLI version mismatch")
    return CliIdentity(
        str(path),
        hashlib.sha256(path.read_bytes()).hexdigest(),
        version,
        startup_version,
    )


class DiagnosticTransport:
    """The method gate applies at the wire, including direct transport callers."""

    def __init__(self, delegate, cwd, *, probe_permissions=False):
        if type(probe_permissions) is not bool:
            raise ValidationError("Explicit permission probe capability required")
        self.delegate, self.cwd = delegate, str(cwd)
        self.probe_permissions = probe_permissions
        self.trace = []
        self.until = time.monotonic() + 45
        self.unexpected_activity = False

    def send(self, frame):
        if time.monotonic() >= self.until:
            raise TransportLost("Diagnostic connection deadline exceeded")
        if not isinstance(frame, dict):
            raise ValidationError("Invalid diagnostic frame")
        method, params = frame.get("method"), frame.get("params", {})
        if method is None:
            if set(frame) == {"id", "result"} and frame["result"] == {
                "decision": "decline"
            }:
                label = "authority_declined"
            elif set(frame) == {"id", "error"} and frame["error"].get("code") == -32601:
                label = "authority_rejected"
            else:
                raise ValidationError("Diagnostic connection cannot grant authority")
        else:
            allowed = DIAGNOSTIC_METHODS | (
                PERMISSION_METHODS if self.probe_permissions else frozenset()
            )
            if method not in allowed:
                raise ValidationError(
                    "Session/turn/tool/config mutation forbidden in diagnostics"
                )
            valid = (
                method == "initialize"
                and params
                == {
                    "clientInfo": {"name": "ii_pro_studio", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": self.probe_permissions},
                }
                or method == "initialized"
                and params == {}
                or method == "account/read"
                and params == {"refreshToken": False}
                or method == "config/read"
                and params == {"includeLayers": False, "cwd": self.cwd}
                or method == "model/list"
                and isinstance(params, dict)
                and set(params) <= {"limit", "includeHidden", "cursor"}
                and type(params.get("limit")) is int
                and params["limit"] == 20
                and params.get("includeHidden") is False
                and (
                    "cursor" not in params
                    or isinstance(params["cursor"], str)
                    and len(params["cursor"]) <= 1000
                )
                or self.probe_permissions
                and method in {"configRequirements/read", "windowsSandbox/readiness"}
                and params == {}
                or self.probe_permissions
                and method == "permissionProfile/list"
                and set(params) <= {"cwd", "limit", "cursor"}
                and params.get("cwd") == self.cwd
                and type(params.get("limit")) is int
                and params["limit"] == 20
                and (
                    "cursor" not in params
                    or isinstance(params["cursor"], str)
                    and 1 <= len(params["cursor"]) <= 1000
                )
            )
            if not valid:
                raise ValidationError(
                    "Diagnostic method parameters differ from the fixed profile"
                )
            label = method
        if len(self.trace) >= 32:
            raise TransportLost("Diagnostic RPC quota exceeded")
        self.trace.append({"direction": "outbound", "method": label})
        self.delegate.send(frame)

    def receive(self, timeout):
        remaining = self.until - time.monotonic()
        if remaining <= 0:
            raise TransportLost("Diagnostic connection deadline exceeded")
        value = self.delegate.receive(min(timeout, remaining))
        if (
            isinstance(value, dict)
            and "id" not in value
            and isinstance(value.get("method"), str)
        ):
            method = value["method"]
            if method.startswith(("thread/started", "turn/", "item/", "process/")):
                self.unexpected_activity = True
                raise TransportLost(
                    "Unexpected agent activity on metadata-only connection"
                )
        return value


def restrictions(config):
    if not isinstance(config, dict):
        raise ValidationError("Missing effective server configuration")
    features = config.get("features", {})
    mcp = config.get("mcp_servers", {})
    apps = config.get("apps", {}).get("_default", {})
    checked = {
        "read_only": config.get("sandbox_mode") == "read-only",
        "human_reviewer": config.get("approval_policy") == "on-request"
        and config.get("approvals_reviewer") == "user",
        "chatgpt_only": config.get("forced_login_method") == "chatgpt",
        "no_mcp_servers": isinstance(mcp, dict)
        and all(
            isinstance(value, dict) and value.get("enabled") is False
            for value in mcp.values()
        ),
        "apps_disabled": apps.get("enabled") is False,
        "agents_disabled": config.get("agents", {}).get("enabled") is False,
        "web_disabled": config.get("web_search") == "disabled",
    }
    for key in (
        "shell_tool",
        "unified_exec",
        "apply_patch_freeform",
        "js_repl",
        "code_mode",
    ):
        checked[key + "_disabled"] = features.get(key) is False
    if not all(checked.values()):
        error = Conflict(
            "Effective App Server restrictions do not match diagnostic profile"
        )
        error.public_checks = checked
        raise error
    return checked


def models(client):
    results, cursor = [], None
    for _ in range(3):
        args = {"limit": 20, "includeHidden": False}
        if cursor is not None:
            args["cursor"] = cursor
        value = client.call("model/list", args)
        if not isinstance(value.get("data"), list) or len(value["data"]) > 20:
            raise TransportLost("Invalid model catalog")
        for item in value["data"]:
            model = item.get("model") if isinstance(item, dict) else None
            if not isinstance(model, str) or not re.fullmatch(
                r"[a-zA-Z0-9._:/-]{1,120}", model
            ):
                raise TransportLost("Invalid public model identity")
            public_text(model)
            efforts = [
                p["reasoningEffort"]
                for p in item.get("supportedReasoningEfforts", [])
                if isinstance(p, dict)
                and isinstance(p.get("reasoningEffort"), str)
                and re.fullmatch(r"[a-z0-9_]{1,32}", p["reasoningEffort"])
            ]
            results.append(
                {
                    "model": model,
                    "is_default": item.get("isDefault") is True,
                    "reasoning_efforts": efforts[:12],
                    "access_verified": False,
                }
            )
        cursor = value.get("nextCursor")
        if cursor is None:
            return results, False
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1000:
            raise TransportLost("Invalid catalog cursor")
    return results, True


def permission_observation(client, cwd):
    """Observe declarations only: catalog membership never proves enforcement.

    No thread, command, setup or permission selection is performed. Raw managed
    requirements and custom profile names/paths never enter events or artifacts.
    """
    profiles, cursor = [], None
    for _ in range(3):
        params = {"cwd": str(cwd), "limit": 20}
        if cursor is not None:
            params["cursor"] = cursor
        reply = client.call("permissionProfile/list", params)
        page = reply.get("data")
        if not isinstance(page, list) or len(page) > 20:
            raise TransportLost("Invalid permission profile catalog")
        for entry in page:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("id"), str)
                or not 1 <= len(entry["id"]) <= 120
                or type(entry.get("allowed")) is not bool
            ):
                raise TransportLost("Invalid permission profile entry")
            name = entry["id"]
            builtin = name in {":read-only", ":workspace", ":danger-full-access"}
            profiles.append(
                {
                    "id": name
                    if builtin
                    else "custom-" + hashlib.sha256(name.encode()).hexdigest()[:16],
                    "builtin": builtin,
                    "allowed": entry["allowed"],
                    "enforcement_verified": False,
                }
            )
        cursor = reply.get("nextCursor")
        if cursor is None:
            break
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1000:
            raise TransportLost("Invalid permission profile cursor")
    requirements = client.call("configRequirements/read", {})
    if "requirements" not in requirements or (
        requirements["requirements"] is not None
        and not isinstance(requirements["requirements"], dict)
    ):
        raise TransportLost("Invalid managed requirements response")
    readiness = client.call("windowsSandbox/readiness", {}).get("status")
    if readiness not in {"ready", "notConfigured", "updateRequired"}:
        raise TransportLost("Invalid Windows sandbox readiness")
    return {
        "profiles": profiles,
        "catalog_truncated": cursor is not None,
        "requirements_present": requirements["requirements"] is not None,
        "windows_sandbox": readiness,
        "selected_profile": None,
        "legacy_sandbox": "read-only",
        "text_ready": False,
        "reason": "Metadata only; no tool/file isolation proved",
    }


class DiagnosticExecutor(Executor):
    name = "codex_diagnostics"
    revision = "codex-diagnostic-v02"
    supports_cancellation = False

    def __init__(
        self, root, *, cli=None, transport_factory=None, probe_permissions=False
    ):
        if type(probe_permissions) is not bool:
            raise ValidationError("Explicit permission probe capability required")
        self.root = Path(root)
        self.probe_permissions = probe_permissions
        self.cli = cli
        self.transport_factory = transport_factory
        self.core = None
        self.last = None

    def approval_configuration(self):
        return {
            "mode": "diagnostics_only",
            "cli": None if self.cli is None else self.cli.__dict__,
            "overrides": list(OVERRIDES),
            "methods": sorted(
                DIAGNOSTIC_METHODS
                | (PERMISSION_METHODS if self.probe_permissions else frozenset())
            ),
            "probe_permissions": self.probe_permissions,
            "workspace": str(self.root),
            "rpc_deadline_seconds": 15,
            "connection_deadline_seconds": 45,
            "pages": 3,
            "mcp_handling": "inventory_then_explicit_disable_v1_max_two_children",
        }

    def prepare(self, request):
        params = json.loads(request.action.parameters_json)
        if request.action.kind != "codex.diagnose" or params[
            "profile_digest"
        ] != digest(self.approval_configuration()):
            raise Conflict("Diagnostic profile changed")
        if self.core is None or self.cli is None:
            raise ValidationError("Codex is not installed")
        binary = Path(self.cli.executable)
        no_links(binary)
        if hashlib.sha256(binary.read_bytes()).hexdigest() != self.cli.sha256:
            raise Conflict("Codex binary identity changed")

    def execute(self, request):
        cwd = safe_root(self.root / request.run_id)
        self.last = {
            "version": 1,
            "run_id": request.run_id,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "cli_installed": True,
            "cli_version": self.cli.version,
            "app_server_version": self.cli.startup_version,
            "binary_sha256": self.cli.sha256,
            "connection": "starting",
            "auth": "not_checked",
            "models": [],
            "agent_sessions": 0,
            "turns": 0,
            "last_error": None,
            "stop_confirmed": False,
        }
        raw = None
        release = None
        gate = None
        started = time.monotonic()
        trace = []
        gates = []

        def connect(disabled_mcp=()):
            if self.transport_factory:
                transport = (
                    self.transport_factory(self.cli, cwd, disabled_mcp=disabled_mcp)
                    if disabled_mcp
                    else self.transport_factory(self.cli, cwd)
                )
            else:
                if disabled_mcp:
                    probe = subprocess.run(
                        arguments(
                            self.cli.executable,
                            preflight=True,
                            disabled_mcp=disabled_mcp,
                        ),
                        env=child_environment(),
                        capture_output=True,
                        shell=False,
                        timeout=15,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    if probe.returncode:
                        raise ValidationError(
                            "Explicit MCP-disable startup profile rejected"
                        )
                    self.last["hardened_startup_validated"] = True
                process = subprocess.Popen(
                    arguments(self.cli.executable, disabled_mcp=disabled_mcp),
                    cwd=cwd,
                    env=child_environment(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                transport = StdioTransport(process.stdout, process.stdin)
                transport.process = process
            return transport

        def inspect(transport):
            scoped = DiagnosticTransport(
                transport, cwd, probe_permissions=self.probe_permissions
            )
            scoped.trace = trace
            scoped.until = started + 45
            gates.append(scoped)
            rpc = AppServerClient(scoped, deadline=15)

            def emit(item):
                with self.core.store.transaction() as conn:
                    self.core.store.event(
                        conn,
                        "codex.diagnostic_authority_denied",
                        run_id=request.run_id,
                        task_id=request.task_id,
                        method=item["data"]["method"],
                    )

            rpc.emit = emit
            rpc.initialize(experimental=self.probe_permissions)
            config = rpc.call("config/read", {"includeLayers": False, "cwd": str(cwd)})[
                "config"
            ]
            return scoped, rpc, config

        try:
            raw = connect()
            release = self.core.store.retain_external_execution(
                request.run_id, threading.Event()
            )
            gate, client, config = inspect(raw)
            mcp = config.get("mcp_servers", {})
            if not isinstance(mcp, dict) or len(mcp) > 64:
                raise Conflict("MCP configuration is not inspectable")
            if any(
                not isinstance(value, dict) or value.get("enabled") is not False
                for value in mcp.values()
            ):
                # Empty tables merge rather than clear inherited entries. Only
                # metadata is sent on this inventory connection. Close it before
                # opening the explicitly disabled profile; never retry a turn.
                names = tuple(sorted(mcp))
                arguments(
                    self.cli.executable, disabled_mcp=names
                )  # validate keys before closing
                if not raw.close():
                    raise TransportLost("Inventory process stop not confirmed")
                process = getattr(raw, "process", None)
                self.last["inventory_exit_code"] = (
                    process.poll() if process is not None else None
                )
                self.last["disabled_mcp_count"] = len(names)
                raw = None
                raw = connect(names)
                gate, client, config = inspect(raw)
            self.last["connection"] = "connected"
            self.last["restrictions"] = restrictions(config)
            self.last["auth"] = "checking"
            auth = client.auth_status()
            self.last["auth_mode"] = (
                auth["mode"]
                if auth["mode"] in {"chatgpt", "apiKey", "unknown"}
                else "other"
            )
            self.last["auth"] = (
                "confirmed"
                if auth["authenticated"] and auth["mode"] == "chatgpt"
                else "login_required"
            )
            if self.last["auth"] == "confirmed":
                self.last["models"], self.last["catalog_truncated"] = models(client)
            if self.probe_permissions:
                self.last["permission_probe"] = permission_observation(client, cwd)
        except Exception as exc:
            if isinstance(exc, Conflict) and hasattr(exc, "public_checks"):
                self.last["restrictions"] = exc.public_checks
            if self.last["auth"] == "checking":
                self.last["auth"] = "error"
            self.last["last_error"] = (
                type(exc).__name__
                if isinstance(
                    exc,
                    (Conflict, ValidationError, TransportLost, OSError, TimeoutError),
                )
                else "DiagnosticError"
            )
            self.last["connection"] = "error"
            raise
        finally:
            try:
                stopped = raw is None or raw.close()
            except (OSError, ValueError):
                stopped = False
            self.last["stop_confirmed"] = stopped
            self.last["elapsed_seconds"] = time.monotonic() - started
            self.last["trace"] = trace
            self.last["unexpected_agent_activity"] = any(
                g.unexpected_activity for g in gates
            )
            if raw is not None:
                process = getattr(raw, "process", None)
                self.last["process_exit_code"] = (
                    process.poll() if process is not None else None
                )
            if release and stopped:
                release()
            if stopped and self.last["connection"] == "connected":
                self.last["connection"] = "closed"
            if not stopped:
                self.last["connection"] = "unconfirmed"
                self.last["last_error"] = "termination_unconfirmed"
            observation_digest = digest(self.last)
            try:
                with self.core.store.transaction() as conn:
                    self.core.store.event(
                        conn,
                        "codex.diagnostic_observation",
                        run_id=request.run_id,
                        task_id=request.task_id,
                        observation=self.last,
                    )
            finally:
                if release and not stopped:
                    watched = getattr(raw, "process", None)

                    def observe():
                        if watched is not None:
                            code = watched.wait()
                            try:
                                with self.core.store.transaction() as conn:
                                    self.core.store.event(
                                        conn,
                                        "codex.owned_process_exit",
                                        run_id=request.run_id,
                                        task_id=request.task_id,
                                        observation_digest=observation_digest,
                                        process_exit_code=code,
                                    )
                            finally:
                                release()

                    threading.Thread(
                        target=observe, name="codex-owned-exit-observer", daemon=True
                    ).start()
        if not self.last["stop_confirmed"]:
            raise TransportLost("Own App Server termination not confirmed")
        return ExecutionResult(
            (
                ProducedArtifact(
                    "codex-diagnostics.json", canonical(self.last).encode()
                ),
            ),
            storage="sqlite",
        )


class Diagnostics:
    """Trusted startup command and read-only status; no HTTP mutation methods."""

    def __init__(self, core, executor):
        self.core, self.executor = core, executor
        executor.core = core

    def run(self, key):
        identity(key)
        if self.core.store.all(
            "SELECT 1 FROM runs WHERE status IN ('running','unknown') OR effect_status IN ('unknown','partial') LIMIT 1"
        ):
            raise RecoveryRequired("Unresolved execution blocks diagnostic reconnect")
        if self.executor.cli is None:
            self.executor.cli = discover_cli()
        params = {
            "request_id": key,
            "profile_digest": digest(self.executor.approval_configuration()),
        }
        task = self.core.tasks.create(
            "Codex App Server diagnostics",
            executor=self.executor.name,
            description="Metadata only: no threads, turns, tools or automatic retry",
            idempotency_key="codex-diag-task:" + key,
        )
        if task.status == "draft":
            self.core.tasks.transition(task.id, "planned")
        run = self.core.runs.create(
            task.id,
            action=Action.build("codex.diagnose", params),
            idempotency_key="codex-diag-run:" + key,
        )
        return self.core.runs.execute(run.id)

    def reconcile_closed(self, run_id):
        """Explicit trusted operation, never called on startup/poll/reconnect."""
        run = self.core.runs.get(identity(run_id))
        verifier = self.core.runs.reconcilers["codex_metadata"]
        verifier.verify(run)
        self.core.runs.resolve_interrupted(run.id, confirm_stopped=True)
        return self.core.runs.reconcile(run.id, verifier="codex_metadata")

    def state(self):
        rows = self.core.store.all(
            "SELECT id,status,error_code,effect_status,execution_stopped,reconciliation_json FROM runs WHERE executor='codex_diagnostics' ORDER BY rowid DESC LIMIT 1"
        )
        if not rows:
            return {
                "cli_installed": self.executor.cli is not None
                or shutil.which("codex") is not None,
                "cli_version": self.executor.cli.version if self.executor.cli else None,
                "connection": "not_checked",
                "auth": "not_checked",
                "models": [],
                "last_error": None,
                "agent_sessions": 0,
                "turns": 0,
            }
        run = rows[0]
        if run["status"] == "succeeded":
            self.core.artifacts.validate_run(run["id"], require_any=True)
            item = self.core.artifacts.list(run["id"])[0]
            result = json.loads(self.core.artifacts.read(item.id))
        else:
            saved = self.core.store.all(
                "SELECT payload_json FROM events WHERE run_id=? AND kind='codex.diagnostic_observation' ORDER BY id DESC LIMIT 1",
                (run["id"],),
            )
            result = (
                json.loads(saved[0][0])["observation"]
                if saved
                else {
                    "cli_installed": self.executor.cli is not None
                    or shutil.which("codex") is not None,
                    "connection": "error"
                    if run["effect_status"] == "not_started"
                    else "unconfirmed",
                    "auth": "not_checked",
                    "models": [],
                    "last_error": run["error_code"],
                    "stop_confirmed": bool(run["execution_stopped"]),
                }
            )
        if (
            run["reconciliation_json"]
            and run["execution_stopped"]
            and not result.get("stop_confirmed")
        ):
            receipt = json.loads(run["reconciliation_json"])
            if receipt.get("verifier") == "codex_metadata":
                proof = self.core.runs.reconcilers["codex_metadata"].verify(
                    self.core.runs.get(run["id"])
                )
                result = {**proof, "connection": "closed", "stop_reconciled": True}
        return {
            **result,
            "run_status": run["status"],
            "run_id": run["id"],
            "historical": True,
        }


class MetadataReconciler(Reconciler):
    name = "codex_metadata"

    def __init__(self):
        self.core = None

    def verify(self, run):
        if (
            run.executor != "codex_diagnostics"
            or Action.from_json(run.action_json).kind != "codex.diagnose"
        ):
            raise RecoveryRequired("Only a metadata diagnostic can use this verifier")
        rows = self.core.store.all(
            "SELECT payload_json FROM events WHERE run_id=? AND kind='codex.diagnostic_observation' ORDER BY id DESC LIMIT 1",
            (run.id,),
        )
        observation = json.loads(rows[0][0])["observation"] if rows else {}
        if observation and observation.get("stop_confirmed") is not True:
            proofs = self.core.store.all(
                "SELECT payload_json FROM events WHERE run_id=? AND kind='codex.owned_process_exit' ORDER BY id DESC LIMIT 1",
                (run.id,),
            )
            proof = json.loads(proofs[0][0]) if proofs else {}
            if (
                proof.get("observation_digest") == digest(observation)
                and type(proof.get("process_exit_code")) is int
            ):
                observation = {
                    **observation,
                    "stop_confirmed": True,
                    "process_exit_code": proof["process_exit_code"],
                    "late_exit_proof": digest(proof),
                }
        if (
            observation.get("run_id") != run.id
            or observation.get("stop_confirmed") is not True
            or type(observation.get("process_exit_code")) is not int
            or observation.get("unexpected_agent_activity") is not False
            or observation.get("agent_sessions") != 0
            or observation.get("turns") != 0
            or not observation.get("trace")
            or any(
                frame.get("method")
                not in DIAGNOSTIC_METHODS
                | PERMISSION_METHODS
                | {"authority_declined", "authority_rejected"}
                for frame in observation["trace"]
            )
        ):
            raise RecoveryRequired(
                "No authoritative stopped metadata-only receipt; keep unknown"
            )
        return observation

    def reconcile(self, run):
        observation = self.verify(run)
        # No agent/user-project operation was applied. Diagnostic/cache/journal
        # observations are preserved; this does not mean the RPC never started.
        return ReconciliationReceipt(
            run.task_id,
            run.id,
            run.action_hash,
            EffectStatus.NOT_APPLIED,
            "codex-metadata-no-agent-effects:" + digest(observation),
        )
