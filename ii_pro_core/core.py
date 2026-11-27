from __future__ import annotations

import re
from pathlib import Path
from types import MappingProxyType

from .approvals import ApprovalManager
from .artifacts import ArtifactManager
from .errors import ValidationError
from .executors import Executor, MockExecutor
from .runs import RunManager
from .storage import Store
from .tasks import TaskManager
from .reconciliation import Reconciler
from .operations import ExecutionPolicy


class Core:
    def __init__(self, state_dir: str | Path, *, executors: list[Executor] | None = None,
                 reconcilers: list[Reconciler] | None = None, shutdown_timeout: float = 1.0,
                 policy: ExecutionPolicy | None = None):
        if policy is not None and type(policy) is not ExecutionPolicy:
            raise ValidationError("A frozen ExecutionPolicy is required")
        selected = [MockExecutor()] if executors is None else list(executors)
        registry = {}
        for executor in selected:
            if (not isinstance(executor, Executor) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", executor.name)
                    or executor.name in registry or not isinstance(executor.supports_cancellation, bool)):
                raise ValidationError("Invalid or duplicate trusted executor registration")
            registry[executor.name] = executor
        verifiers = {}
        for verifier in reconcilers or []:
            if (not isinstance(verifier, Reconciler) or not isinstance(verifier.name, str)
                    or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", verifier.name)
                    or verifier.name in verifiers):
                raise ValidationError("Invalid trusted reconciler registration")
            verifiers[verifier.name] = verifier
        self.store = Store(state_dir, shutdown_timeout=shutdown_timeout)
        try:
            from .memory import EngineeringMemory
            self.memory = EngineeringMemory(self.store)
            self.tasks = TaskManager(self.store, set(registry))
            self.artifacts = ArtifactManager(self.store)
            self.tasks.verify_artifacts = self.artifacts.validate_run
            bindings = MappingProxyType(registry)
            self.approvals = ApprovalManager(self.store, self.tasks, bindings, policy or ExecutionPolicy())
            self.runs = RunManager(self.store, self.tasks, self.artifacts, self.approvals, bindings, verifiers)
            self.recovered_run_ids = self.runs.recover()
        except BaseException:
            self.close()
            raise

    def events(self, task_id: str | None = None):
        return self.store.events(task_id)

    def backup(self):
        return self.store.backup()

    @property
    def state(self):
        return self.store.state

    @property
    def active_run_ids(self):
        return self.store.active_run_ids

    def close(self, *, timeout: float | None = None, cancel: bool = False):
        self.store.close(timeout=timeout, cancel=cancel)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
