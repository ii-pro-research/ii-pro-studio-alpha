"""Live acceptance only. No builds/pulls, global cleanup or generated code."""
from concurrent.futures import ThreadPoolExecutor
import errno
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from ii_pro_core import Action, Core, TaskStatus
from ii_pro_core.errors import CoreBusy, RecoveryRequired
from ii_pro_worker import DockerBackend, WorkerAdapter
from ii_pro_worker.docker_cli import DockerCLI
from ii_pro_worker.errors import DockerConfigMismatch, DockerUnavailable
from ii_pro_worker.supervisor import Limits


def open_core(path, settings, limits=None):
    profile, review = settings
    worker = WorkerAdapter(DockerBackend(profile, approved_review=review),
                           limits=limits or Limits(runtime_ms=5000, heartbeat_ms=2000, cancel_grace_ms=500))
    core = Core(path, executors=[worker], shutdown_timeout=10)
    worker.bind(core)
    return core, worker


def prepare(core, fixture, args=None, key="task"):
    task = core.tasks.create("Approved Docker acceptance fixture", executor="worker", idempotency_key=key)
    core.tasks.transition(task.id, TaskStatus.PLANNED)
    run = core.runs.create(task.id, action=Action.build("worker.fixed", {"fixture": fixture, "arguments": args or {}}), idempotency_key=key + "-run")
    approval = core.approvals.request(run.id)
    core.approvals.decide(approval.id, allow=True)  # operator-authorized finite acceptance matrix only
    return run, approval.id


def settled(core, timeout=15):
    deadline = time.monotonic() + timeout
    while core.active_run_ids:
        assert time.monotonic() < deadline, "Container stop remains unconfirmed; do not delete or restart"
        time.sleep(0.05)


def evidence(core, worker, directory):
    settled(core)
    records = worker.jobs()
    for job in records:
        if not job["stop_confirmed"]:
            worker.recover_docker_job(job["job_id"])
    records = worker.jobs()
    directory.joinpath("jobs.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    for job in records:
        worker.cleanup_docker_job(job["job_id"])


@pytest.fixture
def live(tmp_path, docker_settings):
    core, worker = open_core(tmp_path / "state", docker_settings)
    try:
        yield core, worker
    finally:
        evidence(core, worker, tmp_path)
        core.close()


def output(core, run_id):
    artifact = next(a for a in core.artifacts.list(run_id) if a.relative_path.endswith("/stdout.txt"))
    return json.loads(core.artifacts.read(artifact.id))


def test_actual_smoke_identity_and_limits(live):
    core, worker = live
    run, permit = prepare(core, "smoke")
    assert core.runs.execute(run.id, approval_id=permit).status == "succeeded"
    value = output(core, run.id)
    assert value["python"] == "3.12.16"
    assert value["effective_uid"] == value["effective_gid"] == 65532
    assert value["resources"]["memory.max"] == "134217728"
    assert value["resources"]["pids.max"] == "16"
    assert value["resources"]["cpu.max"].split() == ["50000", "100000"]
    receipt = json.loads(worker.jobs()[0]["receipt_json"])
    observation = receipt["backend_observations"]["container"]
    assert observation["state"]["Running"] is False and observation["state"]["ExitCode"] == 0
    assert observation["memory"] == 134217728 and observation["nano_cpus"] == 500000000
    assert observation["user"] == "65532:65532" and observation["read_only"]


def test_actual_network_filesystem_and_secret_boundary(live, monkeypatch):
    core, worker = live
    monkeypatch.setenv("CORE_PRIVATE_CANARY", "local-test-value-never-forwarded")
    (core.store.root / "core-private-canary.txt").write_text("fixture only", encoding="utf-8")
    run, permit = prepare(core, "isolation")
    assert core.runs.execute(run.id, approval_id=permit).status == "succeeded"
    value = output(core, run.id)
    assert value["network"]["interfaces"] == ["lo"] and value["network_unreachable"]
    assert value["root_write_errno"] in (errno.EROFS, errno.EACCES) and value["root_mount_read_only"]
    assert value["tmpfs_write_ok"]
    assert value["process_security"] == {"CapEff": "0000000000000000", "NoNewPrivs": "1", "Seccomp": "2"}
    assert not value["core_importable"] and not value["core_canary_visible"] and not value["docker_socket_present"]
    observation = json.loads(worker.jobs()[0]["receipt_json"])["backend_observations"]["container"]
    assert not any(m["Type"] != "tmpfs" for m in observation["mounts"])
    assert observation["network"] == "none"
    assert "CORE_PRIVATE_CANARY" not in observation["environment_keys"]


def test_actual_memory_headroom(live):
    core, worker = live
    run, permit = prepare(core, "memory")
    assert core.runs.execute(run.id, approval_id=permit).status == "succeeded"
    value = output(core, run.id)
    assert value["allocation_bytes"] == 8 * 1024 * 1024
    assert 8 * 1024 * 1024 < int(value["resources"]["memory.peak"]) <= 134217728
    assert not json.loads(worker.jobs()[0]["receipt_json"])["backend_observations"]["container"]["state"]["OOMKilled"]


def test_actual_timeout_is_observed_cancel_not_inferred_stop(tmp_path, docker_settings):
    core, worker = open_core(tmp_path / "state", docker_settings, Limits(runtime_ms=500, heartbeat_ms=1000, cancel_grace_ms=500))
    try:
        run, permit = prepare(core, "sleep", {"milliseconds": 1500})
        assert core.runs.execute(run.id, approval_id=permit).status == "unknown"
        settled(core)
        receipt = json.loads(worker.jobs()[0]["receipt_json"])
        assert receipt["reason"] == "timeout" and receipt["stop_confirmed"]
        assert receipt["backend_observations"]["container"]["state"]["ExitCode"] == 2
        assert core.artifacts.list(run.id) == []
    finally:
        evidence(core, worker, tmp_path)
        core.close()


@pytest.mark.parametrize("fixture,code", [("raise", 1), ("overflow", 3)])
def test_actual_worker_error_retains_unknown_and_receipt(live, fixture, code):
    core, worker = live
    run, permit = prepare(core, fixture)
    assert core.runs.execute(run.id, approval_id=permit).status == "unknown"
    settled(core)
    receipt = json.loads(worker.jobs()[0]["receipt_json"])
    assert receipt["result"]["exit_code"] == code
    assert receipt["backend_observations"]["container"]["state"]["ExitCode"] == code
    assert len(receipt["result"]["stdout"].encode()) <= 4096


def test_actual_unknown_survives_reopen(tmp_path, docker_settings):
    core, worker = open_core(tmp_path / "state", docker_settings)
    run, permit = prepare(core, "disconnect")
    assert core.runs.execute(run.id, approval_id=permit).status == "unknown"
    settled(core)
    core.close()
    reopened, replacement = open_core(tmp_path / "state", docker_settings)
    try:
        assert reopened.runs.get(run.id).status == "unknown"
        with pytest.raises(RecoveryRequired):
            reopened.runs.execute(run.id)
        assert len(replacement.jobs()) == len(reopened.runs.list(run.task_id)) == 1
    finally:
        evidence(reopened, replacement, tmp_path)
        reopened.close()


def test_actual_close_drain_preserves_owner(tmp_path, docker_settings):
    core, worker = open_core(tmp_path / "state", docker_settings)
    run, permit = prepare(core, "sleep", {"milliseconds": 1500})
    with ThreadPoolExecutor() as pool:
        future = pool.submit(core.runs.execute, run.id, approval_id=permit)
        deadline = time.monotonic() + 8
        while not any(j["receipt_json"] and json.loads(j["receipt_json"]).get("accepted") for j in worker.jobs()):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        with pytest.raises(CoreBusy):
            core.close(timeout=0, cancel=True)
        with pytest.raises(CoreBusy):
            Core(tmp_path / "state")
        assert future.result(timeout=10).status == "unknown"
    evidence(core, worker, tmp_path)
    core.close()


def test_actual_crashed_owner_recovery_never_replays(tmp_path, docker_settings):
    profile, review = docker_settings
    root = Path(__file__).resolve().parents[1]
    source = '''
import json,os,sys,threading,time
sys.path.insert(0,sys.argv[1])
from ii_pro_core import Action,Core,TaskStatus
from ii_pro_worker import DockerBackend,DockerProfile,WorkerAdapter
from ii_pro_worker.supervisor import Limits
w=WorkerAdapter(DockerBackend(DockerProfile(sys.argv[3]),approved_review=sys.argv[4]),limits=Limits(runtime_ms=5000,heartbeat_ms=2000,cancel_grace_ms=500))
c=Core(sys.argv[2],executors=[w]); w.bind(c)
t=c.tasks.create('Crash acceptance',executor='worker',idempotency_key='task'); c.tasks.transition(t.id,TaskStatus.PLANNED)
r=c.runs.create(t.id,idempotency_key='run',action=Action.build('worker.fixed',{'fixture':'sleep','arguments':{'milliseconds':1500}}))
a=c.approvals.request(r.id);c.approvals.decide(a.id,allow=True)
threading.Thread(target=lambda:c.runs.execute(r.id,approval_id=a.id),daemon=True).start()
deadline=time.monotonic()+15
while not any(j['receipt_json'] and json.loads(j['receipt_json']).get('accepted') for j in w.jobs()):
    if time.monotonic()>deadline: os._exit(2)
    time.sleep(.02)
os._exit(19)
'''
    child = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", source, str(root), str(tmp_path / "state"), profile.image, review],
                           capture_output=True, timeout=20)
    assert child.returncode == 19, child.stderr.decode(errors="replace")
    core, worker = open_core(tmp_path / "state", docker_settings)
    try:
        job = worker.jobs()[0]
        assert core.runs.get(job["run_id"]).status == "unknown" and not job["stop_confirmed"]
        with pytest.raises(RecoveryRequired):
            core.runs.execute(job["run_id"])
        deadline = time.monotonic() + 15
        while True:
            try:
                receipt = worker.recover_docker_job(job["job_id"])
                break
            except DockerConfigMismatch:
                assert time.monotonic() < deadline
                time.sleep(0.1)
        assert receipt["stop_confirmed"] and core.runs.get(job["run_id"]).status == "unknown"
        assert len(worker.jobs()) == 1
    finally:
        evidence(core, worker, tmp_path)
        core.close()


def test_real_cli_unavailable_context_fails_closed(docker_settings):
    # Do not stop the real daemon to simulate unavailability.
    with pytest.raises(DockerUnavailable):
        DockerCLI().call(["--context", "ii-pro-v021-deliberately-missing-context", "info"])
