"""Full School→Core→Supervisor→Docker route, developer constants ONLY."""
from dataclasses import asdict
import json
import shutil
import sqlite3

import pytest

from ii_pro_core.errors import ArtifactMismatch, RecoveryRequired, ValidationError
from ii_pro_school import ModelProfile, School, StudentAdapter
from ii_pro_school.functional_curriculum import CORRECT, developer_sources
from ii_pro_school.mentors import MentorFeedback
from ii_pro_worker import DockerBackend, WorkerAdapter
from ii_pro_worker.supervisor import Limits
from stage2_support import LoggedCLI


class DeveloperMetadata(StudentAdapter):
    revision = "readiness-offline-metadata-v1"

    def __init__(self):
        super().__init__(ModelProfile.build("developer-fixture", "d" * 64))

    def approval_configuration(self):
        return {"profile": self.profile.id, "real_inference": False}

    def probe(self):
        return {"backend": "developer_fixture", "real_inference": False}

    def respond(self, prompt):
        raise AssertionError("No Qwen/Ollama call authorized")


class Laboratory:
    def __init__(self, path, settings):
        self.path, self.settings = path, settings
        self.cli = LoggedCLI(path / "wire")
        self.open()

    def open(self):
        profile, review = self.settings
        worker = WorkerAdapter(DockerBackend(profile, approved_review=review, cli=self.cli),
            limits=Limits(runtime_ms=5000, heartbeat_ms=2000, cancel_grace_ms=500), python_sources=developer_sources())
        self.school = School(self.path / "state", DeveloperMetadata(), worker=worker)
        self.worker, self.functional = worker, self.school.functional
        self.functional.install()
        self.school.register_student("developer", "Known developer fixtures; not Qwen")

    def evaluate(self, submission):
        package = self.functional.prepare(submission)
        run = self.functional.prepare_run(package)
        approval = self.school.core.approvals.request(run.id)
        (self.path / (package + "-operation.json")).write_text(json.dumps(self.school.core.approvals.describe(approval.id), indent=2), encoding="utf-8")
        self.school.core.approvals.decide(approval.id, allow=True)  # fixed developer tests authorized by user
        assert self.functional.execute(package, approval_id=approval.id).status == "succeeded"
        result = self.functional.collect(package)
        assert self.functional.collect(package) == result
        assert self.functional.execute(package, approval_id=approval.id).status == "succeeded"  # replay, no dispatch
        self.snapshot()
        return result

    def snapshot(self):
        core = self.school.core
        records = self.worker.jobs()
        (self.path / "jobs.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
        (self.path / "events.json").write_text(json.dumps([asdict(e) for e in core.events()], indent=2), encoding="utf-8")
        for job in records:
            assert job["stop_confirmed"]
            observation = self.worker.backend.stopped_observation(json.loads(job["handle_json"]))
            (self.path / (job["job_id"] + "-inspect.json")).write_text(json.dumps(observation, indent=2), encoding="utf-8")
            for artifact in core.artifacts.list(job["run_id"]):
                name = artifact.relative_path.rsplit("/", 1)[-1]
                (self.path / (job["job_id"] + "-" + name)).write_bytes(core.artifacts.read(artifact.id))
        self.cli.flush_audit()

    def close(self):
        try:
            self.snapshot()
        finally:
            self.school.close()

    def reopen(self):
        self.close()
        self.open()


@pytest.fixture
def laboratory(tmp_path, docker_settings):
    value = Laboratory(tmp_path, docker_settings)
    try:
        yield value
    finally:
        value.close()


def test_measured_before_practice_feedback_repeat_and_independent_control(laboratory):
    lab = laboratory
    cycle = lab.functional.create_cycle("developer")
    baseline = lab.functional.submit_developer_fixture(cycle, "sum-positive-baseline-v1", "overwrite_bug")
    before = lab.evaluate(baseline)
    assert before["passed"] == 3 and before["total"] == 6 and before["functional_awarded"] == 35
    practice = lab.functional.submit_developer_fixture(cycle, "sum-positive-practice-v1", "overwrite_bug")
    first = lab.evaluate(practice)
    assert first["passed"] == 2 and first["functional_awarded"] == 23
    lab.functional.add_feedback(practice, MentorFeedback("gpt_engineer", "Developer readiness fixture author",
        "Manual test note: add each positive element to total rather than overwrite it. No independent GPT review occurred."))
    retry = lab.functional.submit_developer_fixture(cycle, "sum-positive-practice-v1", "correct")
    assert lab.evaluate(retry)["passed"] == 6
    control = lab.functional.submit_developer_fixture(cycle, "sum-positive-control-v1", "correct")
    assert lab.evaluate(control)["functional_awarded"] == 70
    comparison = lab.functional.comparison(cycle)
    assert comparison["delta_points"] == 35 and comparison["developer_fixture_simulation"]
    exported = lab.functional.export_cycle(cycle)
    assert len(exported["attempts"]) == 4 and exported["attempts"][-1]["combined_awarded"] == 100
    assert all("cases" not in (a["functional"] or {}) for a in exported["attempts"])
    assert not exported["mentor_connected"] and not exported["verified_knowledge"]
    (lab.path / "cycle-export.json").write_text(json.dumps(exported, indent=2), encoding="utf-8")
    (lab.path / "comparison.json").write_text(json.dumps(comparison, indent=2), encoding="utf-8")
    old_jobs = len(lab.worker.jobs())
    lab.reopen()
    assert lab.functional.comparison(cycle) == comparison
    assert lab.functional.collect(lab.functional.prepare(control))["passed"] == 6
    assert len(lab.worker.jobs()) == old_jobs == 4
    # Real measured evidence, copied via SQLite backup; never corrupt original.
    copy = lab.path / "tamper-copy"
    copy.mkdir()
    shutil.copyfile(lab.school.store.backup(), copy / "core.sqlite3")
    profile, review = lab.settings
    copied_worker = WorkerAdapter(DockerBackend(profile, approved_review=review, cli=lab.cli),
        limits=lab.worker.limits, python_sources=developer_sources())
    package_id = lab.functional.prepare(control)
    with School(copy, DeveloperMetadata(), worker=copied_worker) as copied:
        run_id = copied.functional.prepare_run(package_id).id
        artifact = next(a for a in copied.core.artifacts.list(run_id) if a.relative_path.endswith("/stdout.txt"))
        with pytest.raises(sqlite3.IntegrityError, match="sealed"):
            with copied.store.transaction() as conn:
                conn.execute("UPDATE artifacts SET sha256=? WHERE id=?", ("0" * 64, artifact.id))
        # Simulate lower-level corruption in this disposable backup, bypassing
        # SQL UPDATE triggers without modifying schema/manifest. No host paths
        # or real school data are given to a container.
        rowid = copied.store.one("SELECT rowid FROM artifact_blobs WHERE artifact_id=?", (artifact.id,))[0]
        with copied.store.connection.blobopen("artifact_blobs", "content", rowid) as blob:
            blob.write(b"[")
        with pytest.raises(ArtifactMismatch):
            copied.functional.collect(package_id)
    assert lab.functional.collect(package_id)["passed"] == 6
    (lab.path / "tamper-detection.json").write_text(json.dumps({"copied_real_evidence": True,
        "sql_metadata_tamper_blocked": True, "blob_corruption_detected": True,
        "original_unchanged": True, "additional_dispatch": False}), encoding="utf-8")


def test_candidate_runtime_error_is_a_measured_failure_not_a_fake_success(laboratory):
    lab = laboratory
    cycle = lab.functional.create_cycle("developer")
    submission = lab.functional.submit_developer_fixture(cycle, "sum-positive-baseline-v1", "ordinary_error")
    result = lab.evaluate(submission)
    assert result["passed"] == 0 and result["functional_awarded"] == 0
    assert all(c["status"] == "error" for c in result["cases"])
    assert len(lab.worker.jobs()) == 1


def test_unverified_learner_origin_and_syntax_rejection_cannot_start_container(laboratory):
    lab = laboratory
    cycle = lab.functional.create_cycle("developer")
    submission = lab.functional.submit_text(cycle, "sum-positive-baseline-v1", "```python\n" + CORRECT + "```\n")
    package = lab.functional.prepare(submission)
    with pytest.raises(RecoveryRequired, match="separately authorized"):
        lab.functional.execute(package)
    other = lab.functional.create_cycle("developer")
    invalid = lab.functional.submit_text(other, "sum-positive-baseline-v1", "```python\ndef broken(:\n```\n")
    with pytest.raises(ValidationError):
        lab.functional.prepare(invalid)
    assert lab.worker.jobs() == []
    assert lab.school.store.one("SELECT COUNT(*) FROM school_v2_results")[0] == 0
