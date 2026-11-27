"""School domain records on Core's SQLite, runs, artifacts and event journal."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path

from ii_pro_core import Action, Core, RunStatus as R, TaskStatus as T
from ii_pro_core.errors import Conflict, RecoveryRequired, ValidationError
from ii_pro_core.security import canonical, digest, public_text
from ii_pro_core.storage import new_id, utc_now

from .adapters import StudentAdapter
from .analysis import analyze_answer
from .curriculum import initial_lessons, training_prompt
from .mentors import MentorFeedback


class School:
    def __init__(self, state_dir, adapter: StudentAdapter, *, worker=None, learner_authorizations=(), policy=None, core=None):
        if not isinstance(adapter, StudentAdapter) or adapter.capture is not None:
            raise ValidationError("An unbound trusted StudentAdapter is required")
        self.adapter = adapter
        if core is not None and (worker is not None or policy is not None
                or core.store.root != Path(state_dir).resolve()
                or core.runs.executors.get(adapter.name) is not adapter):
            raise ValidationError("A shared Core must already register this exact adapter")
        self._owns_core = core is None
        self.core = core if core is not None else Core(state_dir, executors=[adapter] + ([worker] if worker is not None else []), policy=policy)
        self.store = self.core.store
        adapter.capture = self._capture
        adapter.capture_diagnostic = self._capture_diagnostic
        self.functional = None
        if worker is not None:
            try:
                from .functional import FunctionalSchool
                worker.bind(self.core)
                self.functional = FunctionalSchool(self, worker, learner_authorizations)
            except BaseException:
                self.close()
                raise

    def close(self, **kwargs):
        if self._owns_core:
            self.core.close(**kwargs)
        if self.adapter.capture == self._capture:
            self.adapter.capture = None
            self.adapter.capture_diagnostic = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def add_lesson(self, lesson: dict):
        keys = {"id", "revision", "kind", "max_attempts", "restrictions", "rubric", "title", "function",
                "arguments", "source", "requirements", "public_examples", "reference_criteria"}
        if not isinstance(lesson, dict) or set(lesson) != keys:
            raise ValidationError("Invalid lesson fields")
        for key in ("id", "function"):
            if not isinstance(lesson[key], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", lesson[key]):
                raise ValidationError("Invalid lesson identity")
        if lesson["kind"] not in {"practice", "exam"} or type(lesson["revision"]) is not int or lesson["revision"] < 1:
            raise ValidationError("Invalid lesson version")
        if type(lesson["max_attempts"]) is not int or not 1 <= lesson["max_attempts"] <= 3:
            raise ValidationError("Invalid attempt budget")
        for key in ("title", "source", "requirements"):
            public_text(lesson[key], limit=5000)
        for key in ("arguments", "restrictions", "public_examples", "reference_criteria"):
            if not isinstance(lesson[key], list) or not 0 <= len(lesson[key]) <= 20:
                raise ValidationError("Invalid lesson list")
            for item in lesson[key]:
                public_text(item, limit=2000, empty=False)
        weights = {"syntax": 10, "signature": 10, "single_function_scope": 10, "static_evidence": 15, "functional_pending": 55}
        if lesson["rubric"] != weights or any(type(v) is not int for v in lesson["rubric"].values()):
            raise ValidationError("Unsupported rubric")
        body, fingerprint = canonical(lesson), digest(lesson)
        with self.store.transaction(admit=True) as conn:
            old = conn.execute("SELECT digest FROM school_lessons WHERE id=? AND revision=?", (lesson["id"], lesson["revision"])).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise Conflict("Lesson revisions are immutable")
                return fingerprint
            conn.execute("INSERT INTO school_lessons VALUES(?,?,?,?,?,?)", (
                lesson["id"], lesson["revision"], lesson["kind"], body, fingerprint, utc_now()))
            self.store.event(conn, "school.lesson_created", lesson_id=lesson["id"], revision=lesson["revision"], lesson_kind=lesson["kind"], digest=fingerprint)
        return fingerprint

    def install_curriculum(self):
        for lesson in initial_lessons():
            self.add_lesson(lesson)

    def catalog(self, *, private_exams=False):
        return [json.loads(r[0]) for r in self.store.all(
            "SELECT body_json FROM school_lessons WHERE kind='practice' OR ? ORDER BY id,revision", (bool(private_exams),))]

    def register_model(self, *, defer_probe=False):
        # Planned profiles can be persisted while the service is offline.
        # Readiness and actual metadata are still checked/captured at dispatch.
        snapshot = ({"status": "not_probed", "model": self.adapter.profile.model,
                     "expected_digest": self.adapter.profile.expected_digest}
                    if defer_probe else self.adapter.probe())
        model_id = self.adapter.profile.id
        with self.store.transaction(admit=True) as conn:
            if not conn.execute("SELECT 1 FROM school_models WHERE id=?", (model_id,)).fetchone():
                conn.execute("INSERT INTO school_models VALUES(?,?,?,?)", (
                    model_id, canonical(asdict(self.adapter.profile)), canonical(snapshot), utc_now()))
                self.store.event(conn, "school.model_registered", model_id=model_id)
        return model_id

    def register_student(self, student_id: str, name: str, *, defer_probe=False):
        if not isinstance(student_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", student_id):
            raise ValidationError("Invalid student ID")
        public_text(name, limit=200, empty=False)
        model_id = self.register_model(defer_probe=defer_probe)
        with self.store.transaction(admit=True) as conn:
            old = conn.execute("SELECT * FROM school_students WHERE id=?", (student_id,)).fetchone()
            if old:
                if (old["name"], old["model_id"]) != (name, model_id):
                    raise Conflict("Student registration already has another model/name")
                return student_id
            conn.execute("INSERT INTO school_students VALUES(?,?,?,?)", (student_id, name, model_id, utc_now()))
            self.store.event(conn, "school.student_registered", student_id=student_id, model_id=model_id)
        return student_id

    def create_session(self, student_id: str, *, kind="practice", session_id=None):
        if kind not in {"practice", "exam"}:
            raise ValidationError("Invalid session kind")
        session_id = session_id or new_id()
        if not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise ValidationError("Invalid session ID")
        with self.store.transaction(admit=True) as conn:
            student = self.store.one("SELECT * FROM school_students WHERE id=?", (student_id,), conn=conn)
            if student["model_id"] != self.adapter.profile.id:
                raise Conflict("Session model differs from the configured adapter")
            old = conn.execute("SELECT * FROM school_sessions WHERE id=?", (session_id,)).fetchone()
            if old:
                if (old["student_id"], old["kind"]) != (student_id, kind):
                    raise Conflict("Session ID already used")
                return session_id
            conn.execute("INSERT INTO school_sessions VALUES(?,?,?,?)", (session_id, student_id, kind, utc_now()))
            self.store.event(conn, "school.session_created", session_id=session_id, student_id=student_id, session_kind=kind)
        return session_id

    def prepare_attempt(self, session_id: str, lesson_id: str, *, revision=1, sequence=1, reference=None):
        # One owner process; hold RLock across recoverable public Core steps.
        # Deterministic keys repair partial setup without redispatching a Run.
        with self.store.lock:
            session = self.store.one("SELECT * FROM school_sessions WHERE id=?", (session_id,))
            student = self.store.one("SELECT * FROM school_students WHERE id=?", (session["student_id"],))
            lesson_row = self.store.one("SELECT * FROM school_lessons WHERE id=? AND revision=?", (lesson_id, revision))
            lesson = json.loads(lesson_row["body_json"])
            study_exam = reference is not None and lesson_id.startswith("v2-study-")
            kind = "exam" if study_exam else "practice"
            if session["kind"] != kind or lesson["kind"] != kind:
                raise RecoveryRequired("Private exams are not released by the training path")
            if student["model_id"] != self.adapter.profile.id:
                raise Conflict("Student model profile changed")
            if type(sequence) is not int or not 1 <= sequence <= lesson["max_attempts"]:
                raise ValidationError("Attempt budget exceeded")
            enrollment = self.store.connection.execute("SELECT * FROM school_enrollments WHERE session_id=? AND lesson_id=?", (session_id, lesson_id)).fetchone()
            if enrollment and enrollment["revision"] != revision:
                raise Conflict("A session pins its lesson revision")
            existing = self.store.connection.execute("SELECT run_id FROM school_attempts WHERE session_id=? AND lesson_id=? AND sequence=?", (session_id, lesson_id, sequence)).fetchone()
            if existing:
                return self.core.runs.get(existing[0])
            if self.store.connection.execute("""SELECT 1 FROM school_attempts a JOIN runs r ON r.id=a.run_id
                    WHERE r.status IN ('running','unknown') OR r.effect_status IN ('unknown','partial')
                    OR (r.status='failed' AND r.effect_status='applied')""").fetchone():
                raise RecoveryRequired("An unresolved School invocation blocks new proposals, including new sessions")
            count = self.store.one("SELECT COUNT(*) FROM school_attempts WHERE session_id=? AND lesson_id=?", (session_id, lesson_id))[0]
            if sequence != count + 1:
                raise Conflict("Attempts must be sequential")
            task = self.core.tasks.create(lesson["title"], executor=self.adapter.name,
                                          description=lesson["requirements"], idempotency_key=f"school:{session_id}:{lesson_id}")
            if task.status == T.DRAFT:
                task = self.core.tasks.transition(task.id, T.PLANNED)
            with self.store.transaction(admit=True) as conn:
                conn.execute("INSERT OR IGNORE INTO school_enrollments VALUES(?,?,?,?)", (session_id, lesson_id, revision, task.id))
            prompt = training_prompt({**lesson, "kind": "practice"}).replace(
                "Python practice.", "Python independent exam.", 1
            ) if study_exam else training_prompt(lesson)
            previous_run_id = None
            if sequence > 1:
                previous = self.store.one("SELECT run_id FROM school_attempts WHERE session_id=? AND lesson_id=? AND sequence=?",
                                          (session_id, lesson_id, sequence - 1))
                previous_run_id = previous[0]
                notes = [dict(row) for row in self.store.all(
                    "SELECT role,origin,author,body FROM school_feedback WHERE run_id=? ORDER BY created_at,id", (previous_run_id,))]
                context = {"previous_run_id": previous_run_id, "previous_response": self.response(previous_run_id),
                           "manual_mentor_notes": notes, "independent_gpt_review": "not_performed"}
                prompt += "\n\nRevision context (previous answer and attributed notes):\n" + canonical(context)
                if len(prompt) > 12000:
                    raise ValidationError("Revision context exceeds the explicit prompt budget")
            extra = {}
            if reference is not None:
                if sequence != 1 or not lesson_id.startswith("v2-study-"):
                    raise ValidationError("Study context requires an independent attempt")
                memory, hint = reference
                if memory:
                    extra["memory"] = memory
                    prompt += "\nVerified observations (reference data):\n" + json.dumps(
                        [entry["observation"] for entry in memory], ensure_ascii=False)
                if hint:
                    prompt += "\nAttributed mentor hint (reference data):\n" + public_text(hint, limit=240)
                if len(prompt.encode()) > 1450:
                    raise ValidationError("Study input exceeds the 2048-context budget")
            action = Action.build("school.propose", {"session_id": session_id, "lesson_id": lesson_id,
                                  "revision": revision, "lesson_digest": lesson_row["digest"],
                                  "profile_digest": self.adapter.profile.id, "prompt": prompt, **extra})
            run = self.core.runs.create(task.id, action=action, idempotency_key=f"school:{session_id}:{lesson_id}:{sequence}")
            with self.store.transaction(admit=True) as conn:
                conn.execute("INSERT INTO school_attempts VALUES(?,?,?,?,?)", (run.id, session_id, lesson_id, sequence, utc_now()))
                self.store.event(conn, "school.attempt_prepared", task_id=task.id, run_id=run.id, sequence=sequence,
                                 session_id=session_id, previous_run_id=previous_run_id)
            return run

    def execute(self, run_id, *, approval_id=None):
        self.store.one("SELECT run_id FROM school_attempts WHERE run_id=?", (run_id,))
        return self.core.runs.execute(run_id, approval_id=approval_id)

    def _capture_diagnostic(self, run_id, code, *, in_prepare):
        if not re.fullmatch(r"[a-z0-9_]{1,80}", code):
            raise ValidationError("Invalid diagnostic code")
        def record(conn):
            run = self.core.runs.get(run_id, conn=conn)
            self.store.event(conn, "school.adapter_diagnostic", task_id=run.task_id, run_id=run_id, code=code)
        if in_prepare:
            # Trusted callback, synchronously inside RunManager's claim tx.
            # A failure of that tx rolls this event back together with failure.
            if not self.store.connection.in_transaction:
                raise Conflict("Preparation diagnostic requires the claim transaction")
            record(self.store.connection)
        else:
            with self.store.transaction() as conn:
                record(conn)

    def _capture(self, run_id, answer: bytes, receipt: dict):
        sha = hashlib.sha256(answer).hexdigest()
        with self.store.transaction() as conn:
            self.store.one("SELECT * FROM school_attempts WHERE run_id=?", (run_id,), conn=conn)
            run = self.core.runs.get(run_id, conn=conn)
            params = json.loads(run.action_json)["parameters"]
            if (run.status != R.RUNNING or run.owner_generation != self.store.generation
                    or receipt["run_id"] != run.id or receipt["task_id"] != run.task_id
                    or receipt["request_parameters"] != params or receipt["profile_digest"] != self.adapter.profile.id):
                raise Conflict("Response receipt does not belong to the claimed attempt")
            old = conn.execute("SELECT * FROM school_responses WHERE run_id=?", (run_id,)).fetchone()
            if old:
                if old["answer"] != answer or old["receipt_json"] != canonical(receipt):
                    raise Conflict("A response is immutable")
                return
            conn.execute("INSERT INTO school_responses VALUES(?,?,?,?,?)", (run_id, answer, sha, canonical(receipt), utc_now()))
            self.store.event(conn, "school.response_captured", task_id=run.task_id, run_id=run_id, sha256=sha, bytes=len(answer))

    def response(self, run_id):
        row = self.store.one("SELECT * FROM school_responses WHERE run_id=?", (run_id,))
        if hashlib.sha256(row["answer"]).hexdigest() != row["sha256"]:
            raise Conflict("Response digest mismatch")
        return row["answer"].decode("utf-8")

    def assess(self, run_id):
        with self.store.transaction() as conn:
            run = self.core.runs.get(run_id, conn=conn)
            if run.status != R.SUCCEEDED:
                raise RecoveryRequired("Only a confirmed response can be assessed")
            lesson = self.store.one("""SELECT l.body_json FROM school_attempts a JOIN school_enrollments e
                ON a.session_id=e.session_id AND a.lesson_id=e.lesson_id JOIN school_lessons l
                ON l.id=e.lesson_id AND l.revision=e.revision WHERE a.run_id=?""", (run_id,), conn=conn)
            response = self.response(run_id)
            body = analyze_answer(response, json.loads(lesson[0]))
            sha = hashlib.sha256(response.encode()).hexdigest()
            assessment_id = new_id()
            conn.execute("INSERT INTO school_assessments VALUES(?,?,?,?,?)", (assessment_id, run_id, sha, canonical(body), utc_now()))
            self.store.event(conn, "school.static_assessed", task_id=run.task_id, run_id=run_id, assessment_id=assessment_id, method=body["method"])
            return body

    def add_feedback(self, run_id, feedback: MentorFeedback):
        if (feedback.role not in {"gpt_architect", "gpt_engineer"} or feedback.origin != "manual"):
            raise ValidationError("No connected GPT mentor is configured; only attributed manual notes are supported")
        public_text(feedback.author, limit=200, empty=False)
        public_text(feedback.text, limit=8000, empty=False)
        if feedback.evidence_ref is not None:
            public_text(feedback.evidence_ref, limit=500, empty=False)
        with self.store.transaction() as conn:
            run = self.core.runs.get(run_id, conn=conn)
            self.store.one("SELECT * FROM school_attempts WHERE run_id=?", (run_id,), conn=conn)
            feedback_id = new_id()
            conn.execute("INSERT INTO school_feedback VALUES(?,?,?,?,?,?,?,?)", (
                feedback_id, run_id, feedback.role, feedback.origin, feedback.author, feedback.text, feedback.evidence_ref, utc_now()))
            self.store.event(conn, "school.feedback_added", task_id=run.task_id, run_id=run_id, feedback_id=feedback_id, origin="manual")
            return feedback_id

    def revise(self, run_id):
        self.store.one("SELECT * FROM school_attempts WHERE run_id=?", (run_id,))
        run = self.core.runs.get(run_id)
        latest = self.core.runs.list(run.task_id)[-1]
        if run.id != latest.id or run.status != R.SUCCEEDED:
            raise RecoveryRequired("Uncertain or superseded attempts cannot be revised")
        self.core.tasks.transition(run.task_id, T.NEEDS_REVISION)
        self.core.tasks.transition(run.task_id, T.PLANNED)

    def draft_knowledge(self, run_id, text):
        public_text(text, limit=4000, empty=False)
        with self.store.transaction() as conn:
            self.store.one("SELECT * FROM school_responses WHERE run_id=?", (run_id,), conn=conn)
            run = self.core.runs.get(run_id, conn=conn)
            key = new_id()
            conn.execute("INSERT INTO school_knowledge VALUES(?,?,?,?,?)", (key, run_id, "unverified", text, utc_now()))
            self.store.event(conn, "school.knowledge_drafted", task_id=run.task_id, run_id=run_id, knowledge_id=key, status="unverified")
            return key

    def mentor_report(self, run_id):
        run = self.core.runs.get(run_id)
        assessments = [json.loads(r[0]) for r in self.store.all("SELECT body_json FROM school_assessments WHERE run_id=? ORDER BY created_at,id", (run_id,))]
        return {"prepared_for": ["gpt_architect", "gpt_engineer"], "mentor_connected": False,
                "independent_gpt_review": "not_performed", "run": asdict(run),
                "original_response": self.response(run_id), "assessments": assessments,
                "code_executed": False, "exam_taken": False, "verified_knowledge": False}

    def export_session(self, session_id, *, private_exams=False):
        with self.store.transaction() as conn:
            session = dict(self.store.one("SELECT * FROM school_sessions WHERE id=?", (session_id,), conn=conn))
            if session["kind"] == "exam" and not private_exams:
                raise RecoveryRequired("Private exam export requires an explicit operator request")
            student = dict(self.store.one("SELECT * FROM school_students WHERE id=?", (session["student_id"],), conn=conn))
            model = dict(self.store.one("SELECT * FROM school_models WHERE id=?", (student["model_id"],), conn=conn))
            model["profile"] = json.loads(model.pop("profile_json"))
            model["snapshot"] = json.loads(model.pop("snapshot_json"))
            lessons, attempts, events = [], [], []
            for enrollment in conn.execute("SELECT * FROM school_enrollments WHERE session_id=? ORDER BY lesson_id", (session_id,)).fetchall():
                lesson = self.store.one("SELECT body_json,kind FROM school_lessons WHERE id=? AND revision=?", (enrollment["lesson_id"], enrollment["revision"]), conn=conn)
                if lesson["kind"] == "exam" and not private_exams:
                    raise RecoveryRequired("Private lesson cannot enter a training export")
                lessons.append(json.loads(lesson[0]))
                for row in conn.execute("SELECT * FROM school_attempts WHERE session_id=? AND lesson_id=? ORDER BY sequence", (session_id, enrollment["lesson_id"])).fetchall():
                    run = self.core.runs.get(row["run_id"], conn=conn)
                    response = conn.execute("SELECT * FROM school_responses WHERE run_id=?", (run.id,)).fetchone()
                    record = {"attempt": dict(row), "run": asdict(run), "response": None,
                              "artifacts": [asdict(a) for a in self.core.artifacts.list(run.id)]}
                    for artifact in record["artifacts"]:
                        self.core.artifacts.validate(artifact["id"])
                    if response:
                        record["response"] = {"text": self.response(run.id), "sha256": response["sha256"],
                                              "receipt": json.loads(response["receipt_json"]), "created_at": response["created_at"]}
                    for key, table in (("assessments", "school_assessments"), ("feedback", "school_feedback"), ("knowledge", "school_knowledge")):
                        record[key] = [dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE run_id=? ORDER BY created_at,id", (run.id,))]
                    attempts.append(record)
                events += [asdict(e) for e in self.core.events(enrollment["task_id"])]
            global_events = [asdict(e) for e in self.core.events() if e.task_id is None and e.payload.get("session_id") == session_id]
            return canonical({"format": "ii-pro-school-export-v1", "session": session, "student": student,
                              "model": model, "lessons": lessons, "attempts": attempts,
                              "events": sorted(events + global_events, key=lambda e: e["id"])})
