import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

from ii_pro_core import Action
from ii_pro_core.errors import ValidationError
from ii_pro_worker.profile import DockerProfile
from ii_pro_worker.wire import MAX_FRAME, ProtocolError, encode, read


def request(fixture):
    return {"version": 1, "job_id": "a" * 32, "run_id": "b" * 32, "epoch": 1, "seq": 0, "type": "request",
            "payload": {"task": {"fixture": fixture, "arguments": {}}, "spec_digest": "c" * 64, "max_runtime_ms": 5000}}


@pytest.mark.parametrize("fixture", ["malformed", "oversized"])
def test_finite_protocol_fault_rejected_even_with_process_exit_zero(fixture):
    runtime = Path(__file__).resolve().parents[1] / "ii_pro_worker/runtime.py"
    result = subprocess.run([sys.executable, "-I", "-S", "-B", str(runtime)], input=encode(request(fixture)),
                            capture_output=True, timeout=4)
    assert result.returncode == 0 and result.stderr == b""
    assert len(result.stdout) <= MAX_FRAME + 1024
    stream = io.BytesIO(result.stdout)
    assert read(stream)["type"] == "accepted"
    with pytest.raises(ProtocolError):
        read(stream)


@pytest.mark.parametrize("fixture", ["malformed", "oversized", "bounded_wait"])
def test_stage2_fixtures_have_no_caller_controlled_program_or_arguments(fixture):
    assert Action.build("worker.fixed", {"fixture": fixture, "arguments": {}}).requires_approval
    with pytest.raises(ValidationError):
        Action.build("worker.fixed", {"fixture": fixture, "arguments": {"command": "arbitrary"}})


def test_isolation_resource_profile_matches_first_approved_smoke():
    root = Path(__file__).resolve().parents[1]
    previous = json.loads((root / "deployment/worker/docker-profile.review.json").read_bytes())["review"]["resources"]
    current = DockerProfile("sha256:" + "0" * 64).describe()
    current.pop("image")
    assert current == previous
