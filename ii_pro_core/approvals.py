from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from .errors import Conflict, ValidationError
from .models import Approval, ApprovalStatus as A, TaskStatus as T, approval_from
from .operations import ExecutionPolicy, OperationSpec
from .security import Action, public_text
from .storage import Store, new_id, utc_now
from .tasks import TaskManager


class ApprovalManager:
    def __init__(self, store: Store, tasks: TaskManager, executors, policy: ExecutionPolicy):
        self.store, self.tasks = store, tasks
        self.executors, self._policy = executors, policy
        self.session_authorizer = None
        self.session_required = None

    @property
    def policy(self):
        return self._policy

    def get(self, approval_id: str, *, conn=None) -> Approval:
        return approval_from(self.store.one("""SELECT a.*,s.body_json AS operation_json FROM approvals a
            LEFT JOIN operation_specs s ON s.digest=a.spec_digest WHERE a.id=?""", (approval_id,), conn=conn))

    def current_spec(self, run, bound_methods=None) -> OperationSpec:
        executor = self.executors.get(run["executor"])
        if executor is None:
            raise ValidationError("Executor unavailable")
        return OperationSpec.build(run, executor, self.policy, bound_methods)

    def spec_for(self, approval_id, *, conn=None) -> OperationSpec:
        approval = self.get(approval_id, conn=conn)
        if approval.operation_json is None or approval.spec_digest is None:
            raise ValidationError("Legacy approval has no full operation scope")
        spec = OperationSpec(approval.operation_json)
        if spec.digest != approval.spec_digest:
            raise ValidationError("Operation fingerprint mismatch")
        return spec

    def matches(self, run, spec: OperationSpec, bound_methods=None) -> bool:
        try:
            return self.current_spec(run, bound_methods).digest == spec.digest
        except (ValidationError, TypeError, ValueError):
            return False

    def describe(self, approval_id):
        import json
        return json.loads(self.spec_for(approval_id).json)

    def _invalidate(self, conn, row, reason):
        conn.execute("UPDATE approvals SET status=? WHERE id=?", (A.INVALIDATED, row["id"]))
        self.store.event(conn, "approval.invalidated", task_id=row["task_id"], run_id=row["run_id"],
                         approval_id=row["id"], reason=reason)

    def _expire(self, conn, row) -> bool:
        if row["status"] in {A.PENDING, A.APPROVED} and row["expires_at"] <= utc_now():
            conn.execute("UPDATE approvals SET status=? WHERE id=?", (A.EXPIRED, row["id"]))
            self.store.event(conn, "approval.expired", task_id=row["task_id"], run_id=row["run_id"],
                             approval_id=row["id"])
            return True
        return row["status"] == A.EXPIRED

    def needs_approval(self, conn, run, action):
        # A previously gated Run cannot silently become ungated after policy
        # relaxation or a changed action kind. Its next scope needs new consent.
        return self.policy.requires_approval(action) or conn.execute(
            "SELECT 1 FROM approvals WHERE run_id=? LIMIT 1", (run["id"],)).fetchone() is not None

    def consumed_permit_valid(self, conn, approval_id, spec):
        row = self.store.one("SELECT * FROM approvals WHERE id=?", (approval_id,), conn=conn)
        return (row["status"] == A.CONSUMED and row["spec_digest"] == spec.digest and row["expires_at"] > utc_now()
                and self._session_valid(conn, row))

    def _derivation(self, conn, row):
        matches = conn.execute("""SELECT payload_json FROM events WHERE run_id=? AND kind='approval.derived'
                              AND json_extract(payload_json,'$.approval_id')=?""", (row["run_id"], row["id"])).fetchall()
        if len(matches) > 1:
            raise Conflict("Ambiguous session approval provenance")
        return json.loads(matches[0][0]) if matches else None

    def _session_valid(self, conn, approval):
        proof = self._derivation(conn, approval)
        if proof is None:
            return not (self.session_required and self.session_required(approval["run_id"]))
        if self.session_authorizer is None or proof["spec_digest"] != approval["spec_digest"]:
            return False
        run = self.store.one("SELECT * FROM runs WHERE id=?", (approval["run_id"],), conn=conn)
        try:
            return self.session_authorizer(conn, run, proof, reserve=False) > utc_now()
        except (Conflict, ValidationError):
            return False

    def derive_for_session(self, approval_id, *, job, slot, scope_sha256):
        """Trusted Team reservation, independently checked again at Core dispatch."""
        with self.store.transaction(admit=True) as conn:
            row = self.store.one("SELECT * FROM approvals WHERE id=?", (approval_id,), conn=conn)
            run = self.store.one("SELECT * FROM runs WHERE id=?", (row["run_id"],), conn=conn)
            proof = {"approval_id": approval_id, "spec_digest": row["spec_digest"],
                     "job": job, "slot": slot, "scope_sha256": scope_sha256}
            if (self.session_authorizer is None or row["status"] not in {A.PENDING, A.APPROVED}
                    or row["expires_at"] <= utc_now() or run["status"] != "created"
                    or not self.matches(run, self.spec_for(approval_id, conn=conn))):
                raise Conflict("A current exact session Approval is required")
            existing = self._derivation(conn, row)
            if row["status"] == A.APPROVED:
                if existing != proof or not self._session_valid(conn, row):
                    raise Conflict("Approval was not derived from this session")
                return self.get(approval_id, conn=conn)
            until = self.session_authorizer(conn, run, proof, reserve=True)
            if until <= utc_now():
                raise Conflict("Session permission expired")
            conn.execute("UPDATE approvals SET status=?,decided_at=? WHERE id=?",
                         (A.APPROVED, utc_now(), approval_id))
            self.store.event(conn, "approval.derived", task_id=run["task_id"], run_id=run["id"], **proof)
            return self.get(approval_id, conn=conn)

    def expire_for_run(self, run_id):
        # Also used after a rolled-back preparation/claim, so an observed expiry
        # becomes durable without keeping the consumed event of that rollback.
        with self.store.transaction() as conn:
            for row in conn.execute("SELECT * FROM approvals WHERE run_id=? AND status IN ('pending','approved')", (run_id,)).fetchall():
                self._expire(conn, row)

    def request(self, run_id: str, *, ttl_seconds: int = 300) -> Approval:
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
            raise ValidationError("Approval lifetime must be 1..3600 seconds")
        with self.store.transaction(admit=True) as conn:
            run = self.store.one("SELECT * FROM runs WHERE id=?", (run_id,), conn=conn)
            action = Action.from_json(run["action_json"])
            if action.hash != run["action_hash"]:
                raise ValidationError("Action fingerprint does not match")
            if (run["status"] != "created" or not self.needs_approval(conn, run, action)
                    or self.tasks.get(run["task_id"], conn=conn).status not in {T.PLANNED, T.AWAITING_APPROVAL}):
                raise Conflict("This run does not need a pending approval")
            spec = self.current_spec(run)
            for previous in conn.execute("SELECT * FROM approvals WHERE run_id=? AND status IN ('pending','approved')", (run_id,)).fetchall():
                if self._expire(conn, previous):
                    continue
                if previous["spec_digest"] == spec.digest:
                    if self.spec_for(previous["id"], conn=conn).json != spec.json:
                        raise ValidationError("Operation snapshot mismatch")
                    return self.get(previous["id"], conn=conn)
                self._invalidate(conn, previous, "operation_changed")
            conn.execute("INSERT OR IGNORE INTO operation_specs VALUES(?,?,?,?)", (spec.digest, run_id, spec.json, utc_now()))
            saved = self.store.one("SELECT run_id,body_json FROM operation_specs WHERE digest=?", (spec.digest,), conn=conn)
            if (saved[0], saved[1]) != (run_id, spec.json):
                raise ValidationError("Stored operation fingerprint collision")
            approval_id, now = new_id(), utc_now()
            expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(timespec="microseconds")
            conn.execute("""INSERT INTO approvals(id,task_id,run_id,action_hash,status,created_at,expires_at,spec_digest)
                VALUES(?,?,?,?,?,?,?,?)""", (approval_id, run["task_id"], run_id, action.hash, A.PENDING, now, expires, spec.digest))
            if self.tasks.get(run["task_id"], conn=conn).status == T.PLANNED:
                self.tasks._transition(conn, run["task_id"], T.AWAITING_APPROVAL)
            self.store.event(conn, "approval.requested", task_id=run["task_id"], run_id=run_id,
                             approval_id=approval_id, action_hash=action.hash, spec_digest=spec.digest)
            return self.get(approval_id, conn=conn)

    def decide(self, approval_id: str, *, allow: bool) -> Approval:
        if type(allow) is not bool:
            raise ValidationError("An explicit boolean decision is required")
        expired, changed = False, False
        with self.store.transaction(admit=allow) as conn:
            row = self.store.one("SELECT * FROM approvals WHERE id=?", (approval_id,), conn=conn)
            expired = self._expire(conn, row)
            if not expired:
                target = A.APPROVED if allow else A.REJECTED
                if row["status"] not in {A.PENDING, target}:
                    raise Conflict("An approval decision cannot be reversed or reused")
                if allow:
                    if self.session_required and self.session_required(row["run_id"]):
                        raise Conflict("An autonomous Run requires a derived session Approval")
                    run = self.store.one("SELECT * FROM runs WHERE id=?", (row["run_id"],), conn=conn)
                    changed = run["status"] != "created" or not self.matches(run, self.spec_for(approval_id, conn=conn))
                    if changed:
                        self._invalidate(conn, row, "operation_changed_before_decision")
                if not changed and row["status"] != target:
                    conn.execute("UPDATE approvals SET status=?,decided_at=? WHERE id=?", (target, utc_now(), approval_id))
                    self.store.event(conn, "approval.decided", task_id=row["task_id"], run_id=row["run_id"],
                                     approval_id=approval_id, allowed=allow, spec_digest=row["spec_digest"])
            result = self.get(approval_id, conn=conn)
        if expired:
            raise Conflict("Approval has expired")
        if changed:
            raise Conflict("Operation changed; request and review a new approval")
        return result

    def revoke(self, approval_id: str, *, reason: str = "operator_request") -> Approval:
        public_text(reason, limit=500, empty=False)
        error = None
        with self.store.transaction() as conn:
            row = self.store.one("SELECT * FROM approvals WHERE id=?", (approval_id,), conn=conn)
            if self._expire(conn, row):
                error = "Expired permissions cannot be revoked again"
            elif row["status"] == A.REVOKED:
                return self.get(approval_id, conn=conn)
            elif row["status"] not in {A.PENDING, A.APPROVED}:
                error = "Permission is terminal; revocation cannot undo dispatch or an effect"
            else:
                conn.execute("UPDATE approvals SET status=?,revoked_at=? WHERE id=?", (A.REVOKED, utc_now(), approval_id))
                self.store.event(conn, "approval.revoked", task_id=row["task_id"], run_id=row["run_id"],
                                 approval_id=approval_id, reason=reason, spec_digest=row["spec_digest"])
            result = self.get(approval_id, conn=conn)
        if error:
            raise Conflict(error)
        return result

    def _consume(self, conn, run, action: Action, approval_id: str | None, bound_methods=None) -> str | None:
        if not self.needs_approval(conn, run, action):
            if approval_id is not None:
                return "unexpected_approval"
            return None
        if approval_id is None:
            return "approval_missing"
        row = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            return "approval_missing"
        if (row["task_id"], row["run_id"], row["action_hash"]) != (run["task_id"], run["id"], action.hash):
            return "approval_scope_mismatch"
        if row["spec_digest"] is None:
            return "approval_invalidated"
        if not self.matches(run, self.spec_for(approval_id, conn=conn), bound_methods):
            return "approval_scope_mismatch"
        if self._expire(conn, row):
            return "approval_expired"
        if row["status"] != A.APPROVED:
            return "approval_not_granted"
        if not self._session_valid(conn, row):
            return "session_permission_unavailable"
        conn.execute("UPDATE approvals SET status=?,consumed_at=? WHERE id=?", (A.CONSUMED, utc_now(), approval_id))
        self.store.event(conn, "approval.consumed", task_id=run["task_id"], run_id=run["id"],
                         approval_id=approval_id, action_hash=action.hash, spec_digest=row["spec_digest"])
        return None
