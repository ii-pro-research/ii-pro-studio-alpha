"""UI facade for the existing FunctionalSchool / Worker / Supervisor route."""

from dataclasses import asdict
import hashlib
import json
import re
import threading

from ii_pro_core.errors import Conflict, RecoveryRequired, ValidationError
from ii_pro_school.functional import FunctionalSchool
from ii_pro_worker import WorkerAdapter
from ii_pro_worker.docker_launcher import DockerBackend
from ii_pro_worker.profile import DockerProfile
from ii_pro_worker.supervisor import Limits

from .reader import Source, snapshot, public

IMAGE = "sha256:81538a4143cb5aac5d330ce045e2b8b9354c8dfc61a9fb7866bb91011518dc50"
REVIEW = "b24f7d54f5c99760f9088a79ce6f96c7587fcb9d6953afb416fda047c02d0c21"
SUITE = "sum-positive-baseline-v1"


def selected_source(root, run_id):
    if run_id is None:
        return ()
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValidationError("An exact functional Run permit is required")
    with snapshot(
        Source("permit", "permit", root), root.parent / "ui-functional-snapshots"
    ) as db:
        row = db.execute(
            """SELECT s.code_sha256 FROM school_v2_evaluations e
            JOIN school_v2_packages p ON p.id=e.package_id JOIN school_v2_submissions s
            ON s.id=p.submission_id WHERE e.run_id=?""",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValidationError("Prepare the functional Run before authorizing it")
        return (row[0],)


def build_worker(root, authorized_run):
    return WorkerAdapter(
        DockerBackend(DockerProfile(IMAGE), approved_review=REVIEW, legacy_school=True),
        limits=Limits(runtime_ms=5000, heartbeat_ms=2000, cancel_grace_ms=500),
        python_sources=selected_source(root, authorized_run),
    )


class FunctionalLessons:
    def __init__(self, owner, worker, authorized_run=None):
        self.owner, self.worker = owner, worker
        self.core = owner.school.core
        self.store = self.core.store
        self.authorized_run = authorized_run
        worker.bind(self.core)
        self.domains = {
            key: FunctionalSchool(
                s, worker, worker.python_sources if authorized_run else ()
            )
            for key, s in owner.schools.items()
        }
        for domain in self.domains.values():
            domain.school.functional = domain
        next(iter(self.domains.values())).install()

    def blocked(self, run_id=None):
        if run_id and self.owner.education and self.owner.education.pilot_run(run_id):
            return self.pilot_blocked()
        if run_id and self.owner.education and self.owner.education.comparative_origin_blocked():
            return True
        return bool(
            self.store.all("""SELECT 1 FROM runs WHERE status IN ('running','unknown')
                    OR effect_status IN ('unknown','partial') OR (status='failed' AND effect_status='applied') LIMIT 1""")
        ) or bool(
            self.store.all("SELECT 1 FROM worker_jobs WHERE stop_confirmed=0 LIMIT 1")
        )

    def pilot_blocked(self):
        # The Team journal is authoritative for exact abandoned text operations.
        # Local inference and Worker, including a previous pilot attempt, stay fenced.
        blocked = self.owner.team.blocked() if self.owner.team else self.blocked()
        education = self.owner.education
        return (blocked or education.comparative_origin_blocked(pilot=True)
                or (education.plan() is not None and not education.pilot_card_ready()))

    def _find(self, run_id):
        return self.store.one(
            """SELECT e.package_id,s.source_run_id FROM school_v2_evaluations e
            JOIN school_v2_packages p ON p.id=e.package_id JOIN school_v2_submissions s ON s.id=p.submission_id
            JOIN runs r ON r.id=e.run_id
            WHERE e.run_id=? AND s.source_run_id IS NOT NULL AND r.executor=?""",
            (run_id, self.worker.name),
        )

    def _domain(self, source_run):
        if self.core.runs.get(source_run).executor not in {
            s.adapter.name for s in self.owner.schools.values()
        }:
            raise ValidationError("A saved student response is required")
        school = self.owner._school_for(source_run)
        return next(d for d in self.domains.values() if d.school is school)

    def prepare(self, body):
        if (
            set(body) != {"source_run_id"}
            or not isinstance(body["source_run_id"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", body["source_run_id"])
        ):
            raise ValidationError("An exact saved response Run is required")
        with self.owner.lock:
            if self.owner.stopping or self.owner.threads or self.blocked():
                raise RecoveryRequired(
                    "Active or unresolved execution blocks functional preparation"
                )
            source = body["source_run_id"]
            domain = self._domain(source)
            if (
                self.core.runs.get(source).status != "succeeded"
                or self.store.one(
                    "SELECT lesson_id FROM school_attempts WHERE run_id=?", (source,)
                )[0]
                != "bug-positive-sum"
            ):
                raise ValidationError(
                    "Only a confirmed supported lesson can be prepared"
                )
            student = self.store.one(
                """SELECT s.student_id FROM school_attempts a JOIN school_sessions s
                ON s.id=a.session_id WHERE a.run_id=?""",
                (source,),
            )[0]
            cycle_id = hashlib.sha256(
                ("ui-functional-v1:" + source).encode()
            ).hexdigest()[:32]
            cycle = domain.create_cycle(student, cycle_id=cycle_id)
            submission = domain.submit_saved_response(cycle, SUITE, source)
            package = domain.prepare(submission)
            run = domain.prepare_run(package)
            if run.status == "created":
                self.core.approvals.request(run.id, ttl_seconds=3600)
            return self.view(run.id)

    def decide(self, body):
        if (
            set(body) != {"run_id", "approval_id", "spec_digest", "allow"}
            or type(body["allow"]) is not bool
        ):
            raise ValidationError("Explicit functional decision required")
        for key, length in (("run_id", 32), ("approval_id", 32), ("spec_digest", 64)):
            if not isinstance(body[key], str) or not re.fullmatch(
                r"[a-f0-9]{%d}" % length, body[key]
            ):
                raise ValidationError("Invalid functional operation identity")
        with self.owner.lock:
            record = self._find(body["run_id"])
            run = self.core.runs.get(body["run_id"])
            permit = self.core.approvals.get(body["approval_id"])
            if permit.run_id != run.id or permit.spec_digest != body["spec_digest"]:
                raise Conflict("Functional approval mismatch")
            if run.status == "unknown":
                raise RecoveryRequired(
                    "Unknown requires separate stop/effect reconciliation"
                )
            if run.id in self.owner.threads or run.status != "created":
                return self.view(run.id)
            if self.owner.stopping or self.owner.threads or self.blocked(run.id):
                raise RecoveryRequired("Functional execution is blocked")
            if body["allow"] and run.id != self.authorized_run:
                raise Conflict(
                    "Separate user permission for this exact Worker Run is required"
                )
            # Re-read immutable source/package before consuming the permit.
            domain = self._domain(record["source_run_id"])
            package_row, package = domain._package(record["package_id"])
            submission = domain._submission(package_row["submission_id"])
            if json.loads(run.description) != {
                "source_run_id": record["source_run_id"],
                "answer_sha256": submission["answer_sha256"],
                "submission_id": submission["id"],
                "package_digest": package["digest"],
            }:
                raise Conflict(
                    "Source response scope differs from the approved execution"
                )
            if (
                domain.school.response(record["source_run_id"]).encode()
                != submission["answer"]
                or json.loads(run.action_json)["parameters"]["arguments"]["package"]
                != package
            ):
                raise Conflict("Saved response or package identity changed")
            if not self.core.approvals.matches(
                asdict(run), self.core.approvals.spec_for(permit.id)
            ):
                raise Conflict(
                    "Worker, policy or session changed; prepare and review a fresh approval"
                )
            self.core.approvals.decide(permit.id, allow=body["allow"])
            if body["allow"]:
                thread = threading.Thread(
                    target=self._execute,
                    args=(run.id, record["package_id"], domain, permit.id),
                    name="functional-ui-" + run.id,
                )
                self.owner.threads[run.id] = thread
                try:
                    thread.start()
                except BaseException:
                    self.owner.threads.pop(run.id, None)
                    self.core.approvals.revoke(
                        permit.id, reason="functional_dispatch_not_started"
                    )
                    raise
            return self.view(run.id)

    def revoke(self, body):
        if set(body) != {"run_id", "approval_id", "spec_digest"}:
            raise ValidationError("Exact functional scope required")
        for key, size in (("run_id", 32), ("approval_id", 32), ("spec_digest", 64)):
            if not isinstance(body[key], str) or not re.fullmatch(
                r"[a-f0-9]{%d}" % size, body[key]
            ):
                raise ValidationError("Invalid functional identity")
        with self.owner.lock:
            self._find(body["run_id"])
            permit = self.core.approvals.get(body["approval_id"])
            if (permit.run_id, permit.spec_digest) != (
                body["run_id"],
                body["spec_digest"],
            ):
                raise Conflict("Functional approval mismatch")
            self.core.approvals.revoke(permit.id, reason="ui_operator_request")
            return self.view(permit.run_id)

    def _execute(self, run_id, package_id, domain, approval_id):
        try:
            run = domain.execute(package_id, approval_id=approval_id)
            if run.status == "succeeded":
                domain.collect(package_id)
        except Exception:
            with self.store.transaction() as conn:
                self.store.event(
                    conn, "ui.functional_completion_unconfirmed", run_id=run_id
                )
        finally:
            with self.owner.lock:
                self.owner.threads.pop(run_id, None)

    def view(self, run_id):
        with self.store.lock:
            record = self._find(run_id)
            domain = self._domain(record["source_run_id"])
            row, package = domain._package(record["package_id"])
            sub = domain._submission(row["submission_id"])
            if (
                domain.school.response(record["source_run_id"]).encode()
                != sub["answer"]
            ):
                raise Conflict(
                    "The original saved response differs from the frozen submission"
                )
            self.core.artifacts.validate_run(record["source_run_id"], require_any=True)
            run = self.core.runs.get(run_id)
            permit = self.store.connection.execute(
                "SELECT id FROM approvals WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            approval = self.core.approvals.get(permit[0]) if permit else None
            spec = self.core.approvals.describe(approval.id) if approval else None
            config = json.loads(
                json.dumps(
                    spec["executor"]["configuration"]
                    if spec
                    else self.worker.approval_configuration()
                )
            )
            if "cli" in config.get("backend", {}):
                config["backend"]["cli"]["executable"] = (
                    public(config["backend"]["cli"].get("executable"), 120) or None
                )
            measured = self.store.all(
                "SELECT body_json FROM school_v2_results WHERE package_id=?",
                (record["package_id"],),
            )
            stdout, stderr, exit_code, stop_confirmed, observations = (
                None,
                None,
                None,
                False,
                [],
            )
            receipt = None
            artifacts = self.core.artifacts.list(run_id)
            if run.status == "succeeded":
                self.core.artifacts.validate_run(run_id, require_any=True)
                for artifact in artifacts:
                    if artifact.relative_path.endswith("/result.json"):
                        receipt = json.loads(self.core.artifacts.read(artifact.id))
            else:
                job = self.store.all(
                    "SELECT receipt_json,stop_confirmed FROM worker_jobs WHERE run_id=?",
                    (run_id,),
                )
                if job and job[0]["receipt_json"]:
                    receipt = json.loads(job[0]["receipt_json"])
                    receipt["stop_confirmed"] = (
                        bool(job[0]["stop_confirmed"])
                        and receipt.get("stop_confirmed") is True
                    )
            if receipt:
                result = receipt.get("result") or {}
                stdout = public(result["stdout"], 4096) if "stdout" in result else None
                stderr = public(result["stderr"], 4096) if "stderr" in result else None
                exit_code, stop_confirmed = (
                    receipt.get("process_exit_code"),
                    receipt.get("stop_confirmed") is True,
                )
                observations = receipt.get("backend_observations", [])
            return {
                "run_id": run_id,
                "source_run_id": record["source_run_id"],
                "attempt": run.attempt,
                "task_id": run.task_id,
                "status": str(run.status),
                "effect_status": str(run.effect_status),
                "phase": "dispatching"
                if run_id in self.owner.threads and run.status == "created"
                else str(run.status),
                "error_code": run.error_code,
                "code": sub["code"],
                "code_sha256": sub["code_sha256"],
                "answer_sha256": sub["answer_sha256"],
                "origin": sub["origin"],
                "suite_id": sub["suite_id"],
                "cases": package["cases"],
                "suite_digest": package["suite_digest"],
                "package_id": record["package_id"],
                "package_digest": package["digest"],
                "worker_configuration": config,
                "approval": None
                if approval is None
                else {
                    k: getattr(approval, k)
                    for k in ("id", "status", "spec_digest", "expires_at")
                },
                "dispatch_allowed": run_id == self.authorized_run,
                "static": json.loads(sub["static_json"]),
                "functional": domain.collect(record["package_id"])
                if measured and run.status == "succeeded" and stop_confirmed
                else None,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
                "stop_confirmed": stop_confirmed,
                "worker_reason": receipt.get("reason") if receipt else None,
                "bootstrap_stderr": public(receipt.get("bootstrap_stderr", ""), 4096)
                if receipt
                else None,
                "evidence_sealed": run.status == "succeeded",
                "observations": observations,
                "artifacts": [
                    {k: getattr(a, k) for k in ("id", "sha256", "size")}
                    for a in artifacts
                ],
            }

    def state(self):
        return [
            self.view(r[0])
            for r in self.store.all("""SELECT e.run_id FROM school_v2_evaluations e
            JOIN school_v2_packages p ON p.id=e.package_id JOIN school_v2_submissions s ON s.id=p.submission_id
            WHERE s.source_run_id IS NOT NULL ORDER BY s.created_at DESC LIMIT 32""")
        ]
