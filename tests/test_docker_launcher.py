import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from ii_pro_core import Action, Core, TaskStatus
from ii_pro_core.errors import CoreBusy, RecoveryRequired
from ii_pro_core.security import digest
from ii_pro_core.storage import new_id, utc_now
from ii_pro_worker import DockerBackend, DockerProfile, WorkerAdapter
from ii_pro_worker.acceptance import build_image, proposed_review, verify_base_identity
from ii_pro_worker.docker_cli import DockerCLI
from ii_pro_worker.docker_launcher import QueuedInput, validate_inspection
from ii_pro_worker.errors import DockerApprovalRequired, DockerConfigMismatch, DockerUnavailable

CID = "f" * 64


def inspection(profile, job="a" * 32, run="b" * 32, epoch=1, *, review="review", status="created"):
    command = profile.describe()["entrypoint"]
    return {"id": CID, "name": "/" + profile.container_name(job), "image": profile.image,
            "config": {"User": "65532:65532", "WorkingDir": "/work", "Entrypoint": command[:1], "Cmd": command[1:],
                       "Labels": {**profile.labels(job, run, epoch), "ii-pro.review": review},
                       "Env": ["PYTHON_VERSION=3.12.16"], "Volumes": None, "ExposedPorts": None, "Tty": False},
            "host": {"Privileged": False, "ReadonlyRootfs": True, "NetworkMode": "none", "Memory": 134217728,
                     "MemorySwap": 134217728, "NanoCpus": 500000000, "PidsLimit": 16, "Init": True,
                     "PidMode": "", "UTSMode": "", "IpcMode": "private", "CgroupnsMode": "private", "Runtime": "runc",
                     "UsernsMode": "", "CapDrop": ["ALL"], "CapAdd": None, "SecurityOpt": ["no-new-privileges:true"],
                     "LogConfig": {"Type": "none"}, "Tmpfs": dict(x.split(":", 1) for x in profile.describe()["tmpfs"])},
            "mounts": [], "networks": {"none": {}},
            "state": {"Status": status, "Running": status == "running", "ExitCode": 0, "Dead": False,
                      "OOMKilled": False, "StartedAt": "2026-11-21T00:00:00Z" if status != "created" else "0001-01-01T00:00:00Z",
                      "FinishedAt": "2026-11-21T00:00:01Z" if status == "exited" else "0001-01-01T00:00:00Z"}}


class Process:
    def __init__(self):
        self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()
        self.returncode = 0

    def poll(self):
        return self.returncode


class FakeCLI:
    def __init__(self):
        self.backend, self.info, self.calls, self.unavailable, self.process = None, None, [], False, Process()

    def identity(self):
        return {"executable": "unit-fixture", "sha256": "0" * 64}

    def json(self, args):
        self.calls.append(args)
        if self.unavailable:
            raise DockerUnavailable("fixed unavailable fixture")
        if args[2] == "info":
            return {"os": "linux", "arch": "x86_64", "cgroup": "2", "security": ["name=seccomp,profile=builtin"]}
        if args[2] == "image":
            return {"id": self.backend.profile.image, "os": "linux", "arch": "amd64", "volumes": None,
                    "labels": {"ii-pro.review": self.backend.review_digest}, "env": ["PYTHON_VERSION=3.12.16"]}
        return copy.deepcopy(self.info)

    def call(self, args, **kwargs):
        self.calls.append(args)
        return (CID + "\n").encode()

    def spawn(self, args):
        self.calls.append(args)
        return self.process


def backend():
    profile, cli = DockerProfile("sha256:" + "e" * 64), FakeCLI()
    selected = DockerBackend(profile, cli=cli)
    selected._approved_review = selected.review_digest  # trusted test operator only
    cli.backend = selected
    cli.info = inspection(profile, review=selected.review_digest)
    return selected, cli


@pytest.mark.parametrize("location,key,value", [
    ("host", "Privileged", True), ("host", "ReadonlyRootfs", False), ("host", "NetworkMode", "host"),
    ("host", "Memory", 0), ("host", "MemorySwap", -1), ("host", "NanoCpus", 0), ("host", "PidsLimit", -1),
    ("host", "Init", 1), ("host", "PidMode", "host"), ("host", "IpcMode", "host"),
    ("host", "SecurityOpt", ["seccomp=unconfined"]), ("host", "Binds", ["D:/:/host"]),
    ("host", "Devices", [{"PathOnHost": "/dev/nvidia0"}]), ("host", "CapAdd", ["SYS_ADMIN"]),
    ("host", "PortBindings", {"80/tcp": [{"HostPort": "8000"}]}), ("host", "Tmpfs", {}),
    ("config", "User", "0"), ("config", "Cmd", ["-c", "arbitrary"]), ("config", "Tty", True),
    ("config", "Env", ["CORE_PRIVATE=not-forwarded"]),
])
def test_effective_profile_drift_rejected(location, key, value):
    selected, cli = backend()
    cli.info[location][key] = value
    with pytest.raises(DockerConfigMismatch):
        validate_inspection(cli.info, selected.profile, "a" * 32, "b" * 32, 1, CID, ["PYTHON_VERSION=3.12.16"])


@pytest.mark.parametrize("change", ["foreign_label", "foreign_id", "foreign_name", "mount", "network"])
def test_foreign_container_and_mounts_rejected(change):
    selected, cli = backend()
    if change == "foreign_label":
        cli.info["config"]["Labels"]["ii-pro.job"] = "c" * 32
    elif change == "foreign_id":
        cli.info["id"] = "c" * 64
    elif change == "foreign_name":
        cli.info["name"] = "/other-project"
    elif change == "mount":
        cli.info["mounts"] = [{"Type": "bind", "Destination": "/core"}]
    else:
        cli.info["networks"] = {"bridge": {}}
    with pytest.raises(DockerConfigMismatch):
        selected.inspect_owned(selected.locator("a" * 32, "b" * 32, 1, CID))
    assert not any("rm" in cmd or "start" in cmd for cmd in cli.calls)


def test_cli_exit_is_not_container_exit_and_no_force_cleanup():
    selected, cli = backend()
    selected.prepare()
    handle = selected.start(job_id="a" * 32, run_id="b" * 32, epoch=1)
    cli.info["state"] = inspection(selected.profile, status="running")["state"]
    time.sleep(0.15)
    assert cli.process.poll() == 0 and handle.poll() is None
    with pytest.raises(DockerConfigMismatch):
        selected.remove_stopped(handle.identity)
    cli.info["state"] = inspection(selected.profile, status="exited")["state"]
    deadline = time.monotonic() + 3
    while handle.poll() is None:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert handle.poll() == 0
    handle.cleanup()
    assert selected.remove_stopped(handle.identity) == CID
    assert cli.calls[-1] == ["--context", "desktop-linux", "container", "rm", CID]
    assert not any(any(x in cmd for x in ("--force", "--volumes", "prune", "stop", "kill")) for cmd in cli.calls)


def test_missing_deployment_approval_calls_no_docker():
    selected, cli = backend()
    selected._approved_review = "wrong"
    with pytest.raises(DockerApprovalRequired):
        selected.prepare()
    with pytest.raises(DockerApprovalRequired):
        selected.start(job_id="a" * 32, run_id="b" * 32, epoch=1)
    assert cli.calls == []


def test_docker_unavailable_is_preflight_failure_without_dispatch(tmp_path):
    selected, cli = backend()
    cli.unavailable = True
    worker = WorkerAdapter(selected)
    with Core(tmp_path / "state", executors=[worker]) as core:
        worker.bind(core)
        task = core.tasks.create("Unavailable fixture", executor="worker", idempotency_key="task")
        core.tasks.transition(task.id, TaskStatus.PLANNED)
        run = core.runs.create(task.id, action=Action.build("worker.fixed", {"fixture": "smoke", "arguments": {}}), idempotency_key="run")
        approval = core.approvals.request(run.id)
        core.approvals.decide(approval.id, allow=True)
        result = core.runs.execute(run.id, approval_id=approval.id)
        assert result.status == "failed" and result.error_type == "DockerUnavailable"
        assert result.effect_status == "not_started" and worker.jobs() == []
        assert not any("create" in cmd or "start" in cmd for cmd in cli.calls)


def test_recovery_confirms_only_stop_and_preserves_unknown(tmp_path):
    selected, cli = backend()
    worker = WorkerAdapter(selected)
    with Core(tmp_path / "state", executors=[worker]) as core:
        worker.bind(core)
        task = core.tasks.create("Recovery fixture", executor="worker", idempotency_key="task")
        core.tasks.transition(task.id, TaskStatus.PLANNED)
        run = core.runs.create(task.id, action=Action.build("worker.fixed", {"fixture": "smoke", "arguments": {}}), idempotency_key="run")
        job = new_id()
        identity = selected.locator(job, run.id, core.store.generation, CID)
        cli.info = inspection(selected.profile, job, run.id, core.store.generation, review=selected.review_digest, status="created")
        with core.store.transaction() as conn:
            conn.execute("UPDATE runs SET status='unknown',effect_status='unknown',execution_stopped=0 WHERE id=?", (run.id,))
            conn.execute("INSERT INTO worker_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)", (job, run.id, core.store.generation,
                         digest(worker.approval_configuration()), selected.name, "unknown", json.dumps(identity), 0, None, utc_now(), utc_now()))
        with pytest.raises(DockerConfigMismatch):
            worker.recover_docker_job(job)
        assert worker.jobs()[0]["stop_confirmed"] == 0
        with core.store.transaction() as conn:
            conn.execute("UPDATE runs SET status='running' WHERE id=?", (run.id,))
        with pytest.raises(RecoveryRequired):
            worker.recover_docker_job(job)
        with core.store.transaction() as conn:
            conn.execute("UPDATE runs SET status='unknown' WHERE id=?", (run.id,))
        cli.info["state"] = inspection(selected.profile, status="exited")["state"]
        receipt = worker.recover_docker_job(job)
        assert receipt["stop_confirmed"] and worker.jobs()[0]["stop_confirmed"] == 1
        assert core.runs.get(run.id).status == "unknown" and core.runs.get(run.id).effect_status == "unknown"
        assert worker.cleanup_docker_job(job) == CID
        assert any(e.kind == "worker.stop_recovered" for e in core.events())


def test_control_transport_timeout_does_not_kill_process(monkeypatch):
    cli, processes = DockerCLI(), []
    def spawn(args):
        process = subprocess.Popen([sys.executable, "-I", "-S", "-B", "-c", "import time; time.sleep(.25)"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        processes.append(process)
        return process
    monkeypatch.setattr(cli, "spawn", spawn)
    with pytest.raises(DockerUnavailable, match="timed out"):
        cli.call(["fixed-unit-fixture"], timeout=0.01)
    assert processes[0].poll() is None
    assert processes[0].wait(timeout=3) == 0


def test_uncertain_start_retains_ownership_until_explicit_observed_stop(tmp_path, monkeypatch):
    import ii_pro_worker.docker_launcher as launcher
    selected, cli = backend()
    def create(args, **kwargs):
        labels = dict(args[i + 1].split("=", 1) for i, value in enumerate(args) if value == "--label")
        cli.info = inspection(selected.profile, labels["ii-pro.job"], labels["ii-pro.run"], int(labels["ii-pro.epoch"]),
                              review=selected.review_digest)
        return (CID + "\n").encode()
    cli.call = create
    cli.process.returncode = None
    def fail_monitor(*args):
        raise RuntimeError("fixed monitor startup failure")
    monkeypatch.setattr(launcher, "DockerHandle", fail_monitor)
    worker = WorkerAdapter(selected)
    core = Core(tmp_path / "state", executors=[worker])
    worker.bind(core)
    task = core.tasks.create("Uncertain start", executor="worker", idempotency_key="task")
    core.tasks.transition(task.id, TaskStatus.PLANNED)
    run = core.runs.create(task.id, action=Action.build("worker.fixed", {"fixture": "smoke", "arguments": {}}), idempotency_key="run")
    approval = core.approvals.request(run.id)
    core.approvals.decide(approval.id, allow=True)
    assert core.runs.execute(run.id, approval_id=approval.id).status == "unknown"
    assert core.active_run_ids == (run.id,)
    with pytest.raises(CoreBusy):
        core.close(timeout=0)
    job = worker.jobs()[0]["job_id"]
    with pytest.raises(RecoveryRequired):
        worker.recover_docker_job(job)
    cli.process.returncode = 0
    cli.info["state"] = inspection(selected.profile, status="exited")["state"]
    worker.recover_docker_job(job)
    assert core.active_run_ids == () and core.runs.get(run.id).status == "unknown"
    core.close()


def test_control_output_is_bounded(monkeypatch):
    cli = DockerCLI()
    monkeypatch.setattr(cli, "spawn", lambda args: subprocess.Popen(
        [sys.executable, "-I", "-S", "-B", "-c", "import sys; sys.stdout.write('x'*65537)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
    with pytest.raises(DockerUnavailable, match="failed"):
        cli.call(["fixed-unit-fixture"])


def test_input_writer_never_blocks_supervisor_and_has_quota():
    entered, release = threading.Event(), threading.Event()
    class Blocked(io.BytesIO):
        def write(self, data):
            entered.set()
            assert release.wait(3)
            return super().write(data)
    process = Process()
    process.returncode, process.stdin = None, Blocked()
    writer = QueuedInput(process)
    writer.write(b"first")
    assert entered.wait(1)
    writer.write(b"second")
    writer.write(b"third")
    with pytest.raises(OSError, match="quota"):
        writer.write(b"fourth")
    process.returncode = 0
    release.set()
    writer.close()


def test_build_requires_approval_and_uses_only_pinned_base(tmp_path):
    class Forbidden:
        def call(self, *args, **kwargs):
            raise AssertionError("No image commands without approval")
    with pytest.raises(DockerApprovalRequired):
        build_image(Forbidden(), "not-approved", tmp_path)
    review = proposed_review()
    assert review["base"]["platform"] == "linux/amd64"
    assert review["base"]["python_version"] == "3.12.16"
    assert set(review["review"]["sources"]) == {
        "runtime.py", "wire.py", "probes.py", "school_contract.py", "python_runner.py", "app_contract.py", "app_runner.py",
    }
    recipe = (Path(__file__).resolve().parents[1] / "deployment/worker/Dockerfile").read_text()
    assert "\nRUN " not in recipe and "FROM ${BASE_IMAGE}" in recipe


@pytest.mark.parametrize("variant", ["legacy", "containerd", "wrong_id", "wrong_descriptor", "wrong_platform", "wrong_repository"])
def test_pinned_base_identity_uses_approved_digests_only(variant):
    locked = proposed_review()["base"]
    metadata = {"id": locked["config_digest"], "os": "linux", "arch": "amd64",
                "repo_digests": ["python@" + locked["platform_digest"]],
                "descriptor": {"digest": locked["platform_digest"]}}
    if variant in ("containerd", "wrong_descriptor"):
        metadata["id"] = locked["platform_digest"]
    if variant == "wrong_id":
        metadata["id"] = "sha256:" + "a" * 64
    elif variant == "wrong_descriptor":
        metadata["descriptor"]["digest"] = "sha256:" + "a" * 64
    elif variant == "wrong_platform":
        metadata["arch"] = "arm64"
    elif variant == "wrong_repository":
        metadata["repo_digests"] = ["other/project@" + locked["platform_digest"]]
    if variant in ("legacy", "containerd"):
        assert verify_base_identity(metadata, locked) == ("config-digest" if variant == "legacy" else "platform-manifest-digest")
    else:
        with pytest.raises(DockerConfigMismatch):
            verify_base_identity(metadata, locked)
