"""Replaceable provider interface and the Core executor bridge."""

from abc import ABC, abstractmethod
import json
import threading
from pathlib import Path
import hashlib
import time

from ii_pro_core.errors import Conflict
from ii_pro_core.executors import Executor, ExecutionResult, ProducedArtifact
from ii_pro_core.security import canonical, digest
from .protocol import event, validate_request
from .workspace import CONTRACT


class AgentAdapter(ABC):
    @abstractmethod
    def configuration(self):
        pass

    @abstractmethod
    def run(self, request, emit, cancellation=None):
        pass


class MockAgentAdapter(AgentAdapter):
    """Fixed developer demonstration, not an answer to arbitrary user tasks."""

    def __init__(self, laboratory, *, fault=None):
        self.laboratory, self.fault = laboratory, fault
        self.calls = 0

    def configuration(self):
        from .workspace import BEFORE, AFTER, TEST_SOURCE

        return {
            "backend": "mock",
            "real_inference": False,
            "fixture": "slug-words-v1",
            "fault": self.fault,
            "project": "studio-lab",
            "execution": "developer_reference_only",
            "workspace_root": str(self.laboratory.root),
            "git": {
                "executable": str(self.laboratory.git_executable),
                "sha256": self.laboratory.git_sha256,
            },
            "fixtures_digest": digest(
                {"before": BEFORE, "after": AFTER, "test": TEST_SOURCE}
            ),
            "dependencies": {
                name: hashlib.sha256(
                    Path(__file__).with_name(name).read_bytes()
                ).hexdigest()
                for name in ("workspace.py", "protocol.py")
            },
        }

    def run(self, request, emit, cancellation=None):
        self.calls += 1
        thread = "mock-" + request["session_id"]
        emit(
            event(
                "session",
                {
                    "session_id": request["session_id"],
                    "provider_thread_id": thread,
                    "role": request["role"],
                },
            )
        )
        if self.fault == "disconnect":
            emit(event("status", {"status": "connection_lost", "backend": "mock"}))
            raise ConnectionError("Mock connection lost after dispatch")
        if cancellation and cancellation.requested:
            raise RuntimeError("Cooperative cancellation observed; effects unconfirmed")
        if self.fault == "permission":
            emit(
                event(
                    "approval_request",
                    {
                        "operation": "outside_workspace",
                        "status": "blocked",
                        "decision": "not_granted",
                    },
                )
            )
            raise Conflict("Mock denied out-of-scope operation")
        phase = request["phase"]
        if phase == "plan":
            result = {
                "plan": [
                    "Проверить контракт лаборатории",
                    "Исправить регистр в отдельной копии",
                    "Проверить фиксированную реализацию и Git diff",
                ],
                "criteria": [
                    "4 проверки эталонной функции",
                    "Побайтовая идентичность файлов",
                    "Чистый Git checkpoint",
                ],
                "explanation": "Фиксированный Mock-план демонстрирует маршрут команды. Он не решает произвольный текст задания.",
                "contract": CONTRACT,
            }
        elif phase == "implement":
            emit(
                event(
                    "tool",
                    {
                        "operation": "fixed_fixture_edit",
                        "status": "started",
                        "workspace": "engineer",
                    },
                )
            )
            result = self.laboratory.implement(request["job_id"])
            emit(
                event(
                    "tool",
                    {
                        "operation": "reference_tests_and_git",
                        "status": "completed",
                        "tests_passed": sum(t["passed"] for t in result["tests"]),
                    },
                )
            )
            if self.fault == "after_edit":
                raise ConnectionError("Mock connection lost after changes")
            result["explanation"] = (
                "Добавлено lower() перед разбиением. Проверялся доверенный эталон и точные байты файлов; код workspace не импортировался."
            )
        else:
            verified = self.laboratory.review(
                request["job_id"], request["inputs"]["engineer"]
            )
            result = {
                "review": "verified_fixture",
                "verification": verified,
                "explanation": "Фиксированная проверка подтверждена наблюдаемыми данными; это не независимая GPT-экспертиза.",
            }
        result.update(
            backend="mock",
            real_inference=False,
            provider_thread_id=thread,
            session_id=request["session_id"],
        )
        emit(event("message", {"text": result["explanation"], "role": request["role"]}))
        emit(event("status", {"status": "completed", "backend": "mock"}))
        return result


class TeamExecutor(Executor):
    revision = "gpt-team-core-bridge-v1"
    supports_cancellation = True

    def __init__(self, role, adapter):
        self.role, self.adapter = role, adapter
        self.name = "team_" + role
        self.core = None
        self.stops = {}
        self.stop_lock = threading.Lock()

    def request_stop(self, run_id):
        with self.stop_lock:
            signal = self.stops.get(run_id)
            if signal is not None:
                signal.set()
            return signal is not None

    def approval_configuration(self):
        return {
            "role": self.role,
            "protocol": "team-turn-v1",
            "adapter": self.adapter.configuration(),
            "artifact_storage": "sealed-sqlite-blobs",
            "automatic_authority": False,
        }

    def prepare(self, request):
        value = validate_request(json.loads(request.action.parameters_json))
        if (
            value["role"] != self.role
            or request.action.kind != "team.turn"
            or self.core is None
        ):
            raise Conflict("Team executor scope mismatch")
        if json.loads(request.description) != value:
            raise Conflict("Team action and task snapshot differ")
        if self.adapter.configuration().get("backend") != "mock":
            raise Conflict(
                "Real Team dispatch requires the separately reviewed runtime/ownership integration"
            )

    def execute(self, request):
        value = validate_request(json.loads(request.action.parameters_json))
        events = []

        def emit(item):
            checked = event(item["kind"], item["data"])
            if (
                len(events) >= 128
                or len(canonical(events + [checked]).encode()) > 28000
            ):
                raise Conflict("Agent event quota exceeded")
            events.append(checked)
            with self.core.store.transaction() as conn:
                self.core.store.event(
                    conn,
                    "team.agent_event",
                    run_id=request.run_id,
                    task_id=request.task_id,
                    session_id=value["session_id"],
                    role=self.role,
                    sequence=len(events),
                    agent_event=checked,
                )

        signal = threading.Event()
        outer = request.cancellation

        class CombinedCancellation:
            @property
            def requested(self):
                return signal.is_set() or bool(outer and outer.requested)

        with self.stop_lock:
            self.stops[request.run_id] = signal
        started = time.monotonic()
        try:
            result = self.adapter.run(value, emit, CombinedCancellation())
        finally:
            with self.stop_lock:
                self.stops.pop(request.run_id, None)
        result["elapsed_seconds"] = time.monotonic() - started
        # Public result quota also excludes credentials. Raw provider packets are
        # never accepted as Core artifacts or as authority.
        content = canonical(result).encode()
        if len(content) > 22000:
            raise Conflict("Agent result quota exceeded")
        from ii_pro_core.security import public_text

        public_text(content.decode(), limit=22000)
        artifacts = [
            ProducedArtifact("result.json", content),
            ProducedArtifact("events.json", canonical(events).encode()),
            ProducedArtifact("request.json", canonical(value).encode()),
        ]
        if value["phase"] == "review" and result.get("review") == "verified_fixture":
            # Only fixed developer evidence may enter this v0.1 experience path.
            engineer = value["inputs"]["engineer"]
            self.adapter.laboratory.review(value["job_id"], engineer)
            experience = {
                "version": 1,
                "origin": "mock_developer_fixture",
                "status": "verified_fixture",
                "verified_knowledge": False,
                "task": value["text"],
                "contract": CONTRACT,
                "plan": value["inputs"]["plan"],
                "engineer": engineer,
                "review": result,
                "source_runs": value["inputs"]["source_runs"],
                "artifacts": value["inputs"]["evidence"],
                "user_task_completed": False,
                "errors_and_corrections": [],
                "errors_scope": "only observed successful fixture runs",
                "evidence_digest": digest(engineer),
                "hidden_reasoning_collected": False,
            }
            shadow = {
                "version": 1,
                "origin": "mock_developer_fixture",
                "status": "prepared_not_run",
                "experience_digest": digest(experience),
                "task": "Самостоятельно исправить slug_words согласно открытому контракту.",
                "contract": CONTRACT,
                "workspace": "qwen",
                "teacher_solution_visible": False,
                "model_called": False,
                "verified_knowledge": False,
            }
            artifacts += [
                ProducedArtifact("experience.json", canonical(experience).encode()),
                ProducedArtifact("shadow-task.json", canonical(shadow).encode()),
            ]
        return ExecutionResult(tuple(artifacts), storage="sqlite")
