"""School v2 uses the existing Core, SQLite, approvals and Worker; no new engine."""
import ast
from dataclasses import asdict
import hashlib
import json
import re

from ii_pro_core.errors import Conflict, RecoveryRequired, ValidationError
from ii_pro_core.models import TaskStatus
from ii_pro_core.security import Action, canonical, digest, public_text
from ii_pro_core.storage import new_id, utc_now
from ii_pro_worker.school_contract import PackageError, code_sha, make_package, tests_digest, validate_code, validate_package, validate_result

from .functional_curriculum import FIXTURES, fixture_answer, suites
from .mentors import MentorFeedback


def inspect_answer(answer):
    """Static scores are objective syntax/signature/profile checks, no execution."""
    public_text(answer, limit=16000)
    if len(answer.encode("utf-8")) > 16000:
        raise ValidationError("Answer byte quota exceeded")
    blocks = re.findall(r"```(?:python|py)\s*\n(.*?)```", answer, re.DOTALL)
    code = blocks[0] if len(blocks) == 1 else ""
    scores = {"syntax": 0, "signature": 0, "restricted_profile": 0}
    finding, eligible = "one_code_fence_required", False
    try:
        if not code or len(code.encode()) > 4096:
            raise PackageError("code_budget")
        tree = ast.parse(code)
        scores["syntax"] = 10
        fn = tree.body[0] if len(tree.body) == 1 else None
        if (isinstance(fn, ast.FunctionDef) and fn.name == "sum_positive" and [a.arg for a in fn.args.args] == ["values"]
                and not (fn.args.posonlyargs or fn.args.kwonlyargs or fn.args.vararg or fn.args.kwarg or fn.args.defaults or fn.decorator_list or fn.returns)
                and not any(a.annotation for a in fn.args.args)):
            scores["signature"] = 10
        validate_code(code)
        scores["restricted_profile"], finding, eligible = 10, "bounded_profile_accepted", True
    except (SyntaxError, ValueError, RecursionError) as exc:
        finding = str(exc) if isinstance(exc, PackageError) else "syntax_rejected"
    return code, {"method": "ast_integer_profile_v1", "scores": scores, "awarded": sum(scores.values()),
                  "available": 30, "functional_awarded": None, "eligible": eligible, "finding": finding,
                  "code_executed": False, "mentor_connected": False, "verified_knowledge": False}


class FunctionalSchool:
    def __init__(self, school, worker, learner_authorizations=()):
        self.school, self.worker, self.store = school, worker, school.store
        if self.store.schema_version < 8 or type(learner_authorizations) is not tuple or any(
                type(s) is not str or not re.fullmatch(r"[a-f0-9]{64}", s) for s in learner_authorizations):
            raise ValidationError("Schema 8 and explicit immutable learner-source authorizations required")
        self._learner_authorizations = learner_authorizations

    def install(self):
        self._install(suites())

    def _install(self, curriculum):
        with self.store.transaction(admit=True) as conn:
            for suite in curriculum:
                old = conn.execute("SELECT body_json FROM school_v2_suites WHERE id=?", (suite["id"],)).fetchone()
                if old and old[0] != canonical(suite):
                    raise Conflict("Suite revisions are immutable")
                if not old:
                    conn.execute("INSERT INTO school_v2_suites VALUES(?,?,?,?,?)",
                                 (suite["id"], suite["phase"], canonical(suite), suite["tests_digest"], utc_now()))

    def _suite(self, suite_id):
        row = self.store.one("SELECT * FROM school_v2_suites WHERE id=?", (suite_id,))
        suite = json.loads(row["body_json"])
        if (suite["tests_digest"] != row["tests_digest"] or tests_digest(suite["cases"]) != row["tests_digest"]
                or "task_digest" in suite and digest(suite["public"]) != suite["task_digest"]):
            raise Conflict("Suite digest mismatch")
        return suite

    def student_material(self, suite_id):
        suite = self._suite(suite_id)
        return {"suite_id": suite_id, **suite["public"]}  # NEVER cases/reference answers

    def create_cycle(self, student_id, *, seed=42, cycle_id=None):
        if type(seed) is not int or not 0 <= seed < 2**31:
            raise ValidationError("Invalid reproducibility seed")
        with self.store.transaction(admit=True) as conn:
            if cycle_id is not None:
                if not isinstance(cycle_id, str) or not re.fullmatch(r"[a-f0-9]{32}", cycle_id):
                    raise ValidationError("Invalid cycle ID")
                old = conn.execute("SELECT * FROM school_v2_cycles WHERE id=?", (cycle_id,)).fetchone()
                if old:
                    if (old["student_id"], old["seed"]) != (student_id, seed):
                        raise Conflict("Cycle identity already used")
                    return cycle_id
            if conn.execute("SELECT COUNT(*) FROM school_v2_cycles").fetchone()[0] >= 32:
                raise ValidationError("Readiness database cycle quota reached")
            model = self.store.one("""SELECT m.* FROM school_students s JOIN school_models m ON m.id=s.model_id
                WHERE s.id=?""", (student_id,), conn=conn)
            snapshot = {"profile": json.loads(model["profile_json"]), "snapshot": json.loads(model["snapshot_json"])}
            if json.loads(snapshot["profile"]["options_json"])["seed"] != seed:
                raise ValidationError("Experiment seed must match the frozen model generation seed")
            key = cycle_id or new_id()
            conn.execute("INSERT INTO school_v2_cycles VALUES(?,?,?,?,?)", (key, student_id, canonical(snapshot), seed, utc_now()))
            self.store.event(conn, "school.v2.cycle_created", cycle_id=key, student_id=student_id, seed=seed)
            return key

    def submit_developer_fixture(self, cycle_id, suite_id, fixture):
        if fixture not in FIXTURES:
            raise ValidationError("Unknown developer fixture")
        return self._submit(cycle_id, suite_id, fixture_answer(fixture), "developer_fixture", None)

    def submit_text(self, cycle_id, suite_id, original_answer):
        return self._submit(cycle_id, suite_id, original_answer, "external_unverified", None)

    def submit_model_response(self, cycle_id, suite_id, response_run_id):
        cycle = self.store.one("SELECT student_id FROM school_v2_cycles WHERE id=?", (cycle_id,))
        source = self.store.one("""SELECT s.student_id FROM school_attempts a JOIN school_sessions s ON s.id=a.session_id
            WHERE a.run_id=?""", (response_run_id,))
        if cycle[0] != source[0] or self.school.core.runs.get(response_run_id).status != "succeeded":
            raise RecoveryRequired("Confirmed response from the same student required")
        self.store.one("SELECT run_id FROM school_v2_proposals WHERE cycle_id=? AND suite_id=? AND run_id=?",
                       (cycle_id, suite_id, response_run_id))
        return self._submit(cycle_id, suite_id, self.school.response(response_run_id), "model_response", response_run_id)

    def submit_saved_response(self, cycle_id, suite_id, response_run_id):
        """Attach confirmed existing practice bytes, never create an inference Run.

        This is a separate entry point from v2 examination proposals: it cannot
        manufacture a proposal or relabel a learner answer as a developer fixture.
        """
        with self.store.lock:
            cycle = self.store.one("SELECT * FROM school_v2_cycles WHERE id=?", (cycle_id,))
            source = self.store.one("""SELECT s.student_id,l.body_json FROM school_attempts a
                JOIN school_sessions s ON s.id=a.session_id JOIN school_enrollments e
                ON e.session_id=a.session_id AND e.lesson_id=a.lesson_id JOIN school_lessons l
                ON l.id=e.lesson_id AND l.revision=e.revision WHERE a.run_id=?""", (response_run_id,))
            run = self.school.core.runs.get(response_run_id)
            if (cycle["student_id"] != source["student_id"] or run.status != "succeeded"
                    or not run.execution_stopped or run.executor != self.school.adapter.name
                    or json.loads(source["body_json"])["id"] != "bug-positive-sum"
                    or self._suite(suite_id)["phase"] != "baseline"):
                raise RecoveryRequired("Confirmed matching sum-positive practice required")
            answer = self.school.response(response_run_id)
            self.school.core.artifacts.validate_run(response_run_id, require_any=True)
            receipt = json.loads(self.store.one("SELECT receipt_json FROM school_responses WHERE run_id=?", (response_run_id,))[0])
            if receipt["profile"] != json.loads(cycle["model_json"])["profile"]:
                raise Conflict("Cycle and response model profiles differ")
            old = self.store.all("SELECT id,source_run_id FROM school_v2_submissions WHERE cycle_id=? AND suite_id=?", (cycle_id, suite_id))
            if old:
                if len(old) != 1 or old[0]["source_run_id"] != response_run_id:
                    raise Conflict("Cycle is bound to another response")
                self._submission(old[0]["id"])
                return old[0]["id"]
            return self._submit(cycle_id, suite_id, answer, "model_response", response_run_id, saved_response=True)

    def prepare_model_request(self, cycle_id, suite_id, *, reference=None):
        """Create a normal School/Core inference Run; NEVER sends it to Ollama."""
        with self.store.lock:
            suite = self._suite(suite_id)
            comparison = suite.get("track") == "comparative-v1"
            if comparison != (reference is not None):
                raise ValidationError("Comparative exams require a frozen study arm")
            sequence = self._admit_phase(self.store.connection, cycle_id, suite) + 1
            old = self.store.connection.execute("SELECT run_id FROM school_v2_proposals WHERE cycle_id=? AND suite_id=? AND sequence=?",
                                                (cycle_id, suite_id, sequence)).fetchone()
            if old:
                return self.school.core.runs.get(old[0])
            material = self.student_material(suite_id)
            lesson_id = "v2-" + suite_id
            lesson = {"id": lesson_id, "revision": 1, "kind": "exam" if comparison else "practice", "max_attempts": 2 if suite["phase"] == "practice" else 1,
                      "restrictions": [material["restrictions"], "Return one Python code fence; explain briefly."],
                      "rubric": {"syntax": 10, "signature": 10, "single_function_scope": 10, "static_evidence": 15, "functional_pending": 55},
                      "title": "Bounded " + suite["phase"] + " evaluation", "function": "sum_positive", "arguments": ["values"],
                      "source": material.get("source", "def sum_positive(values):\n    pass\n"), "requirements": material["requirements"],
                      "public_examples": material["public_examples"], "reference_criteria": ["Functional grading is separate; private cases are not in this prompt."]}
            self.school.add_lesson(lesson)
            student = self.store.one("SELECT student_id FROM school_v2_cycles WHERE id=?", (cycle_id,))[0]
            self.school.create_session(student, session_id=cycle_id, kind="exam" if comparison else "practice")
            if sequence > 1:
                previous = self.store.one("SELECT run_id FROM school_v2_proposals WHERE cycle_id=? AND suite_id=? AND sequence=?",
                                          (cycle_id, suite_id, sequence - 1))[0]
                notes = self.store.all("""SELECT f.body_json FROM school_v2_feedback f JOIN school_v2_submissions s ON s.id=f.submission_id
                    WHERE s.cycle_id=? AND s.suite_id=? AND s.sequence=?""", (cycle_id, suite_id, sequence - 1))
                for note in notes:
                    feedback = MentorFeedback(**json.loads(note[0]))
                    if not self.store.all("SELECT 1 FROM school_feedback WHERE run_id=? AND author=? AND body=?", (previous, feedback.author, feedback.text)):
                        self.school.add_feedback(previous, feedback)
                if self.school.core.tasks.get(self.school.core.runs.get(previous).task_id).status != TaskStatus.PLANNED:
                    self.school.revise(previous)
            run = self.school.prepare_attempt(cycle_id, lesson_id, sequence=sequence, reference=reference)
            with self.store.transaction(admit=True) as conn:
                conn.execute("INSERT INTO school_v2_proposals VALUES(?,?,?,?)", (cycle_id, suite_id, sequence, run.id))
            return run

    def _unresolved(self, conn):
        return conn.execute("""SELECT 1 FROM school_v2_evaluations e JOIN runs r ON r.id=e.run_id
            WHERE r.status IN ('running','unknown') OR r.effect_status IN ('unknown','partial')""").fetchone()

    def _admit_phase(self, conn, cycle_id, suite):
        self.store.one("SELECT id FROM school_v2_cycles WHERE id=?", (cycle_id,), conn=conn)
        if self._unresolved(conn):
            raise RecoveryRequired("An unresolved functional evaluation blocks new attempts")
        count = conn.execute("SELECT COUNT(*) FROM school_v2_submissions WHERE cycle_id=? AND suite_id=?", (cycle_id, suite["id"])).fetchone()[0]
        if count >= (2 if suite["phase"] == "practice" else 1):
            raise ValidationError("Suite attempt quota exceeded")
        if suite.get("track") == "comparative-v1":
            return count
        if suite["phase"] != "baseline":
            phase = "baseline" if suite["phase"] == "practice" else "practice"
            needed = 1 if phase == "baseline" else 2
            previous = conn.execute("""SELECT a.id,a.static_json FROM school_v2_submissions a JOIN school_v2_suites s ON s.id=a.suite_id
                WHERE a.cycle_id=? AND s.phase=?""", (cycle_id, phase)).fetchall()
            completed = sum(not json.loads(p["static_json"])["eligible"] or bool(conn.execute("""SELECT 1 FROM school_v2_results r
                JOIN school_v2_packages k ON k.id=r.package_id WHERE k.submission_id=?""", (p["id"],)).fetchone()) for p in previous)
            if completed < needed:
                raise RecoveryRequired("Complete the preceding measured/static-rejected phase first")
        if suite["phase"] == "practice" and count == 1:
            previous_id = self.store.one("SELECT id FROM school_v2_submissions WHERE cycle_id=? AND suite_id=? AND sequence=1",
                                         (cycle_id, suite["id"]), conn=conn)[0]
            if not conn.execute("SELECT 1 FROM school_v2_feedback WHERE submission_id=?", (previous_id,)).fetchone():
                raise RecoveryRequired("Attributed mentor feedback required before repeat practice")
        return count

    def _submit(self, cycle_id, suite_id, answer, origin, source_run, *, saved_response=False):
        code, analysis = inspect_answer(answer)
        suite = self._suite(suite_id)
        with self.store.transaction(admit=True) as conn:
            count = self._admit_phase(conn, cycle_id, suite)
            if origin == "model_response" and not saved_response and not conn.execute("""SELECT 1 FROM school_v2_proposals
                    WHERE cycle_id=? AND suite_id=? AND sequence=? AND run_id=?""",
                    (cycle_id, suite_id, count + 1, source_run)).fetchone():
                raise ValidationError("Response must belong to this exact proposal attempt")
            key = new_id()
            conn.execute("INSERT INTO school_v2_submissions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (key, cycle_id, suite_id, count + 1, origin, source_run, answer.encode(), code_sha(answer), code,
                          code_sha(code), canonical(analysis), utc_now()))
            self.store.event(conn, "school.v2.answer_saved", submission_id=key, cycle_id=cycle_id, origin=origin,
                             source_sha256=code_sha(code), functional_executed=False)
            return key

    def _submission(self, submission_id):
        row = self.store.one("SELECT * FROM school_v2_submissions WHERE id=?", (submission_id,))
        if hashlib.sha256(row["answer"]).hexdigest() != row["answer_sha256"] or code_sha(row["code"]) != row["code_sha256"]:
            raise Conflict("Immutable submission bytes changed")
        return row

    def prepare(self, submission_id):
        with self.store.lock:
            self.store.verify_schema()
            row = self._submission(submission_id)
            if not json.loads(row["static_json"])["eligible"]:
                raise ValidationError("Static profile rejected; nothing is executed")
            old = self.store.connection.execute("SELECT id FROM school_v2_packages WHERE submission_id=?", (submission_id,)).fetchone()
            if old:
                return old[0]
            suite = self._suite(row["suite_id"])
            package = make_package(row["code"], suite["cases"], new_id())
            with self.store.transaction(admit=True) as conn:
                if self._unresolved(conn):
                    raise RecoveryRequired("Unresolved evaluation prevents new packages")
                conn.execute("INSERT INTO school_v2_packages VALUES(?,?,?,?,?)",
                             (package["id"], submission_id, canonical(package), package["digest"], utc_now()))
                self.store.event(conn, "school.v2.package_frozen", package_id=package["id"], submission_id=submission_id,
                                 package_digest=package["digest"], suite_digest=package["suite_digest"])
            return package["id"]

    def _package(self, package_id):
        row = self.store.one("SELECT * FROM school_v2_packages WHERE id=?", (package_id,))
        package = json.loads(row["body_json"])
        validate_package(package)
        if package["digest"] != row["digest"]:
            raise Conflict("Package digest mismatch")
        return row, package

    def prepare_run(self, package_id):
        with self.store.lock:
            row, package = self._package(package_id)
            old = self.store.connection.execute("SELECT run_id FROM school_v2_evaluations WHERE package_id=?", (package_id,)).fetchone()
            if old:
                return self.school.core.runs.get(old[0])
            if self._unresolved(self.store.connection):
                raise RecoveryRequired("Unresolved evaluation prevents dispatch")
            submission = self._submission(row["submission_id"])
            scope = canonical({"source_run_id": submission["source_run_id"],
                               "answer_sha256": submission["answer_sha256"],
                               "submission_id": submission["id"], "package_digest": package["digest"]})
            task = self.school.core.tasks.create("School bounded functional evaluation", executor=self.worker.name,
                                                description=scope, idempotency_key="school-v2:" + package_id)
            if task.status == TaskStatus.DRAFT:
                self.school.core.tasks.transition(task.id, TaskStatus.PLANNED)
            run = self.school.core.runs.create(task.id, idempotency_key="school-v2-run:" + package_id,
                action=Action.build("worker.fixed", {"fixture": "python_test", "arguments": {"package": package}}))
            with self.store.transaction(admit=True) as conn:
                conn.execute("INSERT INTO school_v2_evaluations VALUES(?,?)", (package_id, run.id))
            return run

    def execute(self, package_id, *, approval_id=None):
        row, package = self._package(package_id)
        submission = self._submission(row["submission_id"])
        if submission["origin"] != "developer_fixture" and submission["code_sha256"] not in self._learner_authorizations:
            raise RecoveryRequired("Learner code execution has not been separately authorized")
        if submission["origin"] == "developer_fixture" and submission["code"] not in FIXTURES.values():
            raise ValidationError("Developer provenance mismatch")
        run = self.prepare_run(package_id)
        return self.school.core.runs.execute(run.id, approval_id=approval_id)

    def collect(self, package_id):
        with self.store.lock:
            self.store.verify_schema()
            row, package = self._package(package_id)
            old = self.store.connection.execute("SELECT body_json FROM school_v2_results WHERE package_id=?", (package_id,)).fetchone()
            execution = self.store.one("SELECT run_id FROM school_v2_evaluations WHERE package_id=?", (package_id,))
            run = self.school.core.runs.get(execution[0])
            if run.status != "succeeded":
                raise RecoveryRequired("No confirmed functional result: award no functional points")
            artifacts = self.school.core.artifacts.list(run.id)
            artifact = next(a for a in artifacts if a.relative_path.endswith("/stdout.txt"))
            result = validate_result(json.loads(self.school.core.artifacts.read(artifact.id)), package)
            assessment = {"method": result["method"], "status": "measured", "passed": result["passed"], "total": result["total"],
                          "functional_awarded": 70 * result["passed"] // result["total"], "functional_available": 70,
                          "package_digest": package["digest"], "suite_digest": package["suite_digest"],
                          "source_sha256": package["source_sha256"], "cases": result["cases"],
                          "resources": result["resources"],
                          "claim": "Only the frozen finite suite was checked", "verified_knowledge": False,
                          "independent_gpt_review": "not_performed"}
            if old:
                if old[0] != canonical(assessment):
                    raise Conflict("Saved assessment no longer matches sealed evidence")
                return assessment
            with self.store.transaction() as conn:
                conn.execute("INSERT INTO school_v2_results VALUES(?,?,?,?,?,?)",
                             (new_id(), package_id, run.id, artifact.id, canonical(assessment), utc_now()))
                self.store.event(conn, "school.v2.functional_measured", run_id=run.id, package_id=package_id,
                                 passed=result["passed"], total=result["total"], suite_digest=package["suite_digest"])
            return assessment

    def add_feedback(self, submission_id, feedback: MentorFeedback):
        self._submission(submission_id)
        if feedback.role not in {"gpt_architect", "gpt_engineer"} or feedback.origin != "manual":
            raise ValidationError("Only attributed manual mentor notes; no connected GPT expertise")
        public_text(feedback.author, limit=200, empty=False)
        public_text(feedback.text, limit=4000, empty=False)
        if feedback.evidence_ref is not None:
            public_text(feedback.evidence_ref, limit=500, empty=False)
        with self.store.transaction(admit=True) as conn:
            if conn.execute("SELECT COUNT(*) FROM school_v2_feedback WHERE submission_id=?", (submission_id,)).fetchone()[0] >= 2:
                raise ValidationError("Feedback quota reached")
            key = new_id()
            conn.execute("INSERT INTO school_v2_feedback VALUES(?,?,?,?)", (key, submission_id, canonical(asdict(feedback)), utc_now()))
            self.store.event(conn, "school.v2.feedback_saved", submission_id=submission_id, feedback_id=key, origin="manual")
            return key

    def export_cycle(self, cycle_id):
        cycle = dict(self.store.one("SELECT * FROM school_v2_cycles WHERE id=?", (cycle_id,)))
        attempts = []
        for row in self.store.all("SELECT * FROM school_v2_submissions WHERE cycle_id=? ORDER BY created_at,id", (cycle_id,)):
            self._submission(row["id"])
            measured = self.store.all("""SELECT r.package_id FROM school_v2_results r JOIN school_v2_packages p ON p.id=r.package_id
                WHERE p.submission_id=?""", (row["id"],))
            score = self.collect(measured[0][0]) if measured else None
            if score:
                score = dict(score)
            if score:
                score.pop("cases")  # never expose private case descriptions through student exports
            attempts.append({"id": row["id"], "suite_id": row["suite_id"], "origin": row["origin"],
                             "sequence": row["sequence"], "raw_answer": row["answer"].decode(),
                             "source_sha256": row["code_sha256"], "static": json.loads(row["static_json"]), "functional": score,
                             "combined_awarded": json.loads(row["static_json"])["awarded"] + score["functional_awarded"] if score else None,
                             "feedback": [json.loads(r[0]) for r in self.store.all("SELECT body_json FROM school_v2_feedback WHERE submission_id=? ORDER BY created_at,id", (row["id"],))]})
        return {"format": "school-v2-cycle-v1", "cycle": cycle, "attempts": attempts, "hidden_tests_included": False,
                "mentor_connected": False, "verified_knowledge": False}

    def comparison(self, cycle_id):
        export = self.export_cycle(cycle_id)
        def measured(phase):
            values = [a for a in export["attempts"] if self._suite(a["suite_id"])["phase"] == phase and a["functional"]]
            if len(values) != 1:
                raise RecoveryRequired("Measured baseline and independent control are required")
            return values[0]
        before, after = measured("baseline"), measured("control")
        return {"before": before["functional"], "after": after["functional"],
                "delta_points": after["functional"]["functional_awarded"] - before["functional"]["functional_awarded"],
                "developer_fixture_simulation": all(a["origin"] == "developer_fixture" for a in export["attempts"]),
                "claim": "Different finite suites; no general quality or causal training claim"}
