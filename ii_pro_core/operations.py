"""Versioned immutable consent scope for trusted in-process executors.

Extensions must declare all material public settings, version dependencies,
and execute frozen request configuration. This is not an OS sandbox.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from types import CodeType

from .errors import ValidationError
from .security import Action, canonical, digest, public_text


@dataclass(frozen=True)
class ExecutionPolicy:
    revision: str = "core-policy-v1"
    permissions_revision: str = "trusted-local-v1"
    approval_actions: tuple[str, ...] = ("mock.protected_echo",)

    def __post_init__(self):
        public_text(self.revision, limit=100, empty=False)
        public_text(self.permissions_revision, limit=100, empty=False)
        if (type(self.approval_actions) is not tuple
                or any(a not in {"mock.echo", "mock.protected_echo", "school.propose", "worker.fixed", "team.turn", "codex.diagnose", "local.review", "engineering.apply", "engineering.delete", "engineering.rollback"} for a in self.approval_actions)
                or "mock.protected_echo" not in self.approval_actions):
            raise ValidationError("Policy cannot remove mandatory approvals or allow unknown actions")
        object.__setattr__(self, "approval_actions", tuple(sorted(set(self.approval_actions))))

    def requires_approval(self, action: Action) -> bool:
        return action.requires_approval or action.kind in self.approval_actions

    def describe(self):
        return {"revision": self.revision, "permissions_revision": self.permissions_revision,
                "approval_actions": self.approval_actions,
                "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "action_schema_sha256": hashlib.sha256(Path(inspect.getsourcefile(Action)).read_bytes()).hexdigest()}


def _code_identity(code: CodeType):
    def const(value):
        if isinstance(value, CodeType):
            return _code_identity(value)
        if isinstance(value, bytes):
            return {"bytes": value.hex()}
        if isinstance(value, (tuple, frozenset)):
            values = [const(v) for v in value]
            return sorted(values, key=canonical) if isinstance(value, frozenset) else values
        if value is Ellipsis:
            return {"ellipsis": True}
        if value is None or type(value) in (str, int, float, bool):
            return value
        raise ValidationError("Unsupported executor code constant")
    return {"bytecode": code.co_code.hex(), "constants": [const(c) for c in code.co_consts],
            "names": code.co_names, "locals": code.co_varnames, "freevars": code.co_freevars,
            "cellvars": code.co_cellvars, "flags": code.co_flags,
            "argcount": code.co_argcount, "kwonly": code.co_kwonlyargcount, "posonly": code.co_posonlyargcount,
            "exception_table": code.co_exceptiontable.hex()}


def executor_profile(executor, bound_methods=None):
    bound_methods = bound_methods or {}
    if "revision" not in type(executor).__dict__ or "approval_configuration" not in type(executor).__dict__:
        raise ValidationError("Every concrete protected executor must declare its own revision and full configuration")
    public_text(executor.revision, limit=100, empty=False)
    config = bound_methods.get("approval_configuration", executor.approval_configuration)()
    if not isinstance(config, dict):
        raise ValidationError("Executor must declare a public configuration object")
    config = json.loads(public_text(canonical(config), limit=12000))
    methods = {}
    for name in ("prepare", "execute", "approval_configuration"):
        method = bound_methods.get(name, getattr(executor, name))
        function = getattr(method, "__func__", method)
        if not inspect.isfunction(function):
            raise ValidationError("Opaque executor implementations require a supported adapter")
        methods[name] = _code_identity(function.__code__)
    modules = {}
    for cls in type(executor).__mro__:
        try:
            source = inspect.getsourcefile(cls)
        except TypeError:
            continue
        if source and Path(source).is_file():
            modules[cls.__module__] = hashlib.sha256(Path(source).read_bytes()).hexdigest()
    return {"name": executor.name, "revision": executor.revision,
            "implementation": f"{type(executor).__module__}.{type(executor).__qualname__}",
            "code_sha256": digest({"methods": methods, "modules": modules}),
            "configuration": config, "supports_cancellation": executor.supports_cancellation,
            "runtime": {"python": platform.python_version(), "implementation": sys.implementation.name, "platform": sys.platform}}


@dataclass(frozen=True)
class OperationSpec:
    json: str

    @property
    def digest(self):
        return hashlib.sha256(self.json.encode("utf-8")).hexdigest()

    @property
    def executor_configuration(self):
        return json.loads(self.json)["executor"]["configuration"]

    @classmethod
    def build(cls, run, executor, policy: ExecutionPolicy, bound_methods=None):
        action = Action.from_json(run["action_json"])
        if action.hash != run["action_hash"]:
            raise ValidationError("Action snapshot fingerprint mismatch")
        return cls(canonical({"schema_version": 1, "task_id": run["task_id"], "run_id": run["id"],
                              "attempt": run["attempt"], "idempotency_key": run["idempotency_key"],
                              "title": run["title"], "description": run["description"],
                              "action": json.loads(action.json), "action_hash": action.hash,
                              "executor": executor_profile(executor, bound_methods), "policy": policy.describe()}))
