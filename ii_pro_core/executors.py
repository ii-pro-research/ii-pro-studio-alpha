from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from threading import Event

from .errors import ValidationError
from .operations import OperationSpec
from .security import Action, canonical


class CancellationToken:
    """Cooperative signal only: observing it does not prove absence of effects."""

    def __init__(self, signal: Event):
        self._signal = signal

    @property
    def requested(self) -> bool:
        return self._signal.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._signal.wait(timeout)


@dataclass(frozen=True)
class ExecutionRequest:
    task_id: str
    run_id: str
    title: str
    description: str
    action: Action
    cancellation: CancellationToken | None = None
    operation_spec: OperationSpec | None = None


@dataclass(frozen=True)
class ProducedArtifact:
    relative_path: str
    content: bytes


@dataclass(frozen=True)
class ExecutionResult:
    artifacts: tuple[ProducedArtifact, ...]
    storage: str = "files"


class UncertainExecution(RuntimeError):
    """A trusted executor's sealed diagnostic evidence, without an effect claim."""

    def __init__(self, artifacts: tuple[ProducedArtifact, ...]):
        super().__init__("Execution result could not be confirmed")
        self.artifacts = artifacts


class Executor(ABC):
    """Trusted in-process extension. No database-driven loading or shell commands.

    v0.1 is synchronous. An untrusted executor requires process/OS isolation;
    Python interfaces alone are not a filesystem or network sandbox.
    """
    name: str
    supports_cancellation: bool = False
    revision: str | None = None

    def approval_configuration(self) -> dict:
        """All material public settings, without secrets, telemetry or effects.

        Protected extensions must override this and honor the frozen spec.
        """
        raise ValidationError("Executor has no versioned approval configuration")

    def prepare(self, request: ExecutionRequest) -> None:
        """Trusted readiness check; must not perform the task or business effects.

        If this raises, Core has not called execute and may record not_started.
        Once execute is called, exceptions cannot prove absence of an effect.
        """

    @abstractmethod
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        raise NotImplementedError


class SimulatedInterruption(BaseException):
    """Test-only interruption, intentionally not handled as a normal failure."""


class MockExecutor(Executor):
    name = "mock"
    revision = "mock-v1"

    def approval_configuration(self):
        return {"failures": self.failures, "interrupt": self.interrupt}

    def __init__(self, *, failures: int = 0, interrupt: bool = False):
        self.failures = failures
        self.interrupt = interrupt
        self.calls = 0

    def prepare(self, request: ExecutionRequest) -> None:
        if self.failures > 0:
            self.calls += 1
            self.failures -= 1
            raise RuntimeError("Simulated executor failure")

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls += 1
        interrupt = request.operation_spec.executor_configuration["interrupt"] if request.operation_spec else self.interrupt
        if interrupt:
            raise SimulatedInterruption()
        result = {"task_id": request.task_id, "run_id": request.run_id,
                  "action": request.action.kind, "text": json.loads(request.action.parameters_json)["text"]}
        return ExecutionResult((ProducedArtifact("result.json", (canonical(result) + "\n").encode()),))
