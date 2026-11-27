from __future__ import annotations

from .errors import Conflict, InvalidTransition, RecoveryRequired, ValidationError
from .models import Task, TaskStatus as T, task_from
from .security import digest, public_text
from .storage import Store, new_id, utc_now

TRANSITIONS = {
    T.DRAFT: {T.PLANNED, T.CANCELLED},
    T.PLANNED: {T.AWAITING_APPROVAL, T.RUNNING, T.FAILED, T.CANCELLED},
    T.AWAITING_APPROVAL: {T.RUNNING, T.FAILED, T.CANCELLED},
    T.RUNNING: {T.VERIFYING, T.FAILED},
    T.VERIFYING: {T.VERIFIED, T.NEEDS_REVISION, T.FAILED, T.CANCELLED},
    T.NEEDS_REVISION: {T.PLANNED, T.CANCELLED},
    T.VERIFIED: {T.SAVED, T.NEEDS_REVISION},
    T.FAILED: {T.PLANNED, T.CANCELLED},
    T.CANCELLED: set(), T.SAVED: set(),
}
_MANAGED = {T.RUNNING, T.AWAITING_APPROVAL, T.VERIFYING, T.FAILED}


class TaskManager:
    def __init__(self, store: Store, executor_names: set[str]):
        self.store = store
        self.executor_names = executor_names
        self.verify_artifacts = None  # wired by Core, no dependency on an executor

    def create(self, title: str, *, description: str = "", executor: str = "mock",
               idempotency_key: str) -> Task:
        public_text(title, limit=500, empty=False)
        public_text(description)
        public_text(idempotency_key, limit=200, empty=False)
        if executor not in self.executor_names:
            raise ValidationError("Executor is not registered")
        request_hash = digest({"title": title, "description": description, "executor": executor})
        with self.store.transaction(admit=True) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row:
                if row["request_hash"] != request_hash:
                    raise Conflict("Idempotency key was used for a different task request")
                return task_from(row)
            task_id, now = new_id(), utc_now()
            conn.execute("""INSERT INTO tasks(id,title,description,executor,status,created_at,updated_at,
                         idempotency_key,request_hash) VALUES(?,?,?,?,?,?,?,?,?)""",
                         (task_id, title, description, executor, T.DRAFT, now, now, idempotency_key, request_hash))
            self.store.event(conn, "task.created", task_id=task_id, executor=executor)
            return self.get(task_id, conn=conn)

    def get(self, task_id: str, *, conn=None) -> Task:
        return task_from(self.store.one("SELECT * FROM tasks WHERE id=?", (task_id,), conn=conn))

    def update(self, task_id: str, *, expected_version: int, title: str | None = None,
               description: str | None = None, executor: str | None = None) -> Task:
        with self.store.transaction() as conn:
            task = self.get(task_id, conn=conn)
            if task.version != expected_version:
                raise Conflict("Task was changed by another operation")
            if task.status not in {T.DRAFT, T.PLANNED, T.NEEDS_REVISION, T.FAILED}:
                raise InvalidTransition("Task is not editable in this state")
            if conn.execute("SELECT 1 FROM runs WHERE task_id=? AND status IN ('created','running','unknown')",
                            (task_id,)).fetchone():
                raise Conflict("An active run already holds an immutable task snapshot")
            selected = task.executor if executor is None else executor
            if selected not in self.executor_names:
                raise ValidationError("Executor is not registered")
            new_title = task.title if title is None else public_text(title, limit=500, empty=False)
            new_description = task.description if description is None else public_text(description)
            conn.execute("UPDATE tasks SET title=?,description=?,executor=?,version=version+1,updated_at=? WHERE id=?",
                         (new_title, new_description, selected, utc_now(), task_id))
            self.store.event(conn, "task.updated", task_id=task_id, version=task.version + 1)
            return self.get(task_id, conn=conn)

    def _transition(self, conn, task_id: str, target: T) -> Task:
        task = self.get(task_id, conn=conn)
        if target not in TRANSITIONS[task.status]:
            raise InvalidTransition(f"Cannot transition {task.status} to {target}")
        conn.execute("UPDATE tasks SET status=?,version=version+1,updated_at=? WHERE id=?",
                     (target, utc_now(), task_id))
        self.store.event(conn, "task.transitioned", task_id=task_id,
                         previous=str(task.status), current=str(target))
        return self.get(task_id, conn=conn)

    def transition(self, task_id: str, target: T | str, *, expected_version: int | None = None) -> Task:
        try:
            target = T(target)
        except ValueError:
            raise InvalidTransition("Unknown task state") from None
        if target in _MANAGED:
            raise InvalidTransition("This state is managed by execution or approval")
        with self.store.transaction() as conn:
            task = self.get(task_id, conn=conn)
            if expected_version is not None and task.version != expected_version:
                raise Conflict("Task version does not match")
            if target not in TRANSITIONS[task.status]:
                raise InvalidTransition(f"Cannot transition {task.status} to {target}")
            if conn.execute("SELECT 1 FROM runs WHERE task_id=? AND status='unknown'", (task_id,)).fetchone():
                raise RecoveryRequired("Reconcile the interrupted run before changing the task")
            if target == T.PLANNED and conn.execute(
                    "SELECT 1 FROM runs WHERE task_id=? AND (effect_status IN ('unknown','partial') OR (status='failed' AND effect_status='applied'))",
                    (task_id,)).fetchone():
                raise RecoveryRequired("An unresolved or already applied effect prevents retry")
            if conn.execute("SELECT 1 FROM runs WHERE task_id=? AND status IN ('created','running')",
                            (task_id,)).fetchone():
                raise Conflict("Resolve the active run before changing task state")
            if target in {T.VERIFIED, T.SAVED}:
                run = self.store.one("SELECT * FROM runs WHERE task_id=? ORDER BY attempt DESC LIMIT 1",
                                     (task_id,), conn=conn)
                if run["status"] != "succeeded":
                    raise InvalidTransition("The latest run did not succeed")
                if run["effect_status"] != "applied":
                    raise RecoveryRequired("Legacy result effect requires explicit verification")
                self.verify_artifacts(run["id"], require_any=True, conn=conn)
            return self._transition(conn, task_id, target)
