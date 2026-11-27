"""UI commands are a facade over School/Core, never a second task engine.

Real inference and Worker execution each require their own exact startup Run
permit followed by Core Approval. No browser route can grant those permits.
"""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import threading
import time

from ii_pro_core.errors import Conflict, RecoveryRequired, ValidationError
from ii_pro_core import Core
from ii_pro_core.executors import ExecutionRequest, MockExecutor
from ii_pro_core.operations import ExecutionPolicy
from ii_pro_core.security import Action, digest
from ii_pro_school.adapters import ModelProfile, StudentAdapter, OllamaStudentAdapter
from ii_pro_school.curriculum import initial_lessons
from ii_pro_school.school import School

from .reader import public


TARGET = "qwen7b-fast:latest"
MODEL_ID = "mock-qwen7b"
PROFILE = ModelProfile.build("mock-qwen7b-ui:fixture", digest({"fixture": "ui-v03"}))
REAL_ID = "ollama-qwen7b"
REAL_PROFILE = ModelProfile.build(
    TARGET, "e272f2e7e6b42653ba8f199076db6a3a0e71ae518a2eb7edcdff8fb8b764aa4f"
)


class UIQwenStudent(OllamaStudentAdapter):
    name = "school_ollama"
    revision = "ui-ollama-text-v1"

    def __init__(self, profile=None):
        super().__init__(profile or REAL_PROFILE)

    def approval_configuration(self):
        return {
            **super().approval_configuration(),
            "ui_mode": "ollama_real",
            "artifact_storage": "sqlite",
            "code_execution": False,
        }

    def execute(self, request):
        # Reuse the existing one-shot Ollama client and durable raw capture.
        # Publish bytes through the existing sealed BLOB importer, no host path.
        from dataclasses import replace

        return replace(super().execute(request), storage="sqlite")


TITLES = {
    "bug-positive-sum": "Исправление суммы положительных чисел",
    "write-clamp": "Функция ограничения целого числа",
    "improve-unique": "Улучшение удаления повторов",
}
FIXTURES = {
    "bug-positive-sum": "```python\ndef sum_positive(values):\n    total = 0\n    for value in values:\n        if value > 0:\n            total += value\n    return total\n```\nФиксированный тестовый ответ MockExecutor; Qwen не вызывалась.",
    "write-clamp": "```python\ndef clamp_int(value, lower, upper):\n    if any(type(x) is not int for x in (value, lower, upper)):\n        raise TypeError()\n    if lower > upper:\n        raise ValueError()\n    return min(upper, max(lower, value))\n```\nФиксированный тестовый ответ MockExecutor; Qwen не вызывалась.",
    "improve-unique": "```python\ndef unique_strings(values):\n    seen = set()\n    result = []\n    for value in values:\n        if value not in seen:\n            seen.add(value)\n            result.append(value)\n    return result\n```\nФиксированный тестовый ответ MockExecutor; Qwen не вызывалась.",
}


class MockSchoolStudent(StudentAdapter):
    revision = "ui-school-mock-v1"

    def __init__(self):
        super().__init__(PROFILE)
        self.mock = MockExecutor()

    def approval_configuration(self):
        return {
            "profile": asdict(self.profile),
            "backend": "MockExecutor",
            "fixtures_sha256": digest(FIXTURES),
            "real_inference": False,
            "delegate_revision": self.mock.revision,
            "delegate_configuration": self.mock.approval_configuration(),
        }

    def probe(self):
        return {"backend": "MockExecutor", "real_inference": False}

    def respond(self, prompt):
        raise ValidationError("Use the bound mock execution request")

    def execute(self, request):
        params = json.loads(request.action.parameters_json)
        # Fixed fixture passed as data to the existing MockExecutor. No eval,
        # subprocess, model transport or Worker is involved.
        text = FIXTURES[params["lesson_id"]]
        mock_request = ExecutionRequest(
            request.task_id,
            request.run_id,
            request.title,
            request.description,
            Action.build("mock.echo", {"text": text}),
        )
        started = time.monotonic()
        result = self.mock.execute(mock_request)
        reply = json.loads(result.artifacts[0].content)["text"]
        receipt = {
            "backend": "MockExecutor",
            "real_inference": False,
            "complete": True,
            "elapsed_seconds": time.monotonic() - started,
            "run_id": request.run_id,
            "task_id": request.task_id,
            "profile": asdict(self.profile),
            "profile_digest": self.profile.id,
            "request_parameters": params,
            "prompt_sha256": hashlib.sha256(params["prompt"].encode()).hexdigest(),
            "snapshot": self.snapshots.pop(request.run_id),
        }
        self.capture(request.run_id, reply.encode(), receipt)
        from ii_pro_core.executors import ExecutionResult, ProducedArtifact
        from ii_pro_core.security import canonical

        return ExecutionResult(
            (
                ProducedArtifact("response.txt", reply.encode()),
                ProducedArtifact("receipt.json", canonical(receipt).encode()),
            ),
            storage="sqlite",
        )


class Training:
    def __init__(
        self,
        root: Path,
        session_key: str,
        *,
        adapter_factory=MockSchoolStudent,
        include_ollama=False,
        real_adapter_factory=UIQwenStudent,
        authorized_real_run=None,
        include_worker=False,
        authorized_worker_run=None,
        worker_factory=None,
        include_team=False,
        team_adapter_factory=None,
        text_adapter_factory=None,
        authorized_text_run=None,
        gpt_auth_dir=None,
        local_projects=None,
        local_live=False,
        engineering_worker=None,
    ):
        self.root = Path(root)
        self.factory = adapter_factory
        if authorized_real_run is not None and (
            not isinstance(authorized_real_run, str)
            or not re.fullmatch(r"[a-f0-9]{32}", authorized_real_run)
        ):
            raise ValidationError("A single exact real Run permit is required")
        self.include_ollama = include_ollama
        self.real_factory = real_adapter_factory
        self._authorized_real_run = authorized_real_run
        self.include_worker = include_worker
        if include_worker and not include_ollama:
            raise ValidationError(
                "Functional UI requires the shared School composition"
            )
        self.authorized_worker_run = authorized_worker_run
        self.worker_factory = worker_factory
        self.include_team = include_team
        self.team_adapter_factory = team_adapter_factory
        self.text_adapter_factory = text_adapter_factory
        if authorized_text_run is not None and (
            not isinstance(authorized_text_run, str)
            or not re.fullmatch(r"[a-f0-9]{32}", authorized_text_run)
        ):
            raise ValidationError("An exact Text Run permit is required")
        self.authorized_text_run = authorized_text_run
        self.gpt_auth_dir = gpt_auth_dir
        self.team = None
        self.local_engine = None
        self.local_projects = local_projects or {}
        self.local_live = local_live
        self.engineering_worker = engineering_worker
        self.diagnostics = None
        if include_team and not include_ollama:
            raise ValidationError("Team requires the existing shared Core composition")
        if self.local_projects and not include_ollama:
            raise ValidationError("Local Engineer requires the existing shared Core")
        self.functional = None
        self.education = None
        self.schools = {}
        self.shared_core = None
        self.policy = ExecutionPolicy(
            permissions_revision="ui-session-"
            + hashlib.sha256(session_key.encode()).hexdigest()[:32],
            approval_actions=("mock.protected_echo", "school.propose"),
        )
        self.lock = threading.RLock()
        self.school = None
        self.threads = {}  # thread lifetime only; all Run states belong to Core
        self.stopping = False
        if (self.root / "core.sqlite3").exists():
            self._open()  # recover only this dedicated UI workspace, never exam DBs

    def _open(self):
        if self.school is None:
            if self.include_ollama:
                mock, real = self.factory(), self.real_factory()
                worker = None
                if self.include_worker:
                    from .functional import build_worker

                    worker = (self.worker_factory or build_worker)(
                        self.root, self.authorized_worker_run
                    )
                team_executors = {}
                diagnostic_executor = None
                diagnostic_verifier = None
                if self.include_team:
                    from ii_pro_team.adapters import MockAgentAdapter, TeamExecutor
                    from ii_pro_team.workspace import Laboratory

                    laboratory = Laboratory(self.root / "team-workspaces")
                    factory = self.team_adapter_factory or MockAgentAdapter
                    team_executors = {
                        role: TeamExecutor(role, factory(laboratory))
                        for role in ("architect", "engineer")
                    }
                    from ii_pro_team.text import TextExecutor, build_text

                    text_executor = (
                        TextExecutor(
                            self.text_adapter_factory(), self.authorized_text_run
                        )
                        if self.text_adapter_factory
                        else build_text(
                            self.root,
                            self.authorized_text_run,
                            auth_dir=self.gpt_auth_dir,
                        )
                    )
                    from ii_pro_team.diagnostics import (
                        DiagnosticExecutor,
                        MetadataReconciler,
                    )

                    diagnostic_executor = DiagnosticExecutor(
                        self.root / "codex-diagnostics"
                    )
                    diagnostic_verifier = MetadataReconciler()
                from .local_engine import build_executors
                from ii_pro_core.knowledge import KnowledgeExecutor

                local_executors = build_executors(self.local_projects, REAL_PROFILE)
                knowledge = KnowledgeExecutor()
                from ii_pro_local.engineering_executor import EngineeringExecutor
                from ii_pro_local.recovery import LocalRecovery

                copy_executor = EngineeringExecutor(self.local_projects)
                local_recovery = LocalRecovery() if local_executors else None
                core = Core(
                    self.root,
                    executors=[mock, real]
                    + ([worker] if worker else [])
                    + list(team_executors.values())
                    + ([text_executor] if self.include_team else [])
                    + ([diagnostic_executor] if diagnostic_executor else [])
                    + list(local_executors.values())
                    + ([local_recovery] if local_recovery else [])
                    + ([copy_executor] if self.include_team else [])
                    + ([self.engineering_worker] if self.engineering_worker else [])
                    + ([knowledge] if self.include_worker else []),
                    policy=self.policy,
                    reconcilers=[diagnostic_verifier] if diagnostic_verifier else [],
                )
                try:
                    school = School(self.root, mock, core=core)
                    real_school = School(self.root, real, core=core)
                    real_school.register_student(
                        "ui-qwen",
                        "Qwen7B — Ollama, реальная генерация",
                        defer_probe=True,
                    )
                except BaseException:
                    core.close()
                    raise
                self.shared_core = core
                if local_recovery:
                    local_recovery.core = core
                self.schools[REAL_ID] = real_school
            else:
                school = School(self.root, self.factory(), policy=self.policy)
            try:
                school.install_curriculum()
                school.register_student(
                    "ui-mock", "MockExecutor — тест интерфейса, не Qwen"
                )
            except BaseException:
                school.close()
                if self.shared_core:
                    self.shared_core.close()
                raise
            self.school = school
            self.schools[MODEL_ID] = school
            if self.shared_core is not None:
                from .local_engine import LocalEngine

                self.local_engine = LocalEngine(
                    self,
                    core,
                    self.local_projects,
                    local_executors,
                    live=self.local_live,
                )
            if self.include_team:
                from ii_pro_team.team import Team

                self.team = Team(self, team_executors, laboratory, text_executor)
                from ii_pro_team.workbench import Workbench

                copy_executor.core = core
                if self.engineering_worker:
                    self.engineering_worker.core = core
                    if hasattr(self.engineering_worker, "bind"):
                        self.engineering_worker.bind(core)
                self.team.workbench = Workbench(self.team, copy_executor, self.engineering_worker)
                if self.engineering_worker and hasattr(self.engineering_worker, "project_authorize"):
                    self.engineering_worker.project_authorize = self.team.projects.admit_test
                from ii_pro_team.diagnostics import Diagnostics

                self.diagnostics = Diagnostics(core, diagnostic_executor)
                diagnostic_verifier.core = core
            if self.include_worker:
                from .functional import FunctionalLessons

                try:
                    self.functional = FunctionalLessons(
                        self, worker, self.authorized_worker_run
                    )
                    from .education import Education

                    self.education = Education(self, self.functional.domains[REAL_ID], knowledge)
                except BaseException:
                    self.close()
                    raise
        return self.school

    def _school_for(self, run_id):
        run = self.school.core.runs.get(run_id)
        return next(s for s in self.schools.values() if s.adapter.name == run.executor)

    def prepare(self, body):
        if set(body) != {"request_id", "lesson_id", "model_id"}:
            raise ValidationError("Invalid preparation fields")
        key, lesson, model = (body[k] for k in ("request_id", "lesson_id", "model_id"))
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{32}", key):
            raise ValidationError("Invalid request ID")
        if (
            not isinstance(lesson, str)
            or lesson not in TITLES
            or model not in ({MODEL_ID, REAL_ID} if self.include_ollama else {MODEL_ID})
        ):
            raise ValidationError("Only registered practice profiles are enabled")
        with self.lock:
            if self.team and self.team.blocked():
                raise RecoveryRequired("Unresolved Team execution blocks lessons")
            if self.stopping:
                raise Conflict("Studio is closing")
            self._open()
            if self.functional and self.functional.blocked():
                raise RecoveryRequired(
                    "Unresolved Worker execution blocks model proposals"
                )
            school = self.schools[model]
            with school.store.lock:
                enrolled = school.store.all(
                    "SELECT lesson_id FROM school_enrollments WHERE session_id=?",
                    (key,),
                )
                if enrolled and any(r[0] != lesson for r in enrolled):
                    raise Conflict("Request ID already binds another lesson")
                if school.store.one("SELECT COUNT(*) FROM school_sessions")[
                    0
                ] >= 100 and not school.store.all(
                    "SELECT 1 FROM school_sessions WHERE id=?", (key,)
                ):
                    raise Conflict("UI session quota reached")
                school.create_session(
                    "ui-qwen" if model == REAL_ID else "ui-mock", session_id=key
                )
                run = school.prepare_attempt(key, lesson)
                if run.status == "created":
                    school.core.approvals.request(
                        run.id, ttl_seconds=3600 if model == REAL_ID else 300
                    )
                return self._attempt(run.id)

    def decide(self, body):
        if (
            set(body) != {"run_id", "approval_id", "spec_digest", "allow"}
            or type(body["allow"]) is not bool
        ):
            raise ValidationError("Explicit scoped decision required")
        for field, length in (("run_id", 32), ("approval_id", 32), ("spec_digest", 64)):
            if not isinstance(body[field], str) or not re.fullmatch(
                r"[a-f0-9]{%d}" % length, body[field]
            ):
                raise ValidationError("Invalid operation identity")
        with self.lock:
            if self.stopping or self.school is None:
                raise Conflict("Prepare an operation in the current local session")
            school = self._school_for(body["run_id"])
            run = school.core.runs.get(body["run_id"])
            school.store.one("SELECT 1 FROM school_attempts WHERE run_id=?", (run.id,))
            permit = school.core.approvals.get(body["approval_id"])
            if permit.run_id != run.id or permit.spec_digest != body["spec_digest"]:
                raise Conflict("Approval scope differs")
            if run.status == "unknown":
                raise RecoveryRequired(
                    "Unknown requires explicit reconciliation outside this UI"
                )
            # Duplicate delivery only observes; never makes a second attempt.
            if run.id in self.threads or run.status != "created":
                return self._attempt(run.id)
            if self.threads:
                raise Conflict("Another UI invocation is still active")
            if self.team and self.team.blocked():
                raise RecoveryRequired(
                    "Unresolved Team execution blocks model proposals"
                )
            if self.functional and self.functional.blocked(run.id):
                raise RecoveryRequired(
                    "Unresolved Worker execution blocks model proposals"
                )
            if body["allow"] and self.education:
                self.education.require_pilot_order(run.id)
            if (
                body["allow"]
                and school is self.schools.get(REAL_ID)
                and run.id != self._authorized_real_run
            ):
                raise Conflict(
                    "Real inference requires separate user permission for this exact Run"
                )
            if school.store.all("""SELECT 1 FROM school_attempts a JOIN runs r ON r.id=a.run_id
                    WHERE r.status='unknown' OR r.effect_status IN ('unknown','partial')
                    OR (r.status='failed' AND r.effect_status='applied') LIMIT 1"""):
                raise RecoveryRequired(
                    "An unresolved invocation blocks dispatch of prepared lessons too"
                )
            if not school.core.approvals.matches(
                asdict(run), school.core.approvals.spec_for(permit.id)
            ):
                raise Conflict("Operation or local session changed; prepare again")
            if permit.status in {"rejected", "revoked", "expired", "invalidated"}:
                raise Conflict("Prepare a fresh approval before a new decision")
            school.core.approvals.decide(permit.id, allow=body["allow"])
            if body["allow"]:
                thread = threading.Thread(
                    target=self._execute,
                    args=(run.id, permit.id),
                    name="school-ui-" + run.id,
                )
                self.threads[run.id] = thread
                try:
                    thread.start()
                except BaseException:
                    self.threads.pop(run.id)
                    school.core.approvals.revoke(
                        permit.id, reason="dispatch_not_started"
                    )
                    raise
            return self._attempt(run.id)

    def _execute(self, run_id, approval_id):
        school = self._school_for(run_id)
        try:
            result = school.execute(run_id, approval_id=approval_id)
            if result.status == "succeeded":
                school.assess(run_id)
        except Exception:
            # Core owns failure/unknown. Never infer 'failed' from a UI exception,
            # retry, synthesize a score, or overwrite a persisted response.
            with self.school.store.transaction() as conn:
                self.school.store.event(
                    conn, "ui.lesson_completion_unconfirmed", run_id=run_id
                )
        finally:
            with self.lock:
                self.threads.pop(run_id, None)

    def _attempt(self, run_id):
        s = self._school_for(run_id)
        with s.store.lock:
            run = s.core.runs.get(run_id)
            row = s.store.one("SELECT * FROM school_attempts WHERE run_id=?", (run_id,))
            permit = s.store.connection.execute(
                "SELECT * FROM approvals WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            response = s.store.connection.execute(
                "SELECT * FROM school_responses WHERE run_id=?", (run_id,)
            ).fetchone()
            real = s is self.schools.get(REAL_ID)
            receipt = json.loads(response["receipt_json"]) if response else {}
            spec = s.core.approvals.describe(permit["id"]) if permit else None
            config = spec["executor"]["configuration"] if spec else {}
            profile = (
                receipt.get("profile")
                or config.get("model_profile")
                or config.get("profile")
                or asdict(s.adapter.profile)
            )
            if (
                digest(profile)
                != json.loads(run.action_json)["parameters"]["profile_digest"]
            ):
                raise Conflict("Recorded profile identity differs from the request")
            raw = s.response(run.id) if response else None
            shown = public(raw, 65536) if raw is not None else None
            if run.status == "succeeded":
                s.core.artifacts.validate_run(run.id)
            assessment = s.store.connection.execute(
                "SELECT body_json FROM school_assessments WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            phase = (
                "dispatching"
                if run_id in self.threads and run.status == "created"
                else str(run.status)
            )
            return {
                "run_id": run.id,
                "task_id": run.task_id,
                "session_id": row["session_id"],
                "lesson_id": row["lesson_id"],
                "attempt": row["sequence"],
                "status": str(run.status),
                "phase": phase,
                "effect_status": str(run.effect_status),
                "error_code": run.error_code,
                "created_at": run.created_at,
                "backend": "Ollama Qwen7B" if real else "MockExecutor",
                "real_inference": real,
                "model_id": REAL_ID if real else MODEL_ID,
                "executor": run.executor,
                "dispatch_allowed": not real or run.id == self._authorized_real_run,
                "profile": profile,
                "elapsed_seconds": receipt.get("elapsed_seconds"),
                "statistics": receipt.get("statistics", {}),
                "prompt_sha256": hashlib.sha256(
                    json.loads(run.action_json)["parameters"]["prompt"].encode()
                ).hexdigest(),
                "executor_revision": spec["executor"]["revision"]
                if spec
                else s.adapter.revision,
                "policy_revision": spec["policy"]["revision"]
                if spec
                else self.policy.revision,
                "approval": None
                if permit is None
                else {
                    k: permit[k] for k in ("id", "status", "spec_digest", "expires_at")
                },
                "prompt": json.loads(run.action_json)["parameters"]["prompt"],
                "response": shown,
                "response_redacted": shown != raw,
                "response_sha256": response["sha256"] if response else None,
                "assessment": json.loads(assessment[0]) if assessment else None,
                "diagnostics": [
                    json.loads(r[0]).get("code", "completion_unconfirmed")
                    for r in s.store.all(
                        "SELECT payload_json FROM events WHERE run_id=? AND kind IN ('school.adapter_diagnostic','ui.lesson_completion_unconfirmed') ORDER BY id",
                        (run.id,),
                    )
                ],
                "artifacts": [
                    {k: r[k] for k in ("id", "relative_path", "sha256", "size")}
                    for r in s.store.all(
                        "SELECT * FROM artifacts WHERE run_id=?", (run.id,)
                    )
                ],
            }

    def state(self):
        with self.lock:
            attempts = (
                []
                if self.school is None
                else [
                    self._attempt(r[0])
                    for r in self.school.store.all(
                        "SELECT run_id FROM school_attempts ORDER BY rowid DESC LIMIT 100"
                    )
                ]
            )
            return {
                "mode": "mock_and_ollama" if self.include_ollama else "mock_only",
                "functional_preparation_enabled": self.include_worker,
                "functional_checks": self.functional.state() if self.functional else [],
                "real_inference_enabled": self.include_ollama
                and self._authorized_real_run is not None,
                "authorized_real_run": self._authorized_real_run,
                "code_execution_enabled": self.include_worker
                and self.authorized_worker_run is not None,
                "authorized_worker_run": self.authorized_worker_run,
                "models": [
                    {
                        "id": MODEL_ID,
                        "label": "MockExecutor — тестовое выполнение",
                        "target": TARGET,
                        "actual_model": PROFILE.model,
                        "options": PROFILE.options,
                        "deadline_seconds": 180,
                    }
                ]
                + (
                    [
                        {
                            "id": REAL_ID,
                            "label": "Ollama Qwen7B — реальная генерация",
                            "target": TARGET,
                            "actual_model": REAL_PROFILE.model,
                            "expected_digest": REAL_PROFILE.expected_digest,
                            "options": REAL_PROFILE.options,
                            "deadline_seconds": REAL_PROFILE.deadline_seconds,
                        }
                    ]
                    if self.include_ollama
                    else []
                ),
                "lessons": [
                    {**l, "title_ru": TITLES[l["id"]]}
                    for l in initial_lessons()
                    if l["kind"] == "practice"
                ],
                "attempts": attempts,
                "stopping": self.stopping,
            }

    def close(self):
        with self.lock:
            self.stopping = True
            threads = list(self.threads.values())
        # Join outside locks; Core retains ownership throughout completion.
        for thread in threads:
            thread.join()
        if self.school:
            if self.shared_core:
                self.shared_core.close(timeout=5)
                for school in self.schools.values():
                    school.close()
            else:
                self.school.close(timeout=5)
