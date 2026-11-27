"""Only ordinary permissions and finite errors authorized for stage two."""
import errno
import io
import json
from pathlib import Path
import sqlite3
import struct

import pytest

from ii_pro_core import Action
from ii_pro_core.errors import RecoveryRequired
from ii_pro_worker.supervisor import Limits
from ii_pro_worker.wire import MAX_FRAME, ProtocolError, read
from stage2_support import Case


@pytest.fixture
def case(tmp_path, docker_settings):
    selected = Case(tmp_path, docker_settings)
    try:
        yield selected
    finally:
        selected.finish()


def frames(path):
    stream, values = io.BytesIO(path.read_bytes()), []
    while stream.tell() < len(stream.getvalue()):
        values.append(read(stream))
    return values


def test_success_saved_sealed_and_reopen_does_not_execute_again(case):
    run = case.run("smoke")
    assert run.status == "succeeded" and run.execution_stopped
    value = case.result(run.id)
    assert value["python"] == "3.12.16" and value["effective_uid"] == value["effective_gid"] == 65532
    assert value["resources"]["cpu.max"].split() == ["50000", "100000"]
    assert value["resources"]["memory.max"] == "134217728" and value["resources"]["pids.max"] == "16"
    assert int(value["resources"]["memory.peak"]) < 134217728
    assert 0 < int(value["resources"]["pids.current"]) <= 16
    assert len(case.core.artifacts.list(run.id)) == 3
    assert case.core.store.one("SELECT COUNT(*) FROM artifact_seals")[0] == 1
    case.reopen()
    assert case.core.runs.execute(run.id).status == "succeeded"
    assert len(case.worker.jobs()) == len(case.core.runs.list(run.task_id)) == 1


def test_ordinary_filesystem_network_data_and_device_boundaries(case, monkeypatch):
    # Synthetic fixtures only; no real user secrets or School DB read by Worker.
    root = Path(__file__).resolve().parents[1] / ".local/worker-v021-tests/stage2-canary"
    assert (root / "core.sqlite3").is_file() and (root / "private-canary.txt").is_file()
    with sqlite3.connect((root / "core.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        assert db.execute("SELECT COUNT(*) FROM canary").fetchone()[0] == 1
    monkeypatch.setenv("CORE_PRIVATE_CANARY", "synthetic-test-marker-never-forwarded")
    run = case.run("isolation")
    assert run.status == "succeeded"
    value = case.result(run.id)
    assert value["root_mount_read_only"] and value["root_write_errno"] in (errno.EROFS, errno.EACCES)
    assert value["tmpfs_write_ok"]
    assert value["network"]["interfaces"] == ["lo"] and value["network_unreachable"]
    assert value["external_ipv4_routes"] == value["external_ipv6_routes"] == []
    assert value["process_security"] == {"CapEff": "0000000000000000", "NoNewPrivs": "1", "Seccomp": "2"}
    assert not value["core_importable"] and not value["core_canary_visible"]
    assert not value["docker_socket_present"] and not value["gpu_devices"] and not value["dri_present"]
    assert not value["core_environment_keys"] and all(not p["readable"] for p in value["host_canaries"].values())
    observation = case.receipt()["backend_observations"]["container"]
    assert not observation["binds"] and not observation["volumes"] and observation["mounts"] == []
    assert not observation["devices"] and not observation["device_requests"] and observation["runtime"] == "runc"
    assert observation["user"] == "65532:65532" and observation["network"] == "none"
    assert observation["nano_cpus"] == 500000000
    assert observation["memory"] == observation["memory_swap"] == 134217728 and observation["pids_limit"] == 16
    assert "CORE_PRIVATE_CANARY" not in observation["environment_keys"]


@pytest.mark.parametrize("fixture,code,reason", [
    ("raise", 1, "worker_error_or_channel_loss"),
    ("overflow", 3, "worker_error_or_channel_loss"),
    ("malformed", 0, "protocol_error"),
    ("oversized", 0, "protocol_error"),
])
def test_finite_errors_remain_unknown_across_reopen(case, fixture, code, reason):
    run = case.run(fixture)
    assert run.status == "unknown" and run.effect_status == "unknown"
    assert case.core.artifacts.list(run.id) == []
    receipt = case.receipt()
    assert receipt["reason"] == reason and receipt["stop_confirmed"]
    assert receipt["backend_observations"]["container"]["state"]["ExitCode"] == code
    cid = json.loads(case.worker.jobs()[0]["handle_json"])["container_id"]
    raw = case.directory / "wire" / (cid + "-stdout.bin")
    assert len(raw.read_bytes()) <= 65536
    if fixture == "malformed":
        stream = io.BytesIO(raw.read_bytes())
        assert read(stream)["type"] == "accepted"
        while stream.tell() < len(raw.read_bytes()):
            body = stream.read(struct.unpack(">I", stream.read(4))[0])
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                break
            assert parsed["type"] == "heartbeat"
        else:
            pytest.fail("No malformed JSON frame was observed")
    elif fixture == "oversized":
        stream = io.BytesIO(raw.read_bytes())
        assert read(stream)["type"] == "accepted"
        while stream.tell() < len(raw.read_bytes()):
            position = stream.tell()
            length = struct.unpack(">I", stream.read(4))[0]
            stream.seek(position)
            if length > MAX_FRAME:
                assert length == MAX_FRAME + 1
                with pytest.raises(ProtocolError):
                    read(stream)
                break
            assert read(stream)["type"] == "heartbeat"
        else:
            pytest.fail("No oversized frame was observed")
    else:
        terminal = frames(raw)[-1]
        assert terminal["type"] == "result" and terminal["payload"]["exit_code"] == code
    case.reopen()
    assert case.core.runs.get(run.id).status == "unknown"
    with pytest.raises(RecoveryRequired):
        case.core.runs.execute(run.id)
    case.core.runs.resolve_interrupted(run.id, confirm_stopped=True)
    with pytest.raises(RecoveryRequired):
        case.core.runs.create(run.task_id, action=Action.from_json(run.action_json), idempotency_key="forbidden-retry")
    assert len(case.worker.jobs()) == len(case.core.runs.list(run.task_id)) == 1


def test_timeout_cancels_only_a_fixed_three_second_task(tmp_path, docker_settings):
    selected = Case(tmp_path, docker_settings, limits=Limits(runtime_ms=2000, heartbeat_ms=2000, cancel_grace_ms=500))
    try:
        run = selected.run("bounded_wait")
        assert run.status == "unknown"
        receipt = selected.receipt()
        assert receipt["reason"] == "timeout" and receipt["accepted"] and receipt["stop_confirmed"]
        assert receipt["backend_observations"]["container"]["state"]["ExitCode"] == 2
        cid = json.loads(selected.worker.jobs()[0]["handle_json"])["container_id"]
        terminal = frames(selected.directory / "wire" / (cid + "-stdout.bin"))[-1]
        assert terminal["payload"]["status"] == "cancelled"
        selected.reopen()
        with pytest.raises(RecoveryRequired):
            selected.core.runs.execute(run.id)
        assert len(selected.worker.jobs()) == 1
    finally:
        selected.finish()
