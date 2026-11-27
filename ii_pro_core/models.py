from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    DRAFT = "draft"
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    VERIFYING = "verifying"
    NEEDS_REVISION = "needs_revision"
    VERIFIED = "verified"
    SAVED = "saved"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    REVOKED = "revoked"
    INVALIDATED = "invalidated"


class EffectStatus(StrEnum):
    NOT_STARTED = "not_started"
    UNKNOWN = "unknown"
    NOT_APPLIED = "proven_not_applied"
    APPLIED = "applied"
    PARTIAL = "partial"


class CoreState(StrEnum):
    RUNNING = "running"
    STOPPING = "stopping"
    CLOSED = "closed"


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    description: str
    executor: str
    status: TaskStatus
    version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Run:
    id: str
    task_id: str
    attempt: int
    executor: str
    status: RunStatus
    idempotency_key: str
    action_json: str
    action_hash: str
    title: str
    description: str
    error_code: str | None
    error_type: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    effect_status: EffectStatus
    execution_stopped: int
    reconciliation_json: str | None
    owner_generation: int | None


@dataclass(frozen=True)
class Artifact:
    id: str
    task_id: str
    run_id: str
    relative_path: str
    sha256: str
    size: int
    created_at: str


@dataclass(frozen=True)
class Approval:
    id: str
    task_id: str
    run_id: str
    action_hash: str
    status: ApprovalStatus
    created_at: str
    expires_at: str
    decided_at: str | None
    consumed_at: str | None
    spec_digest: str | None
    operation_json: str | None
    revoked_at: str | None


@dataclass(frozen=True)
class Event:
    id: int
    task_id: str | None
    run_id: str | None
    kind: str
    payload: dict[str, Any]
    created_at: str


def task_from(row) -> Task:
    values = {k: row[k] for k in Task.__dataclass_fields__}
    values["status"] = TaskStatus(values["status"])
    return Task(**values)


def run_from(row) -> Run:
    values = {k: row[k] for k in Run.__dataclass_fields__}
    values["status"] = RunStatus(values["status"])
    values["effect_status"] = EffectStatus(values["effect_status"])
    return Run(**values)


def artifact_from(row) -> Artifact:
    return Artifact(**{k: row[k] for k in Artifact.__dataclass_fields__})


def approval_from(row) -> Approval:
    values = {k: row[k] for k in Approval.__dataclass_fields__}
    values["status"] = ApprovalStatus(values["status"])
    return Approval(**values)
