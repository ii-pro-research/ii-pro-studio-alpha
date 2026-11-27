"""Core Approval for one evidence-backed knowledge card."""

import json

from .errors import Conflict, ValidationError
from .executors import ExecutionResult, Executor, ProducedArtifact
from .security import canonical
from ii_pro_worker.school_contract import validate_result


class KnowledgeExecutor(Executor):
    name = "knowledge_review"
    revision = "knowledge-review-v1"

    def __init__(self):
        self.core = None

    def approval_configuration(self):
        return {"target": "tested_to_verified", "source": "confirmed sealed Worker result"}

    def evidence(self, entry):
        body = entry["body"]
        source = body["source"]
        if "category" not in body or "run_id" not in source:
            raise ValidationError("Functional evidence backing a knowledge card is required")
        run = self.core.runs.get(source["run_id"])
        if run.task_id != source["task_id"] or run.status != "succeeded" or not run.execution_stopped:
            raise Conflict("Knowledge source is not a completed result")
        if run.executor not in {"worker", "engineering_worker"}:
            raise Conflict("A model assertion is not functional evidence")
        if not self.core.store.all("SELECT 1 FROM artifact_seals WHERE run_id=?", (run.id,)):
            raise Conflict("Functional evidence must be sealed")
        self.core.artifacts.validate_run(run.id, require_any=True)
        approvals = self.core.store.all("SELECT id FROM approvals WHERE run_id=? AND status='consumed'", (run.id,))
        if len(approvals) != 1:
            raise Conflict("Functional evidence has no exact execution Approval")
        profile = self.core.approvals.spec_for(approvals[0][0]).executor_configuration.get("backend", {})
        if profile.get("backend") != "docker-fixed":
            raise Conflict("Mock execution does not verify knowledge")
        artifacts = self.core.artifacts.list(run.id)
        found = next((a for a in artifacts if a.relative_path.endswith("/result.json")), None)
        if found is None or found.sha256 not in body["evidence"]["sha256"]:
            raise Conflict("Verified functional receipt must be cited")
        receipt = json.loads(self.core.artifacts.read(found.id))
        package = json.loads(run.action_json)["parameters"]["arguments"]["package"]
        checks = validate_result(json.loads(receipt["result"]["stdout"]), package)
        if (receipt["run_id"] != run.id or not receipt["stop_confirmed"] or receipt["process_exit_code"] != 0
                or checks["passed"] != body["evidence"]["passed"]
                or checks["total"] != body["evidence"]["total"] or not checks["passed"]):
            raise Conflict("Functional evidence differs from the card")

    def prepare(self, request):
        params = json.loads(request.action.parameters_json)
        if request.action.kind != "memory.verify" or request.operation_spec is None:
            raise Conflict("An approved knowledge action is required")
        entry = self.core.memory.get(params["entry"])
        if entry["sha256"] != params["sha256"] or entry["history"][-1]["status"] != "tested":
            raise Conflict("Knowledge or trust status changed")
        self.evidence(entry)

    def execute(self, request):
        self.prepare(request)
        params = json.loads(request.action.parameters_json)
        self.core.memory.assess(params["entry"], "verified", reviewer="Studio operator",
                                reason="Explicit approval of bounded functional evidence", approved_run=request.run_id)
        return ExecutionResult((ProducedArtifact("knowledge.json", canonical(params).encode()),), storage="sqlite")
