import builtins
import hashlib
import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from ii_pro_core import Action, Core, TaskStatus
from ii_pro_core.errors import ApprovalRequired, RecoveryRequired, SchemaError, ValidationError
import ii_pro_core.storage as storage
from ii_pro_core.storage import new_id, utc_now
from ii_pro_school import ModelProfile, School, StudentAdapter, StudentReply
from ii_pro_school.functional import inspect_answer
from ii_pro_school.functional_curriculum import CORRECT, FIXTURES, developer_sources, suites
from ii_pro_school.mentors import MentorFeedback
from ii_pro_worker import NativeFixtureBackend, WorkerAdapter
from ii_pro_worker.school_contract import PackageError, canonical, make_package, validate_code, validate_package, validate_result


class OfflineStudent(StudentAdapter):
    revision = "developer-readiness-metadata-v1"

    def __init__(self):
        super().__init__(ModelProfile.build("developer-fixture", "d" * 64))
        self.calls = 0

    def approval_configuration(self):
        return {"profile": self.profile.id, "inference_allowed": False}

    def probe(self):
        return {"backend": "developer_fixture", "inference_performed": False}

    def respond(self, prompt):
        self.calls += 1
        raise AssertionError("No model invocation authorized during readiness tests")


def open_school(path, *, authorize_developer=True):
    scratch = path.parent / (path.name + "-scratch")
    scratch.mkdir(exist_ok=True)
    worker = WorkerAdapter(NativeFixtureBackend(scratch, allow_trusted_fixtures=True),
                           python_sources=developer_sources() if authorize_developer else ())
    school = School(path, OfflineStudent(), worker=worker)
    school.functional.install()
    school.register_student("developer", "Developer-authored fixtures, not Qwen")
    return school


@pytest.fixture
def school(tmp_path):
    with open_school(tmp_path / "state") as selected:
        yield selected


def baseline(school, *, external=False):
    cycle = school.functional.create_cycle("developer")
    suite = "sum-positive-baseline-v1"
    submission = (school.functional.submit_text(cycle, suite, "```python\n" + CORRECT + "```\n") if external
                  else school.functional.submit_developer_fixture(cycle, suite, "correct"))
    return cycle, submission


@pytest.mark.parametrize("fixture", list(FIXTURES))
def test_developer_code_fits_finite_profile_without_host_execution(fixture):
    validate_code(FIXTURES[fixture])


@pytest.mark.parametrize("code", [
    "import os\ndef sum_positive(values):\n    return 0\n",
    "def sum_positive(values):\n    while True:\n        pass\n",
    "def sum_positive(values):\n    return values.__class__\n",
    "def sum_positive(values):\n    return open('private')\n",
    "def sum_positive(values):\n    return globals()\n",
    "def sum_positive(values):\n    return sum_positive(values)\n",
    "def sum_positive(values):\n    values = 0\n    return 0\n",
    "def sum_positive(values):\n    total = values\n    return total\n",
    "def sum_positive(values):\n    for x in range(10000):\n        x += 1\n    return 0\n",
    "def sum_positive(values):\n    return 10000 ** 10000\n",
    "def sum_positive(values):\n    return [0] * 10000\n",
    "def sum_positive(values):\n    values[0] = 1\n    return 0\n",
    "@print\ndef sum_positive(values):\n    return 0\n",
    "def sum_positive(values, other=print(1)):\n    return 0\n",
])
def test_unsupported_programs_rejected_statically(code):
    with pytest.raises(PackageError):
        validate_code(code)


def test_comprehension_is_bounded_and_valid():
    validate_code("def sum_positive(values):\n    return sum(x for x in values if x > 0)\n")


def test_answer_package_preparation_never_executes_on_host(school, monkeypatch):
    cycle, submission = baseline(school)
    original = school.functional._submission(submission)["answer"]
    def forbidden(*args, **kwargs):
        raise AssertionError("Host code execution forbidden")
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "exec", forbidden)
        package_id = school.functional.prepare(submission)
        assert school.functional.prepare(submission) == package_id
    row, package = school.functional._package(package_id)
    assert package["code"] == CORRECT
    assert school.functional._submission(submission)["answer"] == original
    export = school.functional.export_cycle(cycle)
    assert export["attempts"][0]["static"]["awarded"] == 30
    assert export["attempts"][0]["functional"] is None and export["attempts"][0]["combined_awarded"] is None
    assert school.adapter.calls == 0


def test_learner_provenance_remains_blocked_even_for_same_bytes_as_fixture(school):
    _, submission = baseline(school, external=True)
    package_id = school.functional.prepare(submission)
    with pytest.raises(RecoveryRequired, match="separately authorized"):
        school.functional.execute(package_id)
    assert school.store.one("SELECT COUNT(*) FROM runs")[0] == 0


def test_python_packages_cannot_use_native_backend_or_skip_core_approval(school):
    _, submission = baseline(school)
    package_id = school.functional.prepare(submission)
    run = school.functional.prepare_run(package_id)
    with pytest.raises(ApprovalRequired):
        school.functional.execute(package_id)
    approval = school.core.approvals.request(run.id)
    school.core.approvals.decide(approval.id, allow=True)
    result = school.functional.execute(package_id, approval_id=approval.id)
    assert result.status == "failed" and result.effect_status == "not_started"
    assert school.functional.worker.jobs() == []
    with pytest.raises(RecoveryRequired):
        school.functional.collect(package_id)
    assert school.store.one("SELECT COUNT(*) FROM school_v2_results")[0] == 0


@pytest.mark.parametrize("answer", ["no fence", "```python\ndef sum_positive(:\n```", "```python\nprint(1)\n```", "```python\nx=1\n```\n```py\nx=2\n```"])
def test_invalid_answers_saved_but_not_packaged_or_functionally_scored(school, answer):
    cycle = school.functional.create_cycle("developer")
    submission = school.functional.submit_text(cycle, "sum-positive-baseline-v1", answer)
    with pytest.raises(ValidationError):
        school.functional.prepare(submission)
    entry = school.functional.export_cycle(cycle)["attempts"][0]
    assert entry["raw_answer"] == answer and entry["functional"] is None and entry["combined_awarded"] is None


def test_private_cases_are_disjoint_and_never_in_model_prompt(school):
    cycle = school.functional.create_cycle("developer")
    with pytest.raises(RecoveryRequired):
        school.functional.prepare_model_request(cycle, "sum-positive-control-v1")
    run = school.functional.prepare_model_request(cycle, "sum-positive-baseline-v1")
    assert school.functional.prepare_model_request(cycle, "sum-positive-baseline-v1").id == run.id
    prompt = json.loads(run.action_json)["parameters"]["prompt"]
    assert "cases" not in prompt and "expected" not in prompt
    assert school.adapter.calls == 0 and run.status == "created"
    groups = [{canonical(c["input"]) for c in suite["cases"]} for suite in suites()]
    assert all(not groups[i].intersection(groups[j]) for i in range(3) for j in range(i + 1, 3))
    assert "cases" not in school.functional.student_material("sum-positive-control-v1")


def test_feedback_is_attributed_and_does_not_claim_real_gpt(school):
    cycle, submission = baseline(school)
    feedback = MentorFeedback("gpt_engineer", "Developer test note", "Accumulate instead of overwriting.")
    school.functional.add_feedback(submission, feedback)
    entry = school.functional.export_cycle(cycle)["attempts"][0]
    assert entry["feedback"][0]["origin"] == "manual"
    assert not school.functional.export_cycle(cycle)["mentor_connected"]
    with pytest.raises(ValidationError):
        school.functional.add_feedback(submission, MentorFeedback("gpt_engineer", "Pretend", "text", origin="connected"))


def test_package_and_submission_are_immutable_and_scope_is_checked(school):
    _, submission = baseline(school)
    package_id = school.functional.prepare(submission)
    _, package = school.functional._package(package_id)
    changed = json.loads(canonical(package)); changed["cases"][0]["expected"] = 999
    with pytest.raises(PackageError):
        validate_package(changed)
    for table, column, value in (("school_v2_submissions", "code", "changed"), ("school_v2_packages", "body_json", "{}")):
        with pytest.raises(sqlite3.IntegrityError):
            with school.store.transaction() as conn:
                conn.execute(f"UPDATE {table} SET {column}=?", (value,))


def test_schema_drift_is_detected_during_same_process_before_package(school):
    _, submission = baseline(school)
    with school.store.transaction() as conn:
        conn.execute("DROP TRIGGER events_no_delete")
    with pytest.raises(SchemaError):
        school.functional.prepare(submission)
    assert school.store.one("SELECT COUNT(*) FROM school_v2_packages")[0] == 0


@pytest.mark.parametrize("kind", ["version", "ledger"])
def test_mid_session_version_and_ledger_drift_are_not_accepted(school, kind):
    _, submission = baseline(school)
    with school.store.transaction() as conn:
        if kind == "version":
            conn.execute("PRAGMA user_version=99")
        else:
            conn.execute("UPDATE migrations SET checksum=? WHERE version=1", ("f" * 64,))
    with pytest.raises(SchemaError):
        school.functional.prepare(submission)


def test_generation_seed_and_phase_rules_are_explicit(school):
    with pytest.raises(ValidationError, match="seed"):
        school.functional.create_cycle("developer", seed=99)
    cycle = school.functional.create_cycle("developer")
    with pytest.raises(RecoveryRequired):
        school.functional.submit_developer_fixture(cycle, "sum-positive-control-v1", "correct")
    rejected = school.functional.submit_text(cycle, "sum-positive-baseline-v1", "invalid syntax, no fence")
    assert not json.loads(school.functional._submission(rejected)["static_json"])["eligible"]
    practice = school.functional.submit_text(cycle, "sum-positive-practice-v1", "invalid syntax, no fence")
    with pytest.raises(RecoveryRequired, match="feedback"):
        school.functional.submit_developer_fixture(cycle, "sum-positive-practice-v1", "correct")
    school.functional.add_feedback(practice, MentorFeedback("gpt_engineer", "Developer", "Use the stated signature."))
    school.functional.submit_developer_fixture(cycle, "sum-positive-practice-v1", "correct")
    with pytest.raises(RecoveryRequired):
        school.functional.comparison(cycle)


def test_no_host_path_can_be_imported_as_a_package():
    package = make_package(CORRECT, suites()[0]["cases"], "a" * 32)
    for field in ("path", "archive", "working_directory"):
        with pytest.raises(PackageError):
            validate_package({**package, field: "../../private"})


def test_package_result_identity_and_counts_cannot_be_forged():
    package = make_package(CORRECT, suites()[0]["cases"], "a" * 32)
    valid = {"method": "integer-tests-v1", "package_id": package["id"], "package_digest": package["digest"],
             "source_sha256": package["source_sha256"], "suite_digest": package["suite_digest"],
             "cases": [{"id": c["id"], "status": "passed", "diagnostic": "ok"} for c in package["cases"]], "passed": 6, "total": 6,
             "resources": {"memory_peak": 1024, "cpu_usage_usec": 1000, "pids_current": 4}}
    validate_result(valid, package)  # synthetic schema unit data, never a School assessment
    for key, value in (("package_digest", "f" * 64), ("passed", 1), ("total", True)):
        wrong = {**valid, key: value}
        with pytest.raises(PackageError):
            validate_result(wrong, package)


def test_cycle_attempt_and_raw_answer_quotas(school):
    cycle, _ = baseline(school)
    with pytest.raises(ValidationError, match="quota"):
        school.functional.submit_developer_fixture(cycle, "sum-positive-baseline-v1", "correct")
    for _ in range(31):
        school.functional.create_cycle("developer")
    with pytest.raises(ValidationError, match="quota"):
        school.functional.create_cycle("developer")
    with pytest.raises(ValidationError):
        inspect_answer("x" * 16001)


def test_aggregate_blob_quota_is_atomic_and_blocks_dispatch_before_effect(tmp_path):
    scratch = tmp_path / "scratch"; scratch.mkdir()
    worker = WorkerAdapter(NativeFixtureBackend(scratch, allow_trusted_fixtures=True))
    with Core(tmp_path / "state", executors=[worker]) as core:
        worker.bind(core)
        task = core.tasks.create("Quota fixture", executor="worker", idempotency_key="task")
        core.tasks.transition(task.id, TaskStatus.PLANNED)
        run = core.runs.create(task.id, idempotency_key="run", action=Action.build("worker.fixed", {"fixture": "echo", "arguments": {"text": "never executed"}}))
        content = b"x" * 32768
        with core.store.transaction() as conn:
            for n in range(256):
                key = new_id()
                conn.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)", (key, task.id, run.id, "quota/" + str(n), hashlib.sha256(content).hexdigest(), len(content), utc_now()))
                conn.execute("INSERT INTO artifact_blobs VALUES(?,?)", (key, content))
        with pytest.raises(sqlite3.IntegrityError, match="quota"):
            with core.store.transaction() as conn:
                key = new_id()
                conn.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)", (key, task.id, run.id, "quota/overflow", "a" * 64, len(content), utc_now()))
                conn.execute("INSERT INTO artifact_blobs VALUES(?,?)", (key, content))
        assert core.store.one("SELECT COUNT(*) FROM artifact_blobs")[0] == 256
        approval = core.approvals.request(run.id); core.approvals.decide(approval.id, allow=True)
        assert core.runs.execute(run.id, approval_id=approval.id).effect_status == "not_started"
        assert worker.jobs() == []


def test_v7_upgrade_keeps_legacy_history_and_rolls_back_failed_v8(tmp_path, monkeypatch):
    old = tmp_path / "v7"
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 7)
        with School(old, OfflineStudent()) as previous:
            previous.install_curriculum(); previous.register_student("developer", "Fixture")
            session = previous.create_session("developer")
            expected = previous.export_session(session)
    before = hashlib.sha256((old / "core.sqlite3").read_bytes()).hexdigest()
    copy = tmp_path / "copy"; shutil.copytree(old, copy)
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 8)
        patch.setitem(storage.MIGRATIONS, 8, (*storage.MIGRATIONS[8], "INVALID SQL"))
        with pytest.raises(sqlite3.OperationalError):
            Core(copy)
    with sqlite3.connect(copy / "core.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert db.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 7
        assert db.execute("SELECT name FROM sqlite_master WHERE name='school_v2_packages'").fetchone() is None
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 8)
        with School(copy, OfflineStudent()) as current:
            assert current.export_session(session) == expected and current.store.schema_version == 8
    assert hashlib.sha256((old / "core.sqlite3").read_bytes()).hexdigest() == before
    backups = list((copy / "backups").glob("pre-v8-*.sqlite3"))
    assert backups
    for path in backups:
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 7
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_concurrent_submissions_cannot_exceed_the_baseline_budget(school):
    cycle = school.functional.create_cycle("developer")
    def submit(_):
        try:
            return school.functional.submit_developer_fixture(cycle, "sum-positive-baseline-v1", "correct")
        except ValidationError:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(submit, range(4)))
    assert sum(key is not None for key in ids) == 1
    assert school.store.one("SELECT COUNT(*) FROM school_v2_submissions")[0] == 1


def test_concurrent_package_and_run_preparation_share_one_attempt(school):
    _, submission = baseline(school)
    with ThreadPoolExecutor(max_workers=4) as pool:
        packages = list(pool.map(lambda _: school.functional.prepare(submission), range(4)))
        runs = list(pool.map(lambda _: school.functional.prepare_run(packages[0]).id, range(4)))
    assert len(set(packages)) == len(set(runs)) == 1
    assert school.store.one("SELECT COUNT(*) FROM runs")[0] == 1
    assert school.functional.worker.jobs() == []


def test_interrupted_grading_stays_unknown_and_unscored_after_reopen(tmp_path, monkeypatch):
    from ii_pro_core.executors import SimulatedInterruption
    state = tmp_path / "state"
    # Fault injection only; no candidate execution, no real Docker process.
    def crash(self, request):
        raise SimulatedInterruption()
    with monkeypatch.context() as patch:
        patch.setattr(WorkerAdapter, "prepare", lambda self, request: None)
        patch.setattr(WorkerAdapter, "execute", crash)
        with open_school(state) as original:
            cycle, submission = baseline(original)
            package = original.functional.prepare(submission)
            run = original.functional.prepare_run(package)
            approval = original.core.approvals.request(run.id)
            original.core.approvals.decide(approval.id, allow=True)
            with pytest.raises(SimulatedInterruption):
                original.functional.execute(package, approval_id=approval.id)
    with open_school(state) as reopened:
        assert reopened.core.runs.get(run.id).status == "unknown"
        with pytest.raises(RecoveryRequired):
            reopened.functional.execute(package)
        with pytest.raises(RecoveryRequired):
            reopened.functional.collect(package)
        other = reopened.functional.create_cycle("developer")
        with pytest.raises(RecoveryRequired):
            reopened.functional.submit_developer_fixture(other, "sum-positive-baseline-v1", "correct")
        assert reopened.functional.export_cycle(cycle)["attempts"][0]["functional"] is None
        assert reopened.store.one("SELECT COUNT(*) FROM school_v2_results")[0] == 0
        assert reopened.store.one("SELECT COUNT(*) FROM runs")[0] == 1


def test_original_response_is_bound_to_the_exact_proposal_attempt(school, monkeypatch):
    # Mocked text response tests the existing inference contract, not Qwen.
    answer = "No code fence: developer metadata fixture."
    monkeypatch.setattr(OfflineStudent, "respond", lambda self, prompt: StudentReply(answer, {"backend": "test_fixture"}))
    cycle = school.functional.create_cycle("developer")
    baseline_run = school.functional.prepare_model_request(cycle, "sum-positive-baseline-v1")
    assert school.execute(baseline_run.id).status == "succeeded"
    submission = school.functional.submit_model_response(cycle, "sum-positive-baseline-v1", baseline_run.id)
    assert school.functional._submission(submission)["answer"] == answer.encode()
    practice_run = school.functional.prepare_model_request(cycle, "sum-positive-practice-v1")
    assert school.execute(practice_run.id).status == "succeeded"
    first = school.functional.submit_model_response(cycle, "sum-positive-practice-v1", practice_run.id)
    school.functional.add_feedback(first, MentorFeedback("gpt_engineer", "Developer", "Provide the requested function."))
    second = school.functional.prepare_model_request(cycle, "sum-positive-practice-v1")
    assert second.id != practice_run.id and second.status == "created"
    with pytest.raises(ValidationError, match="exact proposal"):
        school.functional.submit_model_response(cycle, "sum-positive-practice-v1", practice_run.id)
    assert school.execute(second.id).status == "succeeded"
    latest = school.functional.submit_model_response(cycle, "sum-positive-practice-v1", second.id)
    assert school.functional._submission(latest)["sequence"] == 2


def test_concurrent_legacy_registration_cannot_bypass_the_per_run_quota(tmp_path):
    with Core(tmp_path / "state") as core:
        task = core.tasks.create("Developer count fixture", idempotency_key="count")
        core.tasks.transition(task.id, TaskStatus.PLANNED)
        run = core.runs.create(task.id, idempotency_key="count-run")
        core.runs.execute(run.id)
        folder = core.artifacts.files.root / task.id / run.id
        for n in range(24):
            (folder / f"extra-{n}.txt").write_text("small fixture")
        def register(n):
            from ii_pro_core.errors import Conflict
            try:
                return core.artifacts.register(run.id, f"extra-{n}.txt").id
            except Conflict:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(register, range(24)))
        assert len(core.artifacts.list(run.id)) == 16
        assert sum(key is not None for key in ids) == 15  # one initial mock receipt
        accepted = next(n for n, key in enumerate(ids) if key)
        assert core.artifacts.register(run.id, f"extra-{accepted}.txt").id == ids[accepted]
