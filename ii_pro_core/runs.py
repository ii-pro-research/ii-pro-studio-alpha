from __future__ import annotations

import re
from dataclasses import asdict, replace

from .approvals import ApprovalManager
from .artifacts import ArtifactManager
from .errors import (
    ApprovalRequired,
    Conflict,
    CoreBusy,
    InvalidTransition,
    RecoveryRequired,
    ValidationError,
)
from .executors import (
    CancellationToken,
    ExecutionRequest,
    ExecutionResult,
    Executor,
    UncertainExecution,
)
from .memory import validate_review_memory
from .models import EffectStatus as E
from .models import Run, run_from
from .models import RunStatus as R
from .models import TaskStatus as T
from .reconciliation import Reconciler, ReconciliationReceipt
from .security import Action, canonical, public_text
from .storage import ActiveExecution, Store, new_id, utc_now
from .tasks import TaskManager


class RunManager:
    def __init__(self, store: Store, tasks: TaskManager, artifacts: ArtifactManager,
                 approvals: ApprovalManager, executors: dict[str, Executor],
                 reconcilers: dict[str, Reconciler]):
        self.store, self.tasks = store, tasks
        self.artifacts, self.approvals, self.executors = artifacts, approvals, executors
        self.reconcilers = reconcilers
        self._recovery_done = False

    def get(self, run_id: str, *, conn=None) -> Run:
        return run_from(self.store.one("SELECT * FROM runs WHERE id=?", (run_id,), conn=conn))

    def list(self, task_id: str) -> list[Run]:
        self.tasks.get(task_id)
        return [run_from(row) for row in self.store.all(
            "SELECT * FROM runs WHERE task_id=? ORDER BY attempt", (task_id,))]

    def create(self, task_id: str, *, action: Action | None = None, idempotency_key: str) -> Run:
        action = Action.build() if action is None else Action.from_json(action.json)
        public_text(idempotency_key, limit=200, empty=False)
        with self.store.transaction(admit=True) as conn:
            task = self.tasks.get(task_id, conn=conn)
            old = conn.execute("SELECT * FROM runs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if old:
                if (old["task_id"], old["action_hash"], old["executor"], old["title"], old["description"]) != (
                        task.id, action.hash, task.executor, task.title, task.description):
                    raise Conflict("Idempotency key was used for a different run request")
                return run_from(old)
            if conn.execute("SELECT 1 FROM runs WHERE task_id=? AND status='unknown'", (task_id,)).fetchone():
                raise RecoveryRequired("The interrupted run must be reconciled before retry")
            if conn.execute(
                    "SELECT 1 FROM runs WHERE task_id=? AND (effect_status IN ('unknown','partial') OR (status='failed' AND effect_status='applied'))",
                    (task_id,)).fetchone():
                raise RecoveryRequired("An unresolved or already applied effect prevents retry")
            if task.status != T.PLANNED:
                raise InvalidTransition("A new run requires a planned task")
            if action.kind in {"local.review", "school.propose"}:
                validate_review_memory(conn, action)
            if conn.execute("SELECT 1 FROM runs WHERE task_id=? AND status IN ('created','running')",
                            (task_id,)).fetchone():
                raise Conflict("This task already has an active run")
            attempt = conn.execute("SELECT COALESCE(MAX(attempt),0)+1 FROM runs WHERE task_id=?",
                                   (task_id,)).fetchone()[0]
            run_id, now = new_id(), utc_now()
            conn.execute("""INSERT INTO runs(id,task_id,attempt,executor,status,idempotency_key,
                         action_json,action_hash,title,description,created_at,updated_at,effect_status,execution_stopped)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (run_id, task.id, attempt, task.executor, R.CREATED, idempotency_key,
                          action.json, action.hash, task.title, task.description, now, now, E.NOT_STARTED, 1))
            self.store.event(conn, "run.created", task_id=task.id, run_id=run_id,
                             attempt=attempt, action_hash=action.hash, executor=task.executor)
            return self.get(run_id, conn=conn)

    def execute(self, run_id: str, *, approval_id: str | None = None) -> Run:
        with self.store.execution(run_id) as handle:
            try:
                return self._execute(run_id, approval_id=approval_id, handle=handle)
            except ApprovalRequired:
                self.approvals.expire_for_run(run_id)
                raise

    def _execute(self, run_id: str, *, approval_id: str | None, handle: ActiveExecution) -> Run:
        blocked = None
        operation_spec = None
        with self.store.transaction() as conn:
            row = self.store.one("SELECT * FROM runs WHERE id=?", (run_id,), conn=conn)
            if row["status"] in {R.SUCCEEDED, R.FAILED, R.CANCELLED}:
                return run_from(row)  # replay returns the same result, never a new attempt
            if row["status"] == R.UNKNOWN:
                raise RecoveryRequired("Interrupted execution must be reconciled")
            if row["status"] != R.CREATED:
                raise CoreBusy("Run already has an executing owner")
            if conn.execute(
                    "SELECT 1 FROM runs WHERE task_id=? AND id<>? AND (effect_status IN ('unknown','partial') OR (status='failed' AND effect_status='applied'))",
                    (row["task_id"], run_id)).fetchone():
                raise RecoveryRequired("A previous attempt's effect prevents dispatch")
            if conn.execute("SELECT 1 FROM runs WHERE status='running'").fetchone():
                raise CoreBusy("v0.1 permits only one executing run")
            action = Action.from_json(row["action_json"])
            if action.hash != row["action_hash"]:
                raise ValidationError("Action fingerprint does not match")
            executor = self.executors.get(row["executor"])
            if executor is None:
                raise ValidationError("Executor is unavailable; database values cannot load code")
            bound_methods = {name: getattr(executor, name) for name in ("prepare", "execute", "approval_configuration")}
            self.store.enable_cancellation(handle, executor.supports_cancellation)
            request = ExecutionRequest(row["task_id"], run_id, row["title"], row["description"], action,
                                       CancellationToken(handle.cancel) if handle.cancellable else None)
            if self.tasks.get(row["task_id"], conn=conn).status not in {T.PLANNED, T.AWAITING_APPROVAL}:
                raise InvalidTransition("Task is not ready for execution")
            if action.kind in {"local.review", "school.propose"}:
                validate_review_memory(conn, action)
            blocked = self.approvals._consume(conn, row, action, approval_id, bound_methods)
            if blocked:
                self.store.event(conn, "run.blocked", task_id=row["task_id"], run_id=run_id, reason=blocked)
            else:
                if self.approvals.needs_approval(conn, row, action):
                    operation_spec = self.approvals.spec_for(approval_id, conn=conn)
                    request = replace(request, operation_spec=operation_spec)
                # A trusted readiness check runs before execute, never the task itself.
                # Failure here is the only ordinary retryable executor failure.
                try:
                    bound_methods["prepare"](request)
                except Exception as exc:
                    now = utc_now()
                    error_type = self._safe_error_type(exc)
                    conn.execute("UPDATE runs SET status=?,error_code=?,error_type=?,finished_at=?,updated_at=?,effect_status=?,execution_stopped=1 WHERE id=?",
                                 (R.FAILED, "executor_not_started", error_type, now, now, E.NOT_STARTED, run_id))
                    self.tasks._transition(conn, row["task_id"], T.FAILED)
                    self.store.event(conn, "run.failed", task_id=row["task_id"], run_id=run_id,
                                     error_code="executor_not_started", error_type=error_type)
                    return self.get(run_id, conn=conn)
                if operation_spec is not None and not self.approvals.matches(row, operation_spec):
                    # Roll back consumption together with its journal entry.
                    # prepare is contractually side-effect-free; execute was not called.
                    raise ApprovalRequired("approval_scope_changed_during_preparation")
                if operation_spec is not None and not self.approvals.consumed_permit_valid(conn, approval_id, operation_spec):
                    raise ApprovalRequired("approval_expired_during_preparation")
                now = utc_now()
                conn.execute("UPDATE runs SET status=?,started_at=?,updated_at=?,effect_status=?,execution_stopped=0,owner_generation=? WHERE id=?",
                             (R.RUNNING, now, now, E.UNKNOWN, self.store.generation, run_id))
                self.tasks._transition(conn, row["task_id"], T.RUNNING)
                self.store.event(conn, "run.started", task_id=row["task_id"], run_id=run_id)
        if blocked:
            raise ApprovalRequired(blocked)
        if operation_spec is not None:
            with self.store.transaction() as conn:
                current = self.store.one("SELECT * FROM runs WHERE id=?", (run_id,), conn=conn)
                self._check_claim(current)
                if not self.approvals.matches(current, operation_spec):
                    return self._record_uncertain(conn, run_id, "execution_spec_changed", "ApprovalRequired")
                if not self.approvals.consumed_permit_valid(conn, approval_id, operation_spec):
                    return self._record_uncertain(conn, run_id, "approval_no_longer_valid", "ApprovalRequired")
        try:
            result = bound_methods["execute"](request)
        except UncertainExecution as exc:
            with self.store.transaction() as conn:
                current = self.store.one("SELECT * FROM runs WHERE id=?", (run_id,), conn=conn)
                self._check_claim(current)
                try:
                    self._seal_blobs(conn, current, ExecutionResult(exc.artifacts, storage="sqlite"))
                except Exception as error:
                    self.store.event(conn, "run.diagnostic_unavailable", task_id=current["task_id"], run_id=run_id,
                                     error_type=self._safe_error_type(error))
                return self._record_uncertain(conn, run_id, "executor_failed", self._safe_error_type(exc))
        except Exception as exc:
            return self._uncertain(run_id, "executor_failed", self._safe_error_type(exc))
        # BaseException (e.g. KeyboardInterrupt) intentionally leaves durable RUNNING.
        # An I/O/transaction failure here stays unresolved. Files may exist without
        # records; do not replay potentially completed external work automatically.
        with self.store.transaction() as conn:
            current = self.store.one("SELECT * FROM runs WHERE id=?", (run_id,), conn=conn)
            self._check_claim(current)
            # Hold BEGIN IMMEDIATE through import and result commit. A stale
            # generation cannot publish artifacts before its result is rejected.
            try:
                if isinstance(result, ExecutionResult) and result.storage == "sqlite":
                    self._seal_blobs(conn, current, result)
                    names = []
                else:
                    names = self.artifacts.write_outputs(current, result)
            except Exception as exc:
                return self._record_uncertain(conn, run_id, "artifact_failed", self._safe_error_type(exc))
            for name in names:
                self.artifacts._register(conn, current, name)
            now = utc_now()
            conn.execute("UPDATE runs SET status=?,updated_at=?,finished_at=?,effect_status=?,execution_stopped=1 WHERE id=?",
                         (R.SUCCEEDED, now, now, E.APPLIED, run_id))
            self.tasks._transition(conn, current["task_id"], T.VERIFYING)
            self.store.event(conn, "run.succeeded", task_id=current["task_id"], run_id=run_id)
            return self.get(run_id, conn=conn)

    def _seal_blobs(self, conn, run, result):
        conn.execute("SAVEPOINT run_artifacts")
        try:
            self.artifacts.import_blobs(conn, run, result)
        except BaseException:
            conn.execute("ROLLBACK TO run_artifacts")
            raise
        finally:
            conn.execute("RELEASE run_artifacts")

    @staticmethod
    def _safe_error_type(exc):
        name = type(exc).__name__
        return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name) else "ExecutorError"

    def _uncertain(self, run_id: str, code: str, error_type: str) -> Run:
        with self.store.transaction() as conn:
            return self._record_uncertain(conn, run_id, code, error_type)

    def _check_claim(self, run):
        if run["status"] != R.RUNNING or run["owner_generation"] != self.store.generation:
            raise Conflict("Run lost ownership before completion")

    def _record_uncertain(self, conn, run_id: str, code: str, error_type: str) -> Run:
        # Exception messages may contain credentials. Store type + stable code only.
        safe_type = error_type if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", error_type) else "ExecutorError"
        row = self.store.one("SELECT * FROM runs WHERE id=?", (run_id,), conn=conn)
        self._check_claim(row)
        now = utc_now()
        conn.execute("UPDATE runs SET status=?,error_code=?,error_type=?,finished_at=?,updated_at=?,effect_status=?,execution_stopped=0 WHERE id=?",
                     (R.UNKNOWN, code, safe_type, now, now, E.UNKNOWN, run_id))
        self.tasks._transition(conn, row["task_id"], T.FAILED)
        self.store.event(conn, "run.uncertain", task_id=row["task_id"], run_id=run_id,
                         error_code=code, error_type=safe_type)
        return self.get(run_id, conn=conn)

    def recover(self) -> list[str]:
        """Called only after acquiring the exclusive process lock; never redispatch."""
        if self._recovery_done:
            raise CoreBusy("Recovery is startup-only; active invocations cannot be recovered")
        self._recovery_done = True
        recovered = []
        with self.store.transaction() as conn:
            for row in conn.execute("SELECT * FROM runs WHERE status='running'").fetchall():
                now = utc_now()
                conn.execute("UPDATE runs SET status=?,error_code=?,error_type=?,updated_at=?,effect_status=?,execution_stopped=0 WHERE id=?",
                             (R.UNKNOWN, "execution_interrupted", "Interrupted", now, E.UNKNOWN, row["id"]))
                self.tasks._transition(conn, row["task_id"], T.FAILED)
                self.store.event(conn, "run.interrupted", task_id=row["task_id"], run_id=row["id"],
                                 previous=R.RUNNING, current=R.UNKNOWN)
                recovered.append(row["id"])
        return recovered

    def resolve_interrupted(self, run_id: str, *, confirm_stopped: bool = False) -> Run:
        """Record an operator's stop observation, never infer absence of effects.

        Call reconcile with a registered domain verifier before requesting retry.
        This method does not inspect/stop operating-system processes.
        """
        if confirm_stopped is not True:
            raise RecoveryRequired("Explicit reconciliation that execution stopped is required")
        with self.store.transaction() as conn:
            run = self.get(run_id, conn=conn)
            if run.status not in {R.UNKNOWN, R.FAILED, R.SUCCEEDED} or run.effect_status not in {E.UNKNOWN, E.PARTIAL}:
                raise Conflict("Only an interrupted run needs reconciliation")
            now = utc_now()
            conn.execute("UPDATE runs SET execution_stopped=1,updated_at=? WHERE id=?", (now, run_id))
            self.store.event(conn, "run.stop_confirmed", task_id=run.task_id, run_id=run_id)
            return self.get(run_id, conn=conn)

    def reconcile(self, run_id: str, *, verifier: str) -> Run:
        """Resolve through trusted evidence; caller cannot supply an outcome flag.

        A verifier must establish authoritative domain evidence. A bare reference
        or a statement that a process stopped is not automatically such evidence.
        """
        reconciler = self.reconcilers.get(verifier)
        if reconciler is None:
            raise RecoveryRequired("A registered trusted domain verifier is required")
        run = self.get(run_id)
        if (run.status not in {R.UNKNOWN, R.FAILED, R.SUCCEEDED}
                or run.effect_status not in {E.UNKNOWN, E.PARTIAL} or not run.execution_stopped):
            raise RecoveryRequired("Confirm stopped execution before verifying its effect")
        receipt = reconciler.reconcile(run)
        if (not isinstance(receipt, ReconciliationReceipt)
                or (receipt.task_id, receipt.run_id, receipt.action_hash) != (run.task_id, run.id, run.action_hash)):
            raise ValidationError("Evidence does not belong to this run/action")
        try:
            outcome = E(receipt.outcome)
        except (ValueError, TypeError):
            raise ValidationError("Invalid effect outcome") from None
        if outcome == E.NOT_STARTED:
            raise ValidationError("A dispatched operation cannot become never-started")
        public_text(receipt.evidence_ref, limit=1000, empty=False)
        record = {**asdict(receipt), "verifier": verifier,
                  "verifier_class": f"{type(reconciler).__module__}.{type(reconciler).__qualname__}"}
        with self.store.transaction() as conn:
            if self.get(run_id, conn=conn) != run:
                raise Conflict("Run changed during evidence verification")
            status = R.FAILED if run.status == R.UNKNOWN and outcome in {E.NOT_APPLIED, E.APPLIED} else run.status
            now = utc_now()
            conn.execute("UPDATE runs SET status=?,effect_status=?,reconciliation_json=?,updated_at=?,finished_at=COALESCE(finished_at,?) WHERE id=?",
                         (status, outcome, canonical(record), now, now, run_id))
            self.store.event(conn, "run.reconciled", task_id=run.task_id, run_id=run_id,
                             receipt=record)
            return self.get(run_id, conn=conn)

    def cancel(self, run_id: str) -> Run:
        with self.store.transaction() as conn:
            run = self.get(run_id, conn=conn)
            if run.status == R.CANCELLED:
                return run
            if run.status != R.CREATED:
                raise Conflict("Only a not-yet-started run can be cancelled synchronously")
            now = utc_now()
            conn.execute("UPDATE runs SET status=?,finished_at=?,updated_at=?,effect_status=?,execution_stopped=1 WHERE id=?",
                         (R.CANCELLED, now, now, E.NOT_STARTED, run_id))
            conn.execute("UPDATE approvals SET status='rejected',decided_at=? WHERE run_id=? AND status IN ('pending','approved')",
                         (now, run_id))
            self.tasks._transition(conn, run.task_id, T.CANCELLED)
            self.store.event(conn, "run.cancelled", task_id=run.task_id, run_id=run_id)
            return self.get(run_id, conn=conn)
