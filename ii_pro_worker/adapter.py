"""Core's trusted adapter. Worker never receives Store, paths or Approval API."""
from __future__ import annotations

from dataclasses import asdict
import json
import re
import threading

from ii_pro_core.errors import Conflict, RecoveryRequired, ValidationError
from ii_pro_core.executors import ExecutionResult, Executor, ProducedArtifact
from ii_pro_core.security import canonical, digest, public_text
from ii_pro_core.storage import new_id, utc_now

from .backends import DockerBackend, NativeFixtureBackend
from .errors import DockerStartUncertain
from .supervisor import Limits, Supervisor
from .wire import ProtocolError, validate_task


class WorkerAdapter(Executor):
    name = "worker"
    revision = "worker-school-v0.2-readiness"
    supports_cancellation = True

    def __init__(self, backend, *, limits=Limits(), python_sources=()):
        if type(limits) is not Limits:
            raise ValidationError("Frozen Limits required")
        self._backend, self._limits, self.store = backend, limits, None
        if (type(python_sources) is not tuple or len(python_sources) > 8
                or any(type(s) is not str or not re.fullmatch(r"[a-f0-9]{64}", s) for s in python_sources)):
            raise ValidationError("Explicit immutable source-digest authorizations required")
        self._python_sources = tuple(sorted(set(python_sources)))
        self._unconfirmed = {}

    @property
    def backend(self):
        return self._backend

    @property
    def limits(self):
        return self._limits

    @property
    def python_sources(self):
        return self._python_sources

    def bind(self, core):
        """Trusted host composition step, once, after Core startup recovery."""
        if self.store is not None:
            raise Conflict("Worker adapter already bound")
        if core.runs.executors.get(self.name) is not self or core.active_run_ids:
            raise ValidationError("Bind only the registered adapter before execution")
        store = core.store
        with store.transaction() as conn:
            rows = conn.execute("SELECT job_id,run_id FROM worker_jobs WHERE stop_confirmed=0").fetchall()
            for row in rows:
                conn.execute("UPDATE worker_jobs SET phase='unknown',updated_at=? WHERE job_id=?", (utc_now(), row["job_id"]))
                store.event(conn, "worker.quarantined", run_id=row["run_id"], job_id=row["job_id"])
        self.store = store

    def approval_configuration(self):
        return {"backend": self.backend.configuration(), "limits": asdict(self.limits),
                "protocol": "fixed-frames-v1", "artifact_storage": "sealed-sqlite-blobs",
                "python_sources": list(self.python_sources)}

    def prepare(self, request):
        if self.store is None or request.action.kind != "worker.fixed" or request.operation_spec is None:
            raise ValidationError("Bound Worker and an explicitly approved fixed action required")
        try:
            validate_task(json.loads(request.action.parameters_json))
        except ProtocolError as exc:
            raise ValidationError("Invalid Worker task") from exc
        if request.operation_spec.executor_configuration != self.approval_configuration():
            raise ValidationError("Worker configuration changed")
        self.store.verify_schema()
        if self.store.schema_version >= 8 and self.store.one(
                "SELECT COALESCE(SUM(length(content)),0) FROM artifact_blobs")[0] + 65536 > self.store.one(
                "SELECT blob_bytes FROM school_v2_policy WHERE id=1")[0]:
            raise ValidationError("Insufficient aggregate evidence quota before dispatch")
        task = json.loads(request.action.parameters_json)
        if task["fixture"] == "python_test":
            self.authorize_python(request, task["arguments"]["package"])
        elif task["fixture"] == "app_test":
            self.authorize_app(request, task["arguments"]["package"])
        if self.store.one("SELECT COUNT(*) FROM worker_jobs WHERE stop_confirmed=0")[0]:
            raise RecoveryRequired("An old Worker may still be active; trusted stop evidence required")
        self.backend.prepare()
        if type(self.backend) not in (NativeFixtureBackend, DockerBackend):
            raise ValidationError("Only fixed registered backends are enabled")
        if type(self.backend) is NativeFixtureBackend and json.loads(request.action.parameters_json)["fixture"] in {"isolation", "memory"}:
            raise ValidationError("Isolation probes require Docker")

    def authorize_python(self, request, package):
        if (type(self.backend) is not DockerBackend or package["function"] != "sum_positive"
                or package["source_sha256"] not in self.python_sources):
            raise ValidationError("This exact School source is not authorized for Docker evaluation")

    def authorize_app(self, request, package):
        raise ValidationError("No approved application test profile")

    def jobs(self):
        return [dict(row) for row in self.store.all("SELECT * FROM worker_jobs ORDER BY created_at,job_id")]

    def execute(self, request):
        config = request.operation_spec.executor_configuration
        backend = self.backend
        limits = Limits(**config["limits"])
        if config != self.approval_configuration():
            raise ValidationError("Worker profile changed after approval")
        job_id, epoch = new_id(), self.store.generation
        cancel = threading.Event()
        release = self.store.retain_external_execution(request.run_id, cancel)
        # Core cancellation token is observed independently from reaper lifetime.
        cancellation = request.cancellation

        class CombinedCancellation:
            def is_set(self):
                return cancel.is_set() or (cancellation is not None and cancellation.requested)

            def wait(self, timeout):
                cancel.wait(timeout)
                return self.is_set()

        now = utc_now()
        locator = backend.locator(job_id, request.run_id, epoch) if type(backend) is DockerBackend else None
        try:
            with self.store.transaction() as conn:
                if conn.execute("SELECT 1 FROM worker_jobs WHERE stop_confirmed=0").fetchone():
                    raise RecoveryRequired("Worker dispatch is quarantined")
                conn.execute("INSERT INTO worker_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                             (job_id, request.run_id, epoch, digest(config), backend.name,
                              "launching", canonical(locator) if locator else None, 0, None, now, now))
                self.store.event(conn, "worker.launch_intent", task_id=request.task_id, run_id=request.run_id,
                                 job_id=job_id, epoch=epoch, spec_digest=request.operation_spec.digest,
                                 profile_digest=digest(config), max_runtime_ms=limits.runtime_ms)
        except BaseException:
            release()  # no external dispatch has been attempted
            raise

        def publish(phase, identity, receipt, stopped):
            # Every write fenced by Store.transaction; stale owners cannot commit.
            if receipt is not None:
                public_text(canonical(receipt), limit=32768)
            with self.store.transaction() as conn:
                row = self.store.one("SELECT * FROM worker_jobs WHERE job_id=?", (job_id,), conn=conn)
                if row["epoch"] != epoch:
                    raise Conflict("Worker epoch changed")
                if row["stop_confirmed"]:
                    return  # stop evidence never regresses to unknown
                conn.execute("UPDATE worker_jobs SET phase=?,handle_json=?,receipt_json=?,stop_confirmed=?,updated_at=? WHERE job_id=?",
                             (phase, canonical(identity), canonical(receipt) if receipt else None, int(stopped), utc_now(), job_id))
                self.store.event(conn, "worker." + phase, task_id=request.task_id, run_id=request.run_id,
                                 job_id=job_id, stop_confirmed=stopped,
                                 accepted=receipt["accepted"] if receipt else False,
                                 receipt_digest=digest(receipt) if receipt else None)

        try:
            handle = backend.start(job_id=job_id, run_id=request.run_id, epoch=epoch) if type(backend) is DockerBackend else backend.start()
        except DockerStartUncertain as exc:
            self._unconfirmed[job_id] = (release, exc.process)
            with self.store.transaction() as conn:
                conn.execute("UPDATE worker_jobs SET phase='unknown',handle_json=?,updated_at=? WHERE job_id=?",
                             (canonical(exc.identity), utc_now(), job_id))
                self.store.event(conn, "worker.start_uncertain", run_id=request.run_id, job_id=job_id)
            raise  # no release without stop proof
        except BaseException:
            # Native Popen failure or Docker failure BEFORE attach/start.
            # Durable intent remains quarantined, even if create ack was lost.
            release()
            raise
        value = {"version": 1, "job_id": job_id, "run_id": request.run_id, "epoch": epoch, "seq": 0,
                 "type": "request", "payload": {"task": json.loads(request.action.parameters_json),
                 "spec_digest": request.operation_spec.digest, "max_runtime_ms": limits.runtime_ms}}
        receipt = Supervisor(limits).run(handle, value, CombinedCancellation(), publish, release)
        result = receipt["result"]
        return ExecutionResult((ProducedArtifact("result.json", (canonical(receipt) + "\n").encode()),
                                ProducedArtifact("stdout.txt", result["stdout"].encode()),
                                ProducedArtifact("stderr.txt", result["stderr"].encode())), storage="sqlite")

    def _docker_job(self, job_id):
        if type(self.backend) is not DockerBackend:
            raise ValidationError("Docker backend required")
        row = self.store.one("SELECT * FROM worker_jobs WHERE job_id=?", (job_id,))
        if row["backend"] != self.backend.name or row["profile_digest"] != digest(self.approval_configuration()):
            raise RecoveryRequired("Original approved backend profile is required")
        identity = json.loads(row["handle_json"])
        if (identity["job_id"], identity["run_id"], identity["epoch"]) != (job_id, row["run_id"], row["epoch"]):
            raise RecoveryRequired("Saved job identity mismatch")
        return row, identity

    def recover_docker_job(self, job_id):
        """Read-only daemon query + durable stop receipt; never resolve effects."""
        row, identity = self._docker_job(job_id)
        if row["stop_confirmed"]:
            return json.loads(row["receipt_json"])
        if self.store.one("SELECT status FROM runs WHERE id=?", (row["run_id"],))[0] != "unknown":
            raise RecoveryRequired("Wait for the active invocation to finish before stop reconciliation")
        pending = self._unconfirmed.get(job_id)
        if pending and pending[1] is not None and pending[1].poll() is None:
            raise RecoveryRequired("The old Docker control process is still active")
        observation = self.backend.stopped_observation(identity)
        identity["container_id"] = observation["id"]
        receipt = {"job_id": job_id, "run_id": row["run_id"], "epoch": row["epoch"],
                   "stop_confirmed": True, "recovered_stop_only": True, "container": observation}
        with self.store.transaction() as conn:
            current = self.store.one("SELECT * FROM worker_jobs WHERE job_id=?", (job_id,), conn=conn)
            if not current["stop_confirmed"]:
                conn.execute("UPDATE worker_jobs SET phase='exited',stop_confirmed=1,receipt_json=?,handle_json=?,updated_at=? WHERE job_id=?",
                             (canonical(receipt), canonical(identity), utc_now(), job_id))
                self.store.event(conn, "worker.stop_recovered", run_id=row["run_id"], job_id=job_id,
                                 container_id=identity["container_id"], receipt_digest=digest(receipt))
        if pending:
            pending[0]()
            self._unconfirmed.pop(job_id, None)
        return receipt

    def cleanup_docker_job(self, job_id):
        # close cannot release the owner/DB during this already-admitted cleanup.
        with self.store.lock:
            self.store._require_connection()
            self.store.assert_owner(self.store.connection)
            row, identity = self._docker_job(job_id)
            if not row["stop_confirmed"]:
                raise RecoveryRequired("Persist stop evidence before removing a container")
            cid = self.backend.remove_stopped(identity)
            with self.store.transaction() as conn:
                self.store.event(conn, "worker.container_removed", run_id=row["run_id"], job_id=job_id, container_id=cid)
            return cid
