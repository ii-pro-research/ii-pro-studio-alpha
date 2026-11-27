from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import threading
import time

import pytest

from ii_pro_core import Action, Core, RunStatus as R, TaskStatus as T
from ii_pro_core.errors import ApprovalRequired, ArtifactMismatch, Conflict, CoreBusy, OwnershipLost, RecoveryRequired, SchemaError, ValidationError
from ii_pro_core.executors import ExecutionResult, ProducedArtifact
from ii_pro_worker import DockerBackend, DockerProfile, NativeFixtureBackend, WorkerAdapter
from ii_pro_worker.backends import DockerApprovalRequired
from ii_pro_worker.supervisor import Limits, Supervisor, WorkerUncertain
from ii_pro_worker.wire import MAX_FRAME, ProtocolError, decode, encode, fingerprint, packet, read


def setup(tmp_path, limits=Limits()):
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    worker = WorkerAdapter(NativeFixtureBackend(scratch, allow_trusted_fixtures=True), limits=limits)
    core = Core(tmp_path / "state", executors=[worker])
    worker.bind(core)
    return core, worker


def planned(core, fixture="echo", arguments=None, key="task"):
    task = core.tasks.create("Fixed fixture", executor="worker", idempotency_key=key)
    core.tasks.transition(task.id, T.PLANNED)
    run = core.runs.create(task.id, idempotency_key=key + "-run",
                           action=Action.build("worker.fixed", {"fixture": fixture,
                                               "arguments": {"text": "hello"} if arguments is None else arguments}))
    return task, run


def approve(core, run):
    approval = core.approvals.request(run.id)
    core.approvals.decide(approval.id, allow=True)
    return approval.id


def wait_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition did not become true"
        time.sleep(0.01)


def request():
    return {"version": 1, "job_id": "a" * 32, "run_id": "b" * 32, "epoch": 1, "seq": 0,
            "type": "request", "payload": {"task": {"fixture": "echo", "arguments": {"text": "x"}},
            "spec_digest": "c" * 64, "max_runtime_ms": 1000}}


def test_real_fixture_success_sealed_bytes_no_path_import(tmp_path, monkeypatch):
    core, worker = setup(tmp_path)
    with core:
        def forbidden(*args, **kwargs):
            raise AssertionError("Worker must never enter filesystem importer")
        monkeypatch.setattr(core.artifacts.files, "write", forbidden)
        monkeypatch.setattr(core.artifacts.files, "fingerprint", forbidden)
        task, run = planned(core, "sum", {"values": [1, 2, -1]})
        approval = approve(core, run)
        assert core.runs.execute(run.id, approval_id=approval).status == R.SUCCEEDED
        artifacts = core.artifacts.list(run.id)
        by_name = {a.relative_path.rsplit("/", 1)[1]: a for a in artifacts}
        assert core.artifacts.read(by_name["stdout.txt"].id) == b"2"
        receipt = json.loads(core.artifacts.read(by_name["result.json"].id))
        assert receipt["stop_confirmed"] is True and receipt["process_exit_code"] == 0
        assert receipt["result"]["exit_code"] == 0
        assert core.active_run_ids == ()
        assert worker.jobs()[0]["phase"] == "exited"
        assert not list((core.store.root / "workspace").rglob("*.txt"))
        core.tasks.transition(task.id, T.VERIFIED)
        core.tasks.transition(task.id, T.SAVED)
        with pytest.raises(Conflict, match="sealed"):
            core.artifacts.register(run.id, "late.txt")
        assert core.runs.execute(run.id, approval_id=approval).status == R.SUCCEEDED
        assert len(worker.jobs()) == 1


@pytest.mark.parametrize("fixture,args,limits,reason", [
    ("sleep", {"milliseconds": 1500}, Limits(runtime_ms=150), "timeout"),
    ("raise", {}, Limits(), "worker_error_or_channel_loss"),
    ("overflow", {}, Limits(), "worker_error_or_channel_loss"),
    ("disconnect", {}, Limits(), "channel_lost"),
])
def test_real_fixture_errors_are_unknown_and_never_replayed(tmp_path, fixture, args, limits, reason):
    core, worker = setup(tmp_path, limits)
    _, run = planned(core, fixture, args)
    assert core.runs.execute(run.id, approval_id=approve(core, run)).status == R.UNKNOWN
    wait_until(lambda: not core.active_run_ids)
    job = worker.jobs()[0]
    assert json.loads(job["receipt_json"])["reason"] == reason
    assert job["stop_confirmed"] == 1
    assert core.artifacts.list(run.id) == []
    core.close()
    reopened, new_worker = setup(tmp_path)
    with reopened:
        assert reopened.runs.get(run.id).status == R.UNKNOWN
        with pytest.raises(RecoveryRequired):
            reopened.runs.execute(run.id)
        assert len(new_worker.jobs()) == 1
        reopened.runs.resolve_interrupted(run.id, confirm_stopped=True)
        with pytest.raises(RecoveryRequired):
            reopened.runs.create(run.task_id, action=Action.from_json(run.action_json), idempotency_key="retry")


def test_missing_rejected_revoked_approval_never_starts_worker(tmp_path):
    core, worker = setup(tmp_path)
    with core:
        _, run = planned(core)
        with pytest.raises(ApprovalRequired):
            core.runs.execute(run.id)
        denied = core.approvals.request(run.id)
        core.approvals.decide(denied.id, allow=False)
        with pytest.raises(ApprovalRequired):
            core.runs.execute(run.id, approval_id=denied.id)
        permit = approve(core, run)
        core.approvals.revoke(permit)
        with pytest.raises(ApprovalRequired):
            core.runs.execute(run.id, approval_id=permit)
        assert worker.jobs() == []


def test_profile_drift_invalidates_approval(tmp_path):
    core, worker = setup(tmp_path)
    _, run = planned(core)
    permit = approve(core, run)
    with pytest.raises(AttributeError):
        worker.limits = Limits(runtime_ms=2000)
    core.close()
    reopened, replacement = setup(tmp_path, Limits(runtime_ms=2000))
    with reopened:
        with pytest.raises(ApprovalRequired):
            reopened.runs.execute(run.id, approval_id=permit)
        assert replacement.jobs() == []


def test_close_during_worker_keeps_owner_then_cancels_cooperatively(tmp_path):
    core, worker = setup(tmp_path, Limits(runtime_ms=3000))
    _, run = planned(core, "sleep", {"milliseconds": 1500})
    permit = approve(core, run)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(core.runs.execute, run.id, approval_id=permit)
        wait_until(lambda: any(j["phase"] == "running" for j in worker.jobs()))
        with pytest.raises(CoreBusy):
            core.close(timeout=0.01)
        with pytest.raises(CoreBusy):
            Core(tmp_path / "state")
        with pytest.raises(CoreBusy):
            core.runs.execute(run.id, approval_id=permit)
        core.close(timeout=3, cancel=True)
        assert future.result().status == R.UNKNOWN
    with Core(tmp_path / "state") as reopened:
        assert reopened.runs.get(run.id).status == R.UNKNOWN


def test_concurrent_dispatch_preserves_single_attempt(tmp_path):
    core, worker = setup(tmp_path, Limits(runtime_ms=3000))
    with core, ThreadPoolExecutor() as pool:
        _, run = planned(core, "sleep", {"milliseconds": 200})
        permit = approve(core, run)
        first = pool.submit(core.runs.execute, run.id, approval_id=permit)
        wait_until(lambda: bool(core.active_run_ids))
        with pytest.raises(CoreBusy):
            core.runs.execute(run.id, approval_id=permit)
        assert first.result().status == R.SUCCEEDED
        assert len(core.runs.list(run.task_id)) == len(worker.jobs()) == 1


def test_native_fixture_does_not_inherit_core_import_or_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CORE_PRIVATE_FIXTURE", "not-forwarded")
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
    core, _ = setup(tmp_path)
    with core:
        _, run = planned(core, "contract", {})
        assert core.runs.execute(run.id, approval_id=approve(core, run)).status == R.SUCCEEDED
        artifact = next(a for a in core.artifacts.list(run.id) if a.relative_path.endswith("stdout.txt"))
        contract = json.loads(core.artifacts.read(artifact.id))
        assert contract["core_importable"] is False
        assert "CORE_PRIVATE_FIXTURE" not in contract["environment_keys"]
        assert "PYTHONPATH" not in contract["environment_keys"]
        assert "DOCKER_HOST" not in contract["environment_keys"]
        # Native fixtures share the host filesystem. Docker isolation is tested separately.
        assert contract["docker_socket_present"] is Path("/var/run/docker.sock").exists()


@pytest.mark.parametrize("task", [
    {"fixture": "shell", "arguments": {"command": "echo anything"}},
    {"fixture": "echo", "arguments": {"text": "x", "path": "../../state"}},
    {"fixture": "echo", "arguments": {"text": "x" * 1025}},
    {"fixture": "sum", "arguments": {"values": [True]}},
    {"fixture": "sum", "arguments": {"values": [0] * 33}},
    {"fixture": "sleep", "arguments": {"milliseconds": 1501}},
    {"fixture": "raise", "arguments": {"approve": True}},
    {"fixture": "echo", "arguments": {}, "extra": 1},
])
def test_invalid_task_refused_before_dispatch(task):
    with pytest.raises(ValidationError):
        Action.build("worker.fixed", task)


def test_wire_roundtrip_partial_reads_and_eof():
    value = request()
    class Fragmented(io.BytesIO):
        def read(self, count=-1):
            return super().read(min(count, 2))
    assert read(Fragmented(encode(value))) == value
    with pytest.raises(EOFError):
        read(io.BytesIO(encode(value)[:-1]))


@pytest.mark.parametrize("raw", [b"{}", b"{", b"\xff", b'{"x":1,"x":2}', b'{"x":NaN}',
                                      b"[" * 3000 + b"]" * 3000, b"x" * (MAX_FRAME + 1)])
def test_malformed_wire_is_rejected(raw):
    with pytest.raises(ProtocolError):
        decode(raw)


@pytest.mark.parametrize("length", [0, MAX_FRAME + 1, 2**32 - 1])
def test_bad_frame_length_rejected_without_body_read(length):
    with pytest.raises(ProtocolError):
        read(io.BytesIO(struct.pack(">I", length)))


@pytest.mark.parametrize("key,value", [("version", 2), ("epoch", True), ("epoch", 0), ("seq", -1),
                                        ("job_id", "../db"), ("type", "approve"), ("extra", 1)])
def test_protocol_closed_fields_and_types(key, value):
    item = request()
    item[key] = value
    with pytest.raises(ProtocolError):
        encode(item)


class FakeHandle:
    """No OS authority: intentionally ignores cancellation until test releases it."""
    def __init__(self, frames=b"", exit_code=None, stderr=b""):
        self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(frames), io.BytesIO(stderr)
        self.exit_code, self.cleaned = exit_code, False
        self.identity = {"fixture": "fake"}

    def poll(self):
        return self.exit_code

    def cleanup(self):
        assert self.exit_code is not None
        self.cleaned = True


def monitor(handle, value=None, limits=Limits(runtime_ms=60), cancellation=None):
    receipts, released = [], threading.Event()
    def publish(*args):
        receipts.append(args)
    def execute():
        return Supervisor(limits).run(handle, value or request(), cancellation or threading.Event(), publish, released.set)
    return execute, receipts, released


def test_ignoring_cancel_retains_lease_until_observed_exit():
    handle = FakeHandle()
    execute, receipts, released = monitor(handle, limits=Limits(runtime_ms=20, cancel_grace_ms=10))
    with pytest.raises(WorkerUncertain):
        execute()
    assert not released.is_set() and not handle.cleaned
    assert receipts[-1][0] == "unknown" and receipts[-1][3] is False
    handle.exit_code = 7
    assert released.wait(2)
    assert handle.cleaned and receipts[-1][3] is True


def test_worker_success_message_does_not_prove_process_stopped():
    value = request()
    result = {"status": "ok", "stdout": "x", "stderr": "", "exit_code": 0,
              "request_digest": fingerprint(value["payload"])}
    frames = encode(packet(value, "accepted", 0, {})) + encode(packet(value, "result", 1, result))
    handle = FakeHandle(frames)
    execute, receipts, released = monitor(handle, limits=Limits(runtime_ms=20, cancel_grace_ms=10))
    with pytest.raises(WorkerUncertain):
        execute()
    assert not released.is_set() and receipts[-1][3] is False
    assert receipts[-1][2]["result"]["status"] == "ok"
    handle.exit_code = 0
    assert released.wait(2)


@pytest.mark.parametrize("variant", ["wrong_epoch", "duplicate_seq", "result_first", "result_hash", "trailing", "oversized_stderr", "exit_mismatch"])
def test_supervisor_refuses_forged_or_inconsistent_results(variant):
    value = request()
    result = {"status": "ok", "stdout": "x", "stderr": "", "exit_code": 0,
              "request_digest": fingerprint(value["payload"])}
    frames = [packet(value, "accepted", 0, {}), packet(value, "result", 1, result)]
    if variant == "wrong_epoch":
        frames[0]["epoch"] = 2
    elif variant == "duplicate_seq":
        frames[1]["seq"] = 0
    elif variant == "result_first":
        frames = [packet(value, "result", 0, result)]
    elif variant == "result_hash":
        frames[1]["payload"]["request_digest"] = "d" * 64
    elif variant == "trailing":
        frames.append(packet(value, "heartbeat", 2, {}))
    handle = FakeHandle(b"".join(encode(f) for f in frames), 1 if variant == "exit_mismatch" else 0,
                        b"x" * 4097 if variant == "oversized_stderr" else b"")
    execute, receipts, released = monitor(handle)
    with pytest.raises(WorkerUncertain):
        execute()
    assert released.is_set() and receipts[-1][3] is True


def test_missing_heartbeat_requests_cancel():
    exited = threading.Event()
    class OpenChannel(io.BytesIO):
        def read(self, count=-1):
            value = super().read(count)
            if value:
                return value
            exited.wait(3)
            return b""
    handle = FakeHandle()
    handle.stdout = OpenChannel(encode(packet(request(), "accepted", 0, {})))
    execute, receipts, released = monitor(handle, limits=Limits(runtime_ms=500, heartbeat_ms=100, cancel_grace_ms=10))
    with pytest.raises(WorkerUncertain):
        execute()
    assert not released.is_set() and receipts[-1][2]["reason"] == "heartbeat_lost"
    handle.exit_code = 0
    exited.set()
    assert released.wait(2)


def test_external_store_lease_prevents_close_after_invocation_returns(tmp_path):
    core, _ = setup(tmp_path)
    _, run = planned(core)
    with core.store.execution(run.id):
        signal = threading.Event()
        release = core.store.retain_external_execution(run.id, signal)
    assert core.active_run_ids == (run.id,)
    with pytest.raises(CoreBusy):
        core.close(timeout=0, cancel=True)
    assert signal.is_set()
    with pytest.raises(CoreBusy):
        Core(tmp_path / "state")
    release()
    release()
    core.close()


def test_adapter_timeout_retains_core_ownership_for_ignoring_fixture(tmp_path, monkeypatch):
    core, worker = setup(tmp_path, Limits(runtime_ms=20, cancel_grace_ms=10))
    handle = FakeHandle()
    monkeypatch.setattr(NativeFixtureBackend, "start", lambda self: handle)
    _, run = planned(core)
    assert core.runs.execute(run.id, approval_id=approve(core, run)).status == R.UNKNOWN
    assert core.active_run_ids == (run.id,)
    assert worker.jobs()[0]["stop_confirmed"] == 0
    with pytest.raises(CoreBusy):
        core.close(timeout=0, cancel=True)
    with pytest.raises(CoreBusy):
        Core(tmp_path / "state")
    handle.exit_code = 7
    wait_until(lambda: not core.active_run_ids)
    assert worker.jobs()[0]["stop_confirmed"] == 1
    assert core.runs.get(run.id).status == R.UNKNOWN
    core.close()


def test_unconfirmed_old_job_quarantines_other_tasks_after_restart(tmp_path):
    core, _ = setup(tmp_path)
    _, run = planned(core)
    approve(core, run)
    with core.store.transaction() as conn:
        # Crash window: intent exists, no launch acknowledgement. Not proof of no process.
        conn.execute("INSERT INTO worker_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     ("a" * 32, run.id, core.store.generation, "b" * 64, "trusted-native-fixture",
                      "launching", None, 0, None, "fixture", "fixture"))
    core.close()
    reopened, worker = setup(tmp_path)
    with reopened:
        assert worker.jobs()[0]["phase"] == "unknown"
        _, new = planned(reopened, key="new")
        assert reopened.runs.execute(new.id, approval_id=approve(reopened, new)).status == R.FAILED
        assert len(worker.jobs()) == 1
        assert reopened.runs.get(new.id).error_type == "RecoveryRequired"


@pytest.mark.parametrize("change", ["missing_guard", "altered_guard", "missing_index"])
def test_schema_drift_fail_closed_with_valid_ledger(tmp_path, change):
    state = tmp_path / "state"
    with Core(state):
        pass
    with sqlite3.connect(state / "core.sqlite3") as db:
        if change in {"missing_guard", "altered_guard"}:
            db.execute("DROP TRIGGER events_no_delete")
            if change == "altered_guard":
                db.execute("CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT 1; END")
        else:
            db.execute("DROP INDEX one_active_run")
    with pytest.raises(SchemaError):
        Core(state)


def test_seal_detects_metadata_manifest_change_and_blocks_blob_mutation(tmp_path):
    core, _ = setup(tmp_path)
    with core:
        task, run = planned(core)
        core.runs.execute(run.id, approval_id=approve(core, run))
        artifact = core.artifacts.list(run.id)[0]
        with pytest.raises(sqlite3.IntegrityError):
            with core.store.transaction() as conn:
                conn.execute("UPDATE artifact_blobs SET content=? WHERE artifact_id=?", (b"changed", artifact.id))
        with pytest.raises(sqlite3.IntegrityError):
            with core.store.transaction() as conn:
                conn.execute("UPDATE artifacts SET relative_path=? WHERE id=?", ("db/changed", artifact.id))
        # Simulated hostile DB administrator bypass, only inside this test DB.
        with core.store.transaction() as conn:
            conn.execute("DROP TRIGGER sealed_artifact_no_update")
            conn.execute("UPDATE artifacts SET relative_path=? WHERE id=?", ("db/changed", artifact.id))
        with pytest.raises(ArtifactMismatch, match="manifest"):
            core.artifacts.read(artifact.id)
        with pytest.raises(ArtifactMismatch):
            core.tasks.transition(task.id, T.VERIFIED)


def test_core_publication_fault_rolls_back_entire_worker_artifact_set(tmp_path):
    core, worker = setup(tmp_path)
    with core:
        _, run = planned(core)
        with core.store.transaction() as conn:
            conn.execute("""CREATE TRIGGER fixture_blob_failure BEFORE INSERT ON artifact_blobs
                WHEN (SELECT relative_path FROM artifacts WHERE id=NEW.artifact_id) LIKE '%stderr.txt'
                BEGIN SELECT RAISE(ABORT,'fixture'); END""")
        assert core.runs.execute(run.id, approval_id=approve(core, run)).status == R.UNKNOWN
        assert core.runs.get(run.id).error_code == "artifact_failed"
        assert core.artifacts.list(run.id) == []
        assert core.store.one("SELECT COUNT(*) FROM artifact_blobs")[0] == 0
        assert core.store.one("SELECT COUNT(*) FROM artifact_seals")[0] == 0
        assert not any(e.kind == "artifact.registered" for e in core.events(run.task_id))
        assert worker.jobs()[0]["stop_confirmed"] == 1


def test_stale_owner_cannot_publish_worker_receipt_or_artifacts(tmp_path):
    from ii_pro_core.storage import utc_now
    core, worker = setup(tmp_path, Limits(runtime_ms=3000))
    _, run = planned(core, "sleep", {"milliseconds": 250})
    permit = approve(core, run)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(core.runs.execute, run.id, approval_id=permit)
        wait_until(lambda: any(j["phase"] == "running" for j in worker.jobs()))
        with sqlite3.connect(core.store.path) as conn:
            conn.execute("INSERT INTO owner_sessions(owner_id,opened_at) VALUES(?,?)", ("fixture-new-owner", utc_now()))
        with pytest.raises(OwnershipLost):
            future.result(timeout=4)
    wait_until(lambda: not core.active_run_ids)
    assert core.artifacts.list(run.id) == []
    assert worker.jobs()[0]["stop_confirmed"] == 0
    core.close()
    reopened, new_worker = setup(tmp_path)
    with reopened:
        assert reopened.runs.get(run.id).status == R.UNKNOWN
        assert new_worker.jobs()[0]["phase"] == "unknown"


def test_attempt_history_survives_preflight_failure_and_explicit_new_approval(tmp_path):
    core, worker = setup(tmp_path)
    with core:
        task, first = planned(core)
        # A readiness failure guarantees execute was not called.
        scratch = worker.backend.scratch_root
        held = scratch.with_name("held-scratch")
        scratch.rename(held)
        assert core.runs.execute(first.id, approval_id=approve(core, first)).status == R.FAILED
        assert worker.jobs() == []
        held.rename(scratch)
        core.tasks.transition(task.id, T.PLANNED)
        second = core.runs.create(task.id, action=Action.from_json(first.action_json), idempotency_key="retry")
        with pytest.raises(ApprovalRequired):
            core.runs.execute(second.id)
        assert core.runs.execute(second.id, approval_id=approve(core, second)).status == R.SUCCEEDED
        attempts = core.runs.list(task.id)
        assert [(r.attempt, r.status) for r in attempts] == [(1, R.FAILED), (2, R.SUCCEEDED)]
        assert len(worker.jobs()) == 1


@pytest.mark.parametrize("artifacts", [
    tuple(ProducedArtifact(str(i), b"x") for i in range(9)),
    (ProducedArtifact("../outside", b"x"),),
    (ProducedArtifact("A.txt", b"x"),),
    (ProducedArtifact("a.txt", b"x"), ProducedArtifact("a.txt", b"y")),
    (ProducedArtifact("x", b"x" * 32769),),
    tuple(ProducedArtifact(str(i), b"x" * 30000) for i in range(3)),
    (ProducedArtifact("a", b"ok"), ProducedArtifact("b", b"\xff")),
])
def test_blob_quota_names_and_atomic_publication(tmp_path, artifacts):
    core, _ = setup(tmp_path)
    with core:
        _, run = planned(core)
        # Exercise publication in a rollback savepoint, including late invalid UTF-8.
        with core.store.transaction() as conn:
            row = core.store.one("SELECT * FROM runs WHERE id=?", (run.id,), conn=conn)
            conn.execute("SAVEPOINT fixture")
            with pytest.raises((ValidationError, UnicodeError)):
                core.artifacts.import_blobs(conn, row, ExecutionResult(artifacts, "sqlite"))
            conn.execute("ROLLBACK TO fixture")
            conn.execute("RELEASE fixture")
        assert core.artifacts.list(run.id) == []
        assert core.store.one("SELECT COUNT(*) FROM artifact_seals")[0] == 0


@pytest.mark.parametrize("sizes", [(32767,), (32768,), (32768, 32768), (8192,) * 8])
def test_blob_quota_boundaries_allow_valid_bounded_content(tmp_path, sizes):
    core, _ = setup(tmp_path)
    with core:
        _, run = planned(core)
        output = ExecutionResult(tuple(ProducedArtifact(str(i), b"x" * n) for i, n in enumerate(sizes)), "sqlite")
        with core.store.transaction() as conn:
            row = core.store.one("SELECT * FROM runs WHERE id=?", (run.id,), conn=conn)
            core.artifacts.import_blobs(conn, row, output)
        core.artifacts.validate_run(run.id)
        assert sum(a.size for a in core.artifacts.list(run.id)) == sum(sizes)


def test_wire_result_size_and_contradictory_exit_code_refused():
    value = request()
    result = {"status": "ok", "stdout": "x" * 4096, "stderr": "", "exit_code": 0,
              "request_digest": fingerprint(value["payload"])}
    assert read(io.BytesIO(encode(packet(value, "result", 1, result))))["payload"]["stdout"] == "x" * 4096
    result["stdout"] += "x"
    with pytest.raises(ProtocolError):
        encode(packet(value, "result", 1, result))
    result["stdout"], result["exit_code"] = "", 1
    with pytest.raises(ProtocolError):
        encode(packet(value, "result", 1, result))


def test_docker_profile_is_closed_and_launch_is_disabled(tmp_path, monkeypatch):
    profile = DockerProfile("sha256:" + "a" * 64)  # synthetic test image, never launched
    argv = profile.create_argv("b" * 32, "c" * 32, 1)
    assert argv[:5] == ["--context", "desktop-linux", "create", "--pull", "never"]
    for pair in (("--network", "none"), ("--user", "65532:65532"), ("--cap-drop", "ALL"),
                 ("--memory", "128m"), ("--pids-limit", "16"), ("--log-driver", "none")):
        assert argv[argv.index(pair[0]) + 1] == pair[1]
    for forbidden in ("--privileged", "--use-api-socket", "--volume", "--mount", "--publish", "--env", "--pid", "--device", "--rm"):
        assert forbidden not in argv
    assert "--read-only" in argv and "--init" in argv
    assert profile.describe()["mounts"] == []
    def forbidden(*args, **kwargs):
        raise AssertionError("Docker approval gate must perform no CLI calls")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    backend = DockerBackend(profile)
    with pytest.raises(DockerApprovalRequired):
        backend.prepare()
    with pytest.raises(DockerApprovalRequired):
        backend.start()
    assert backend.configuration()["launch_enabled"] is False


@pytest.mark.parametrize("image,options", [("python:latest", {}), ("sha256:" + "a" * 64, {"context": "default"}),
                                            ("sha256:" + "a" * 64, {"memory": "1g"})])
def test_docker_profile_rejects_unpinned_or_modified_resources(image, options):
    with pytest.raises(ValueError):
        DockerProfile(image, **options)


def test_native_backend_cannot_be_enabled_accidentally(tmp_path):
    with pytest.raises(ValueError):
        NativeFixtureBackend(tmp_path)


def test_actual_worker_rejects_invalid_frame_before_acceptance(tmp_path):
    backend = NativeFixtureBackend(tmp_path, allow_trusted_fixtures=True)
    backend.prepare()
    handle = backend.start()
    stdout, stderr = handle.process.communicate(struct.pack(">I", MAX_FRAME + 1), timeout=3)
    assert handle.poll() != 0 and stdout == b""
    assert len(stderr) <= 4096
    assert b"Invalid frame length" in stderr
    handle.cleanup()


def test_owner_crash_leaves_unknown_and_blocks_worker_redispatch(tmp_path):
    # The owner exits itself; no host kill, no untrusted code and no Docker.
    root = Path(__file__).resolve().parents[1]
    source = '''
import json, os, sys, threading, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from ii_pro_core import Core, Action, TaskStatus
from ii_pro_worker import WorkerAdapter, NativeFixtureBackend
from ii_pro_worker.supervisor import Limits
base=Path(sys.argv[2]); (base/'scratch').mkdir()
w=WorkerAdapter(NativeFixtureBackend(base/'scratch', allow_trusted_fixtures=True), limits=Limits(runtime_ms=3000))
c=Core(base/'state', executors=[w]); w.bind(c)
t=c.tasks.create('Crash fixture',executor='worker',idempotency_key='crash')
c.tasks.transition(t.id,TaskStatus.PLANNED)
r=c.runs.create(t.id,idempotency_key='crash-run',action=Action.build('worker.fixed',{'fixture':'sleep','arguments':{'milliseconds':1500}}))
a=c.approvals.request(r.id); c.approvals.decide(a.id,allow=True)
threading.Thread(target=lambda:c.runs.execute(r.id,approval_id=a.id),daemon=True).start()
deadline=time.monotonic()+5
while not any(j['phase']=='running' and j['receipt_json'] and json.loads(j['receipt_json'])['accepted'] for j in w.jobs()):
    if time.monotonic()>deadline: os._exit(2)
    time.sleep(.01)
os._exit(19)
'''
    process = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", source, str(root), str(tmp_path)],
                             capture_output=True, timeout=8)
    assert process.returncode == 19, process.stderr.decode(errors="replace")
    core, worker = setup(tmp_path)
    with core:
        run = core.store.one("SELECT id FROM runs WHERE idempotency_key='crash-run'")
        assert core.runs.get(run[0]).status == R.UNKNOWN
        assert worker.jobs()[0]["stop_confirmed"] == 0
        with pytest.raises(RecoveryRequired):
            core.runs.execute(run[0])
        assert len(core.runs.list(core.runs.get(run[0]).task_id)) == 1
    # The finite child sees channel EOF and exits; no stop claim is inferred from PID absence.
    time.sleep(0.2)
