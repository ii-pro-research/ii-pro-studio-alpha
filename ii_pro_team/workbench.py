"""Team operator workflow for sealed apply, tests and rollback."""

import json

from ii_pro_core.errors import Conflict, CoreError, RecoveryRequired, ValidationError
from ii_pro_core.security import Action, canonical
from ii_pro_core.storage import utc_now
from ii_pro_local.engineering_executor import artifact_json, change, hashes, render_diff
from ii_pro_worker.adapter import WorkerAdapter
from ii_pro_worker.docker_launcher import DockerBackend
from ii_pro_worker.profile import DockerProfile
from ii_pro_worker.school_contract import PackageError, make_package, validate_result
from ii_pro_worker.supervisor import Limits

from . import engineering
from .protocol import identity

COMMAND = ["/usr/local/bin/python", "-I", "-S", "-B", "/opt/worker/runtime.py"]
PHASES = ("apply", "test", "rollback")
CASES = (
    ("discount_ten", 100, 10, 90),
    ("zero_percent", 100, 0, 100),
    ("full_discount", 100, 100, 0),
    ("different_price", 80, 25, 60),
    ("zero_price", 0, 25, 0),
    ("larger_price", 250, 6, 235),
)


def package(job, code):
    try:
        return make_package(code, [
            {"id": name, "input": [price, percent], "expected": expected}
            for name, price, percent, expected in CASES
        ], job, function="discounted")
    except PackageError as exc:
        raise ValidationError("Candidate is outside the finite pricing test profile") from exc


def backup_and_copy(core, run):
    if run.executor != "engineering_copy" or run.status != "succeeded":
        raise RecoveryRequired("The sealed working copy is unavailable")
    backup = artifact_json(core, run.id, "backup.json")
    copy = artifact_json(core, run.id, "working-copy.json")
    scope = json.loads(run.action_json)["parameters"]
    if (backup["snapshot"] != copy["snapshot"] or copy["snapshot"] != scope["snapshot"]
            or hashes(backup["files"]) != backup["sha256"] or hashes(copy["files"]) != copy["sha256"]):
        raise Conflict("Working copy or backup identity differs")
    expected = backup["files"].copy()
    for item in scope["changes"]:
        if expected.get(item["path"]) != item["before"]:
            raise Conflict("Frozen source differs from backup")
        if item["after"] is None:
            del expected[item["path"]]
        else:
            expected[item["path"]] = item["after"]
    if expected != copy["files"]:
        raise Conflict("Working copy differs from the approved change")
    return backup, copy


class EngineeringWorker(WorkerAdapter):
    name = "engineering_worker"
    revision = "worker-pricing-arithmetic-v1"

    def __init__(self, image, review, permit):
        super().__init__(DockerBackend(DockerProfile(image), approved_review=review),
                         limits=Limits(runtime_ms=5000, heartbeat_ms=2000, cancel_grace_ms=500))
        self.permit = identity(permit) if permit is not None else None
        self.core = None
        self.project_authorize = None

    def approval_configuration(self):
        return {**super().approval_configuration(), "suite": "pricing-arithmetic-v1", "command": COMMAND}

    def authorize_python(self, request, received):
        if request.run_id != self.permit or not self.backend.configuration()["launch_enabled"]:
            raise ValidationError("Exact operator Run and reviewed Docker runtime required")
        scope = json.loads(request.description)
        _, copy = backup_and_copy(self.core, self.core.runs.get(scope["parent"]))
        approved_copy = self.core.runs.executors["engineering_copy"]
        snapshot = approved_copy.projects[engineering.PROJECT][1]
        if snapshot.identity != copy["snapshot"]:
            raise Conflict("Pricing snapshot registration changed")
        snapshot.confirm_current()
        if received != package(scope["job"], copy["files"][engineering.PATH]):
            raise Conflict("The approved pricing test package differs from its sealed input")

    def authorize_app(self, request, package):
        if not self.project_authorize or not self.backend.configuration()["launch_enabled"]:
            raise ValidationError("An explicitly reviewed application Worker is required")
        self.project_authorize(request.run_id, package)


class Workbench:
    def __init__(self, team, executor, worker):
        self.team, self.core, self.owner = team, team.core, team.owner
        self.executor, self.worker = executor, worker

    def _run(self, job, phase):
        rows = self.core.store.all("SELECT id FROM tasks WHERE idempotency_key=?",
                                   ("engineering:" + job + ":" + phase,))
        runs = self.core.runs.list(rows[0][0]) if rows else []
        return runs[-1] if runs else None

    def proposal(self, job):
        run = self.team._run(job, "implement")
        data = artifact_json(self.core, run.id, "engineering.json") if run.status == "succeeded" else {}
        context = json.loads(run.description)["inputs"]["context"]
        snapshot = self.executor.projects[engineering.PROJECT][1]
        if context["snapshot"] != snapshot.identity or not engineering.shareable(snapshot):
            raise Conflict("Engineer evidence differs from the registered snapshot")
        patch = data.get("patch", {})
        path = engineering.PATH
        original = dict(snapshot.files)[path]
        if (data.get("state") != "proposed" or patch.get("path") != path
                or not patch.get("before") or original.count(patch["before"]) != 1):
            raise ValidationError("No anchored Engineer proposal is available")
        candidate = original.replace(patch["before"], patch["after"], 1)
        item = change(path, original, candidate)
        run_artifact = next(a for a in self.core.artifacts.list(run.id)
                            if a.relative_path.endswith("/engineering.json"))
        return snapshot, item, {"run": run.id, "sha256": run_artifact.sha256}

    def _result(self, run):
        if run.status == "unknown":
            return {"status": "unknown", "diagnostic": run.error_code, "worker": self._receipt(run)}
        if run.status in {"failed", "cancelled"}:
            return {"status": "infrastructure", "diagnostic": run.error_code, "worker": self._receipt(run)}
        if run.status != "succeeded":
            return {"status": "pending"}
        receipt = artifact_json(self.core, run.id, "result.json")
        frozen = json.loads(run.action_json)["parameters"]["arguments"]["package"]
        try:
            result = receipt["result"]
            stderr = result["stderr"]
            stdout = result["stdout"]
            payload = validate_result(json.loads(stdout), frozen)
            if not receipt["stop_confirmed"] or result["status"] != "ok" or result["exit_code"] != 0 or receipt["process_exit_code"] != 0:
                raise ValueError("Unconfirmed result")
            # Provenance is durable across restarts, including a disabled backend.
            permit = self._permit(run)
            backend = self.core.approvals.spec_for(permit.id).executor_configuration.get("backend", {})
            real = backend.get("backend") == "docker-fixed"
            if real:
                observed = receipt["backend_observations"]["container"]
                if (observed["state"]["Status"] != "exited" or observed["image"] != backend["profile"]["image"]
                        or receipt["reason"] is not None or receipt["accepted"] is not True):
                    raise ValueError("Container exit or image identity is unconfirmed")
            verdict = ("passed" if payload["passed"] == payload["total"] else "failed")
            if not real:
                verdict = "mock_" + verdict
            return {"status": verdict, **payload, "stdout": stdout, "stderr": stderr,
                    "command": COMMAND, "worker": receipt}
        except (KeyError, TypeError, ValueError, RecursionError):
            return {"status": "infrastructure", "diagnostic": "invalid_test_evidence", "worker": receipt}

    def _receipt(self, run):
        rows = self.core.store.all("SELECT receipt_json FROM worker_jobs WHERE run_id=?", (run.id,))
        return json.loads(rows[0][0]) if rows and rows[0][0] else None

    def _permit(self, run):
        rows = self.core.store.all("SELECT id FROM approvals WHERE run_id=? ORDER BY rowid DESC LIMIT 1", (run.id,))
        return self.core.approvals.get(rows[0][0]) if rows else None

    def view(self, job):
        try:
            snapshot, item, evidence = self.proposal(job)
        except (CoreError, KeyError, ValueError):
            return None
        proof = None
        try:
            frozen = package(job, item["after"])
            proof = {key: frozen[key] for key in ("source_sha256", "suite_digest", "digest", "cases")}
        except ValidationError:
            pass
        phases = {}
        result = None
        backup_state = "planned"
        recovery = "not_needed"
        for phase in PHASES:
            run = self._run(job, phase)
            if not run:
                continue
            permit = self._permit(run)
            permit_view = None
            if permit:
                status = permit.status
                if status in {"pending", "approved"} and permit.expires_at <= utc_now():
                    status = "expired"
                elif status == "pending" and run.status == "created" and not self.core.approvals.matches(
                        self.core.store.one("SELECT * FROM runs WHERE id=?", (run.id,)),
                        self.core.approvals.spec_for(permit.id)):
                    status = "invalidated"
                permit_view = {"id": permit.id, "status": status, "digest": permit.spec_digest, "expires_at": permit.expires_at}
            phases[phase] = {"run": run.id, "status": run.status, "approval": permit_view,
                             "history": [dict(row) for row in self.core.store.all(
                                 "SELECT kind,created_at FROM events WHERE run_id=? ORDER BY id", (run.id,))],
                             "artifacts": [{"name": a.relative_path.rsplit("/", 1)[-1], "sha256": a.sha256}
                                           for a in self.core.artifacts.list(run.id)]}
            if phase == "test":
                try:
                    result = self._result(run)
                except (CoreError, ValueError, KeyError):
                    result = {"status": "integrity_error"}
            if phase == "apply" and run.status == "succeeded":
                try:
                    backup, _ = backup_and_copy(self.core, run)
                    backup_state = "sealed" if backup["files"] == dict(snapshot.files) else "integrity_error"
                except (CoreError, ValueError, KeyError):
                    backup_state = "integrity_error"
            if phase == "rollback" and run.status == "succeeded":
                try:
                    _, restored = backup_and_copy(self.core, run)
                    recovery = "restored" if restored["sha256"] == hashes(dict(snapshot.files)) else "integrity_error"
                except (CoreError, ValueError, KeyError):
                    recovery = "integrity_error"
        if recovery == "not_needed" and backup_state == "sealed" and result and result["status"] in {"failed", "mock_failed", "infrastructure"}:
            recovery = "baseline_selected"
        if backup_state == "integrity_error" or result and result["status"] in {"integrity_error", "unknown"}:
            recovery = "quarantined"
        config = self.worker.approval_configuration() if self.worker is not None else None
        backend = config.get("backend", {}) if config else {}
        return {"revision": snapshot.revision, "snapshot": snapshot.identity, "files": [item],
                "diff": render_diff(item["path"], item["before"], item["after"]), "source": evidence,
                "backup": backup_state, "rollback": recovery, "test_contract": proof, "test": result,
                "active_version": "quarantined" if recovery == "quarantined" else
                ("candidate" if backup_state == "sealed" and recovery == "not_needed" else "baseline"),
                "worker_ready": self.worker is not None and (not isinstance(self.worker, EngineeringWorker)
                                 or backend["launch_enabled"]),
                "worker_profile": {"command": COMMAND, "profile": backend.get("profile"),
                                   "review_digest": backend.get("review_digest")},
                "test_permitted": self.worker is not None and (not isinstance(self.worker, EngineeringWorker)
                                   or self.worker.permit is not None and self.worker.permit == phases.get("test", {}).get("run")),
                "phases": phases}

    def prepare(self, job, phase):
        if self.owner.stopping or self.team.blocked():
            raise RecoveryRequired("Unsettled execution blocks engineering work")
        existing = self._run(job, phase)
        if existing:
            permit = self._permit(existing)
            if existing.status == "created" and (permit is None or permit.status in {"expired", "revoked", "invalidated"}
                    or permit.status == "pending" and (permit.expires_at <= utc_now() or not self.core.approvals.matches(
                        self.core.store.one("SELECT * FROM runs WHERE id=?", (existing.id,)),
                        self.core.approvals.spec_for(permit.id)))):
                self.core.approvals.request(existing.id, ttl_seconds=1800)
            return
        snapshot, item, evidence = self.proposal(job)
        snapshot.confirm_current()
        body = {"job": job, "phase": phase, "engineer": evidence, "parent": None}
        executor = self.executor.name
        params = {"project": engineering.PROJECT, "snapshot": snapshot.identity, "changes": [item], "parent": None}
        kind = "engineering.apply"
        if phase != "apply":
            applied = self._run(job, "apply")
            if applied is None:
                raise Conflict("Apply the approved proposal first")
            backup, copy = backup_and_copy(self.core, applied)
            if backup["files"] != dict(snapshot.files):
                raise Conflict("Backup contains a different snapshot")
            body["parent"] = applied.id
            if phase == "test":
                if self.worker is None or self._run(job, "rollback") is not None:
                    raise Conflict("A reviewed pricing Worker and active candidate are required")
                if isinstance(self.worker, EngineeringWorker) and not self.worker.approval_configuration()["backend"]["launch_enabled"]:
                    raise Conflict("The pinned image requires the current reviewed deployment")
                executor, kind = self.worker.name, "worker.fixed"
                params = {"fixture": "python_test", "arguments": {"package": package(job, copy["files"][engineering.PATH])}}
            elif phase == "rollback":
                tested = self._run(job, "test")
                if tested and tested.status in {"running", "created", "unknown"}:
                    permit = self._permit(tested)
                    if tested.status != "created" or permit is None or permit.status not in {"rejected", "revoked", "expired", "invalidated"}:
                        raise Conflict("Resolve the test operation before rollback")
                kind = "engineering.rollback"
                params["parent"] = applied.id
                params["changes"] = [change(item["path"], copy["files"][item["path"]], backup["files"][item["path"]])]
        action = Action.build(kind, params)
        key = "engineering:" + job + ":" + phase
        task = self.core.tasks.create("Engineering · " + phase, description=canonical(body), executor=executor, idempotency_key=key)
        if task.status == "draft":
            self.core.tasks.transition(task.id, "planned")
        run = self.core.runs.create(task.id, action=action, idempotency_key=key)
        self.core.approvals.request(run.id, ttl_seconds=1800)

    def scope(self, run):
        if run.executor not in {"engineering_copy", "engineering_worker"}:
            return None
        body = json.loads(run.description)
        phase = body["phase"]
        if phase not in PHASES or self._run(body["job"], phase) != run:
            raise Conflict("Run is outside the Team engineering scope")
        return body

    def admit(self, body, run):
        if body["phase"] == "test" and (
                self.worker is None or isinstance(self.worker, EngineeringWorker) and self.worker.permit != run.id):
            raise Conflict("An exact pricing Worker Run permit is required")
        if body["phase"] == "test" and self._run(body["job"], "rollback"):
            raise Conflict("The candidate is already reserved for rollback")
        snapshot, _, evidence = self.proposal(body["job"])
        if evidence != body["engineer"]:
            raise Conflict("The Engineer artifact changed")
        if body["phase"] != "apply":
            snapshot.confirm_current()

    def review_evidence(self, job):
        run = self._run(job, "test")
        if run is None:
            applied = self._run(job, "apply")
            restored = self._run(job, "rollback")
            if applied and applied.status in {"created", "succeeded"} and not (restored and restored.status == "succeeded"):
                raise Conflict("Resolve the working-copy operation before the review")
            return None
        result = self._result(run)
        if result["status"] == "pending":
            restored = self._run(job, "rollback")
            if restored and restored.status == "succeeded":
                return None
            raise Conflict("Finish the prepared tests before the review")
        if result["status"] not in {"passed", "failed"}:
            return None
        sealed = next(a for a in self.core.artifacts.list(run.id) if a.relative_path.endswith("/result.json"))
        return {"run": run.id, "sha256": sealed.sha256, "status": result["status"],
                "passed": result["passed"], "total": result["total"], "suite_digest": result["suite_digest"]}
