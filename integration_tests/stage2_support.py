"""Audited finite stage-two harness. No stop/kill/rm/pull/build operations."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import threading
import time

from ii_pro_core import Action, Core, TaskStatus
from ii_pro_core.security import no_links
from ii_pro_worker import DockerBackend, WorkerAdapter
from ii_pro_worker.docker_cli import DockerCLI
from ii_pro_worker.supervisor import Limits


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


class Capture:
    def __init__(self, source, path, limit):
        self.source, self.output, self.limit = source, path.open("xb"), limit
        self.count, self.closed = 0, False

    def read(self, size):
        data = self.source.read(size)
        retained = data[:max(0, self.limit - self.count)]
        self.output.write(retained)
        self.output.flush()
        self.count += len(retained)
        return data

    def close(self):
        if not self.closed:
            self.closed = True
            self.source.close()
            self.output.close()


class LoggedCLI(DockerCLI):
    def __init__(self, directory):
        super().__init__()
        self.directory = Path(directory)
        self.directory.mkdir(exist_ok=True)
        no_links(self.directory)
        self.audit, self.audit_lock = [], threading.Lock()

    def identity(self):
        return {**super().identity(), "audit_wrapper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}

    def spawn(self, args):
        process = super().spawn(args)
        if len(args) > 2 and args[2] == "start" and "--attach" in args:
            cid = args[-1]
            if len(cid) != 64 or any(ch not in "abcdef0123456789" for ch in cid):
                raise ValueError("Expected exact owned container ID")
            process.stdout = Capture(process.stdout, self.directory / (cid + "-stdout.bin"), 65536)
            process.stderr = Capture(process.stderr, self.directory / (cid + "-stderr.bin"), 4096)
        with self.audit_lock:
            self.audit.append((list(args), process))
        self.flush_audit()
        return process

    def flush_audit(self):
        with self.audit_lock:
            write_json(self.directory / "commands.json", [
                {"argv": args, "pid": process.pid, "exit_code": process.poll()} for args, process in self.audit])


class Case:
    def __init__(self, directory, settings, *, limits=None):
        self.directory = Path(directory)
        self.profile, self.review = settings
        self.limits = limits or Limits(runtime_ms=5000, heartbeat_ms=2000, cancel_grace_ms=500)
        self.cli = LoggedCLI(self.directory / "wire")
        self.generation = 0
        self.open()

    def open(self):
        self.worker = WorkerAdapter(DockerBackend(self.profile, approved_review=self.review, cli=self.cli), limits=self.limits)
        self.core = Core(self.directory / "state", executors=[self.worker], shutdown_timeout=15)
        self.worker.bind(self.core)
        self.generation += 1

    def run(self, fixture, arguments=None):
        task = self.core.tasks.create("Authorized finite stage-two test", executor="worker", idempotency_key="task")
        self.core.tasks.transition(task.id, TaskStatus.PLANNED)
        run = self.core.runs.create(task.id, idempotency_key="run",
            action=Action.build("worker.fixed", {"fixture": fixture, "arguments": arguments or {}}))
        approval = self.core.approvals.request(run.id)
        write_json(self.directory / "operation.json", self.core.approvals.describe(approval.id))
        # Explicit user instruction authorizes this fixed test matrix only.
        self.core.approvals.decide(approval.id, allow=True)
        result = self.core.runs.execute(run.id, approval_id=approval.id)
        self.settled()
        self.snapshot("after-execution")
        return result

    def settled(self):
        deadline = time.monotonic() + 15
        while self.core.active_run_ids:
            if time.monotonic() >= deadline:
                raise AssertionError("Stop is unconfirmed: preserve unknown/ownership, do not force termination")
            time.sleep(0.05)

    def snapshot(self, label):
        self.settled()
        records = self.worker.jobs()
        write_json(self.directory / (label + "-jobs.json"), records)
        write_json(self.directory / (label + "-events.json"), [asdict(e) for e in self.core.events()])
        write_json(self.directory / (label + "-runs.json"), [dict(r) for r in self.core.store.all("SELECT * FROM runs")])
        for job in records:
            if not job["stop_confirmed"]:
                raise AssertionError("No persisted stop evidence")
            identity = json.loads(job["handle_json"])
            observation = self.worker.backend.stopped_observation(identity)
            write_json(self.directory / (label + "-inspect.json"), observation)
            artifacts = self.core.artifacts.list(job["run_id"])
            manifest = []
            for artifact in artifacts:
                content = self.core.artifacts.read(artifact.id)
                assert hashlib.sha256(content).hexdigest() == artifact.sha256
                name = artifact.relative_path.rsplit("/", 1)[-1]
                (self.directory / (label + "-" + name)).write_bytes(content)
                manifest.append({**asdict(artifact), "verified": True})
            write_json(self.directory / (label + "-artifacts.json"), manifest)
        self.cli.flush_audit()

    def reopen(self):
        self.settled()
        self.core.close()
        self.open()
        self.snapshot("after-reopen")

    def finish(self):
        try:
            self.snapshot("final")
        finally:
            self.core.close()
            self.cli.flush_audit()
        # Keep owned, stopped containers for evidence. No cleanup command here.

    def result(self, run_id):
        artifact = next(a for a in self.core.artifacts.list(run_id) if a.relative_path.endswith("/stdout.txt"))
        return json.loads(self.core.artifacts.read(artifact.id))

    def receipt(self):
        return json.loads(self.worker.jobs()[0]["receipt_json"])
