import hashlib
import os
import subprocess

import pytest

from ii_pro_core import Core, ExecutionResult, MockExecutor, ProducedArtifact, RunStatus as R, TaskStatus as T
from ii_pro_core.errors import ArtifactMismatch, UnsafePath, ValidationError
from ii_pro_core.security import MAX_ARTIFACT_BYTES
from conftest import planned


def successful(core):
    task = planned(core)
    run = core.runs.create(task.id, idempotency_key="run")
    core.runs.execute(run.id)
    return task, run, core.artifacts.list(run.id)[0]


def test_artifact_has_verified_hash_and_registration_is_idempotent(core):
    task, run, artifact = successful(core)
    path = core.artifacts.files.path(artifact.relative_path)
    assert artifact.task_id == task.id and artifact.run_id == run.id
    assert artifact.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert artifact.size == path.stat().st_size and core.artifacts.validate(artifact.id)
    assert core.artifacts.register(run.id, "result.json").id == artifact.id
    assert len(core.artifacts.list(run.id)) == 1


@pytest.mark.parametrize("change", ["modify", "remove"])
def test_missing_or_changed_artifact_blocks_verification(core, change):
    task, _, artifact = successful(core)
    path = core.artifacts.files.path(artifact.relative_path)
    if change == "modify":
        path.write_text("changed", encoding="utf-8")
    else:
        path.unlink()  # only a fixture created under pytest's temporary directory
    with pytest.raises(ArtifactMismatch):
        core.artifacts.validate(artifact.id)
    with pytest.raises(ArtifactMismatch):
        core.tasks.transition(task.id, T.VERIFIED)
    assert core.tasks.get(task.id).status == T.VERIFYING


def test_save_rechecks_artifacts_after_verification(core):
    task, _, artifact = successful(core)
    core.tasks.transition(task.id, T.VERIFIED)
    core.artifacts.files.path(artifact.relative_path).write_text("changed", encoding="utf-8")
    with pytest.raises(ArtifactMismatch):
        core.tasks.transition(task.id, T.SAVED)
    assert core.tasks.get(task.id).status == T.VERIFIED


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/outside", "C:relative", "\\\\host\\share",
                                  "a/../../escape", "./file", "a//file", "a\\file", "file:stream",
                                  "NUL", "a/CON.txt", "trailing.", "trailing "])
def test_unsafe_artifact_paths_are_rejected(core, name):
    _, run, _ = successful(core)
    with pytest.raises(UnsafePath):
        core.artifacts.register(run.id, name)


def test_executor_output_cannot_escape_workspace(tmp_path):
    class Escape(MockExecutor):
        def execute(self, request):
            return ExecutionResult((ProducedArtifact("../../outside.txt", b"invalid"),))
    with Core(tmp_path / "state", executors=[Escape()]) as core:
        task = planned(core)
        run = core.runs.create(task.id, idempotency_key="escape")
        assert core.runs.execute(run.id).error_code == "artifact_failed"
        assert not (tmp_path / "outside.txt").exists() and core.artifacts.list(run.id) == []


@pytest.mark.parametrize("variant", ["oversized", "duplicate", "binary", "credential"])
def test_invalid_executor_outputs_fail_without_success(tmp_path, variant):
    class Invalid(MockExecutor):
        def execute(self, request):
            choices = {
                "oversized": (ProducedArtifact("large.txt", b"x" * (MAX_ARTIFACT_BYTES + 1)),),
                "duplicate": (ProducedArtifact("same.txt", b"a"), ProducedArtifact("SAME.txt", b"b")),
                "binary": (ProducedArtifact("binary.bin", b"\xff\xfe"),),
                "credential": (ProducedArtifact("secret.txt", b"api_key=private-fixture"),),
            }
            return ExecutionResult(choices[variant])
    with Core(tmp_path / "state", executors=[Invalid()]) as core:
        task = planned(core)
        run = core.runs.create(task.id, idempotency_key="invalid")
        assert core.runs.execute(run.id).status == R.UNKNOWN
        assert core.artifacts.list(run.id) == []
        assert not any(e.kind == "run.succeeded" for e in core.events())


def make_directory_link(link, target):
    if os.name == "nt":
        # Trusted test setup only; never an agent/database command. Both paths
        # are generated fixtures under tmp_path, with no shell metacharacters.
        assert not any(ch in str(link) + str(target) for ch in '&|<>^%!"')
        subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, check=True, timeout=10)
    else:
        link.symlink_to(target, target_is_directory=True)


def test_windows_junction_or_symlink_cannot_escape(core, tmp_path):
    _, run, _ = successful(core)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("outside fixture")
    run_root = core.artifacts.files.root / run.task_id / run.id
    make_directory_link(run_root / "redirect", outside)
    with pytest.raises(UnsafePath):
        core.artifacts.register(run.id, "redirect/private.txt")
    assert (outside / "private.txt").read_text() == "outside fixture"


def test_junction_workspace_root_is_rejected(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    make_directory_link(state / "workspace", outside)
    with pytest.raises(UnsafePath):
        Core(state)
    assert list(outside.iterdir()) == []


def test_hardlinked_file_is_not_accepted(core, tmp_path):
    _, run, _ = successful(core)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside fixture")
    linked = core.artifacts.files.root / run.task_id / run.id / "linked.txt"
    os.link(outside, linked)
    with pytest.raises(ArtifactMismatch):
        core.artifacts.register(run.id, "linked.txt")


@pytest.mark.parametrize("text", ["sk-abcdefghijklmnopqrstuv", "Bearer private-fixture", "password=private-fixture"])
def test_credential_like_input_is_rejected_before_database_write(core, text):
    before = len(core.events())
    with pytest.raises(ValidationError):
        core.tasks.create(text, idempotency_key="secret")
    assert len(core.events()) == before


def test_executor_exception_does_not_persist_secret_message(tmp_path):
    marker = "canary-private-value-never-store"
    class Leaky(MockExecutor):
        def execute(self, request):
            raise RuntimeError(marker)
    state = tmp_path / "state"
    with Core(state, executors=[Leaky()]) as core:
        task = planned(core)
        run = core.runs.create(task.id, idempotency_key="run")
        result = core.runs.execute(run.id)
        assert result.error_type == "RuntimeError" and result.error_code == "executor_failed"
        assert marker not in str(core.events())
    assert marker.encode() not in (state / "core.sqlite3").read_bytes()
