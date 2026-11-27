import hashlib
import json
import sqlite3
from threading import Event
from concurrent.futures import ThreadPoolExecutor

import pytest

from ii_pro_core import Action, Core, RunStatus as R, TaskStatus as T
from ii_pro_core.errors import Conflict, CoreBusy, RecoveryRequired, ValidationError
from ii_pro_core.storage import MIGRATIONS, SCHEMA_VERSION, migration_checksum, utc_now
from ii_pro_school import ModelProfile, School, StudentAdapter, StudentReply
from ii_pro_school.adapters import StudentFailure
from ii_pro_school.analysis import analyze_answer
from ii_pro_school.curriculum import initial_lessons, training_prompt
from ii_pro_school.experiment import memory_snapshot, run_first_experiment
from ii_pro_school.mentors import MentorFeedback

ANSWER = "```python\r\ndef sum_positive(values):\r\n    total = 0\r\n    for value in values:\r\n        if value > 0:\r\n            total += value\r\n    return total\r\n```\r\nThe old code overwrote the accumulator.\r\n"


class FakeStudent(StudentAdapter):
    def __init__(self, *, answer=ANSWER, complete=True, error=None, preflight_error=None):
        super().__init__(ModelProfile.build("qwen7b-fast:latest", "a" * 64))
        self.answer, self.complete, self.error = answer, complete, error
        self.preflight_error = preflight_error
        self.calls, self.prompts = 0, []

    def probe(self):
        if self.preflight_error:
            raise StudentFailure(self.preflight_error)
        return {"backend": "test_fake", "digest": self.profile.expected_digest}

    def respond(self, prompt):
        self.calls += 1
        self.prompts.append(prompt)
        if self.error:
            raise StudentFailure(self.error)
        return StudentReply(self.answer, {"backend": "test_fake", "elapsed_seconds": 0.1, "statistics": {}}, self.complete)


@pytest.fixture
def school(tmp_path):
    adapter = FakeStudent()
    with School(tmp_path / "state", adapter) as school:
        school.install_curriculum()
        school.register_student("qwen", "Local Qwen")
        yield school


def attempt(school, *, session_id=None, sequence=1):
    session_id = session_id or school.create_session("qwen")
    return session_id, school.prepare_attempt(session_id, "bug-positive-sum", sequence=sequence)


def test_curriculum_has_three_distinct_practice_and_private_exams(school):
    assert len(school.catalog()) == 3
    all_lessons = school.catalog(private_exams=True)
    assert len(all_lessons) == 6
    assert len({l["id"] for l in all_lessons}) == 6
    for lesson in all_lessons:
        assert sum(lesson["rubric"].values()) == 100
        if lesson["kind"] == "exam":
            with pytest.raises(ValueError):
                training_prompt(lesson)


def test_lesson_versions_and_registrations_are_immutable(school):
    lesson = initial_lessons()[0]
    school.add_lesson(lesson)  # same request is idempotent
    with pytest.raises(Conflict):
        school.add_lesson({**lesson, "requirements": "changed"})
    school.add_lesson({**lesson, "revision": 2, "requirements": "New version"})
    school.register_student("qwen", "Local Qwen")
    with pytest.raises(Conflict):
        school.register_student("qwen", "Another name")
    assert school.register_model() == school.adapter.profile.id


def test_raw_response_history_and_artifacts_survive_reopen(school, tmp_path):
    session, run = attempt(school)
    result = school.execute(run.id)
    assert result.status == R.SUCCEEDED and result.attempt == 1
    assert school.core.tasks.get(run.task_id).status == T.VERIFYING
    assert school.response(run.id) == ANSWER
    artifact = next(a for a in school.core.artifacts.list(run.id) if a.relative_path.endswith("response.txt"))
    assert school.core.artifacts.files.path(artifact.relative_path).read_bytes() == ANSWER.encode()
    assert school.prepare_attempt(session, "bug-positive-sum").id == run.id
    assert school.execute(run.id) == result and school.adapter.calls == 1
    report = school.assess(run.id)
    assert report["score_awarded"] == report["score_available"] == 45
    assert report["functional_status"] == "not_executed" and report["mentor_status"] == "not_connected"
    export = school.export_session(session)
    assert export == school.export_session(session)
    school.close()
    adapter = FakeStudent()
    with School(tmp_path / "state", adapter) as reopened:
        assert reopened.export_session(session) == export
        assert reopened.response(run.id) == ANSWER and adapter.calls == 0
        assert reopened.core.runs.get(run.id) == result


def test_feedback_drafts_and_exam_claims_are_honest(school):
    _, run = attempt(school)
    school.execute(run.id)
    school.add_feedback(run.id, MentorFeedback("gpt_engineer", "Human operator", "Please review the accumulator.", evidence_ref="manual:note-1"))
    school.draft_knowledge(run.id, "Accumulate instead of overwriting; needs behavioral validation.")
    report = school.mentor_report(run.id)
    assert report["independent_gpt_review"] == "not_performed"
    assert report["verified_knowledge"] is False and report["exam_taken"] is False
    with pytest.raises(ValidationError):
        school.add_feedback(run.id, MentorFeedback("gpt_engineer", "Fake GPT", "Passed", origin="connected"))
    assert school.store.one("SELECT status FROM school_knowledge")[0] == "unverified"
    with pytest.raises(sqlite3.IntegrityError), school.store.transaction() as conn:
        conn.execute("UPDATE school_knowledge SET status='verified'")


def test_exams_cannot_leak_to_training_adapter_or_default_export(school):
    session = school.create_session("qwen")
    for exam in [l for l in school.catalog(private_exams=True) if l["kind"] == "exam"]:
        with pytest.raises(RecoveryRequired):
            school.prepare_attempt(session, exam["id"])
    exam_session = school.create_session("qwen", kind="exam")
    with pytest.raises(RecoveryRequired):
        school.prepare_attempt(exam_session, "bug-positive-sum")
    with pytest.raises(RecoveryRequired):
        school.export_session(exam_session)
    run = school.prepare_attempt(session, "bug-positive-sum")
    school.execute(run.id)
    text = school.export_session(session)
    for exam in [l for l in school.catalog(private_exams=True) if l["kind"] == "exam"]:
        assert exam["requirements"] not in school.adapter.prompts[0] and exam["requirements"] not in text
    assert school.store.one("SELECT COUNT(*) FROM school_assessments")[0] == 0


def test_explicit_revision_keeps_all_three_attempts(school):
    session, run = attempt(school)
    for sequence in (1, 2, 3):
        assert school.execute(run.id).status == R.SUCCEEDED
        school.assess(run.id)
        if sequence < 3:
            school.add_feedback(run.id, MentorFeedback("gpt_engineer", "Test operator", f"Review note for attempt {sequence}"))
            school.revise(run.id)
            run = school.prepare_attempt(session, "bug-positive-sum", sequence=sequence + 1)
            prompt = json.loads(run.action_json)["parameters"]["prompt"]
            assert f"Review note for attempt {sequence}" in prompt
            assert "previous_run_id" in prompt and "previous_response" in prompt
    assert [r.attempt for r in school.core.runs.list(run.task_id)] == [1, 2, 3]
    assert school.adapter.calls == 3
    with pytest.raises(ValidationError):
        school.prepare_attempt(session, "bug-positive-sum", sequence=4)
    assert school.store.one("SELECT COUNT(*) FROM school_responses")[0] == 3
    assert school.store.one("SELECT COUNT(*) FROM school_assessments")[0] == 3


def test_postdispatch_timeout_is_unknown_and_new_session_cannot_bypass(school, tmp_path):
    school.adapter.error = "timeout"
    session, run = attempt(school)
    result = school.execute(run.id)
    assert result.status == R.UNKNOWN and school.adapter.calls == 1
    with pytest.raises(RecoveryRequired):
        school.execute(run.id)
    with pytest.raises(RecoveryRequired):
        school.prepare_attempt(session, "bug-positive-sum", sequence=2)
    other = school.create_session("qwen")
    with pytest.raises(RecoveryRequired):
        school.prepare_attempt(other, "write-clamp")
    school.core.runs.resolve_interrupted(run.id, confirm_stopped=True)
    with pytest.raises(RecoveryRequired):
        school.revise(run.id)
    assert school.store.one("SELECT payload_json FROM events WHERE kind='school.adapter_diagnostic'")[0] == '{"code":"timeout"}'
    school.close()
    with School(tmp_path / "state", FakeStudent()) as reopened:
        assert reopened.core.runs.get(run.id).status == R.UNKNOWN
        assert reopened.adapter.calls == 0


def test_preflight_unavailability_never_dispatches_and_failure_is_durable(school):
    _, run = attempt(school)
    school.adapter.preflight_error = "unavailable"
    result = school.execute(run.id)
    assert result.status == R.FAILED and result.effect_status == "not_started"
    assert school.adapter.calls == 0
    assert any(e.kind == "school.adapter_diagnostic" for e in school.core.events(run.task_id))


def test_truncated_answer_is_captured_but_not_accepted(school):
    school.adapter.complete = False
    _, run = attempt(school)
    assert school.execute(run.id).status == R.UNKNOWN
    assert school.response(run.id) == ANSWER
    with pytest.raises(RecoveryRequired):
        school.assess(run.id)


def test_capture_survives_completion_transaction_failure(school, tmp_path):
    session, run = attempt(school)
    with school.store.transaction() as conn:
        conn.execute("""CREATE TRIGGER fail_success BEFORE INSERT ON events
            WHEN NEW.kind='run.succeeded' BEGIN SELECT RAISE(ABORT,'fixture fault'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        school.execute(run.id)
    assert school.response(run.id) == ANSWER and school.core.runs.get(run.id).status == R.RUNNING
    school.close()
    with School(tmp_path / "state", FakeStudent()) as reopened:
        assert reopened.core.runs.get(run.id).status == R.UNKNOWN
        assert reopened.response(run.id) == ANSWER and reopened.adapter.calls == 0
        assert json.loads(reopened.export_session(session))["attempts"][0]["response"]["text"] == ANSWER


def test_school_close_retains_active_ownership(tmp_path):
    started, release = Event(), Event()
    class Blocking(FakeStudent):
        def respond(self, prompt):
            started.set()
            assert release.wait(5)
            return super().respond(prompt)
    school = School(tmp_path / "state", Blocking())
    school.install_curriculum()
    school.register_student("qwen", "Local Qwen")
    _, run = attempt(school)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(school.execute, run.id)
        try:
            assert started.wait(5)
            with pytest.raises(CoreBusy):
                school.close(timeout=0)
            with pytest.raises(CoreBusy):
                Core(tmp_path / "state")
        finally:
            release.set()
        assert future.result(timeout=5).status == R.SUCCEEDED
    school.close()


@pytest.mark.parametrize("answer", ["plain explanation", "```python\ndef nope(:\n```", "```python\nimport os\ndef sum_positive(values):\n return 0\n```", "```python\ndef wrong(values):\n return 0\n```", "```python\ndef sum_positive(values):\n return 0\n```\n```python\nprint(1)\n```"])
def test_static_analysis_rejects_bad_structure_without_executing(answer, tmp_path):
    result = analyze_answer(answer, initial_lessons()[0])
    assert result["score_awarded"] < 45
    assert result["functional_status"] == "not_executed" and result["verified_knowledge"] is False


def test_malicious_function_body_is_only_parsed(tmp_path):
    sentinel = tmp_path / "must-not-exist"
    answer = f'```python\ndef sum_positive(values):\n    open({str(sentinel)!r}, "w").write("bad")\n    return 0\n```'
    result = analyze_answer(answer, initial_lessons()[0])
    assert not sentinel.exists() and result["functional_status"] == "not_executed"
    assert result["scores"]["single_function_scope"] == 0


@pytest.mark.parametrize("index,answer", [
    (1, "```python\ndef clamp_int(value, lower, upper):\n    if any(type(x) is not int for x in (value, lower, upper)):\n        raise TypeError\n    if lower > upper:\n        raise ValueError\n    return max(lower, min(value, upper))\n```"),
    (2, "```python\ndef unique_strings(values):\n    seen = set()\n    result = []\n    for value in values:\n        if value not in seen:\n            seen.add(value)\n            result.append(value)\n    return result\n```"),
])
def test_other_two_practice_lessons_have_static_checks_only(index, answer):
    result = analyze_answer(answer, initial_lessons()[index])
    assert result["score_awarded"] == 45
    assert result["functional_status"] == "not_executed"


def test_concurrent_school_prepare_is_one_attempt(school):
    session = school.create_session("qwen")
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: school.prepare_attempt(session, "bug-positive-sum").id, range(8)))
    assert len(set(ids)) == 1
    assert school.store.one("SELECT COUNT(*) FROM school_attempts")[0] == 1
    assert school.adapter.calls == 0


def test_closed_facade_does_not_unbind_reused_adapter(tmp_path):
    adapter = FakeStudent()
    first = School(tmp_path / "first", adapter)
    first.close()
    with School(tmp_path / "second", adapter) as second:
        first.close()
        second.install_curriculum()
        second.register_student("qwen", "Local Qwen")
        _, run = attempt(second)
        assert second.execute(run.id).status == R.SUCCEEDED


def test_cli_catalog_export_and_manual_feedback_need_no_model_call(school, capsys):
    from ii_pro_school.__main__ import main
    session, run = attempt(school)
    school.execute(run.id)
    state = str(school.store.root)
    school.close()
    assert main(["--state-dir", state, "catalog"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 3
    assert main(["--state-dir", state, "export", session]) == 0
    assert json.loads(capsys.readouterr().out)["attempts"][0]["response"]["text"] == ANSWER
    assert main(["--state-dir", state, "feedback", run.id, "--role", "gpt_engineer", "--author", "Test operator", "--text", "Manual review pending"]) == 0
    assert json.loads(capsys.readouterr().out)["origin"] == "manual"
    assert school.adapter.calls == 1


def test_memory_snapshot_is_read_only_observation():
    snapshot = memory_snapshot()
    assert snapshot["status"] in {"observed", "unavailable"}
    if snapshot["status"] == "observed":
        assert snapshot["total_mib"] > 0 and snapshot["available_mib"] >= 0


@pytest.mark.parametrize("table", ["school_lessons", "school_models", "school_students", "school_sessions", "school_enrollments", "school_attempts", "school_responses", "school_assessments", "school_feedback", "school_knowledge"])
def test_school_history_cannot_be_deleted(school, table):
    _, run = attempt(school)
    school.execute(run.id)
    school.assess(run.id)
    school.add_feedback(run.id, MentorFeedback("gpt_architect", "Operator", "Needs review"))
    school.draft_knowledge(run.id, "Draft only")
    with pytest.raises(sqlite3.IntegrityError), school.store.transaction() as conn:
        conn.execute(f"DELETE FROM {table}")


def test_experiment_fake_backend_is_reproducibly_recorded(tmp_path):
    fake = FakeStudent()
    result = run_first_experiment(tmp_path / "experiment", adapter=fake, resources=lambda: {"available_mib": 6000})
    assert result["status"] == "response_received_static_only" and fake.calls == 1
    evidence = tmp_path / "experiment/evidence"
    assert (evidence / "response.txt").read_bytes() == ANSWER.encode()
    exported = json.loads((evidence / "session.json").read_text())
    assert exported["attempts"][0]["response"]["sha256"] == hashlib.sha256(ANSWER.encode()).hexdigest()
    with pytest.raises(ValueError):
        run_first_experiment(tmp_path / "experiment", adapter=FakeStudent())


def test_insufficient_resources_diagnosed_without_calling_model(tmp_path):
    fake = FakeStudent()
    result = run_first_experiment(tmp_path / "low-memory", adapter=fake, resources=lambda: {"available_mib": 100})
    assert result["error_code"] == "insufficient_or_unknown_memory" and fake.calls == 0
    assert (tmp_path / "low-memory/evidence/experiment.json").exists()
    assert not (tmp_path / "low-memory/core.sqlite3").exists()


def test_unavailable_model_experiment_preserves_diagnostics_without_inference(tmp_path):
    fake = FakeStudent(preflight_error="model_missing")
    result = run_first_experiment(tmp_path / "missing", adapter=fake, resources=lambda: {"available_mib": 6000})
    assert result["error_code"] == "model_missing" and fake.calls == 0
    assert result["session_id"] is None and result["status"] == "diagnostic"
    assert (tmp_path / "missing/evidence/experiment.json").exists()


def test_school_migration_failure_rolls_back_v4_database(tmp_path, monkeypatch):
    state = tmp_path / "legacy"
    state.mkdir()
    path = state / "core.sqlite3"
    with sqlite3.connect(path) as conn:
        for version in (1, 2, 3, 4):
            for sql in MIGRATIONS[version]:
                conn.execute(sql)
            conn.execute("INSERT INTO migrations VALUES(?,?,?)", (version, migration_checksum(MIGRATIONS[version]), utc_now()))
        conn.execute("PRAGMA user_version=4")
    with monkeypatch.context() as patch:
        patch.setitem(MIGRATIONS, 5, (*MIGRATIONS[5], "INVALID SQL"))
        with pytest.raises(sqlite3.OperationalError):
            Core(state)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 4
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'school_%'").fetchall()
    with Core(state) as core:
        assert core.store.one("PRAGMA integrity_check")[0] == "ok"
        assert core.store.all("PRAGMA foreign_key_check") == []
        backups = list((state / "backups").glob(f"pre-v{SCHEMA_VERSION}-*.sqlite3"))
        assert backups
        with sqlite3.connect(backups[-1]) as backup:
            assert backup.execute("PRAGMA user_version").fetchone()[0] == 4
            assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("field,value", [("revision", True), ("lesson_digest", "bad"), ("session_id", "../escape"), ("prompt", ""), ("extra", "bad")])
def test_school_action_schema_rejects_invalid_fields(school, field, value):
    _, run = attempt(school)
    params = json.loads(run.action_json)["parameters"]
    params[field] = value
    with pytest.raises(ValidationError):
        Action.build("school.propose", params)
