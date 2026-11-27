"""Approval-backed administrative closure of a proven terminal Local review."""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from ii_pro_core.errors import Conflict, CoreError
from ii_pro_core.executors import (
    ExecutionRequest,
    ExecutionResult,
    Executor,
    ProducedArtifact,
)
from ii_pro_core.security import Action, canonical, digest
from ii_pro_core.storage import utc_now

from .agent import LocalAgentExecutor
from .epoch import (
    startup_proof,
    timestamp,
    windows_boot,
    windows_startup,
)


class LocalRecovery(Executor):
    name = "local_recovery"
    revision = "local-recovery-v3"

    def __init__(self):
        self.core = None
        self._startup_sample = None

    def approval_configuration(self):
        return {"target": "one unknown bounded Local finish review", "effect": "administrative_closure_no_retry",
                "proof": "sealed terminal response or an observed Windows boot boundary on the same host",
                "epoch_source_sha256": hashlib.sha256(Path(__file__).with_name("epoch.py").read_bytes()).hexdigest()}

    @staticmethod
    def key(target, *, fence=False):
        return ("local-fence:" if fence else "local-recovery:") + target

    def _sealed(self, run):
        if not self.core.store.all("SELECT 1 FROM artifact_seals WHERE run_id=?", (run,)):
            return {}
        return {entry.relative_path.rsplit("/", 1)[-1]: entry for entry in self.core.artifacts.list(run)}

    def _consumed(self, run):
        approvals = self.core.store.all("SELECT id FROM approvals WHERE run_id=? AND status='consumed'", (run.id,))
        if len(approvals) != 1:
            raise Conflict("Exactly one consumed Approval is required")
        return self.core.approvals.get(approvals[0][0])

    def evidence(self, target):
        """Prove terminal provider response and that no Local tool was dispatched."""
        run = self.core.runs.get(target)
        agent = self.core.runs.executors.get(run.executor)
        if (run.status != "unknown" or run.effect_status != "unknown" or run.attempt != 1
                or type(agent) is not LocalAgentExecutor or agent.workflow is None):
            raise Conflict("Target is not an eligible unknown Local Run")
        action = Action.from_json(run.action_json)
        meta = json.loads(run.description)
        approval = self._consumed(run)
        spec = self.core.approvals.spec_for(approval.id)
        historic = json.loads(spec.json)
        artifacts = self._sealed(run.id)
        diagnostic = artifacts.get("diagnostic.json")
        if diagnostic is None:
            raise Conflict("Missing sealed terminal Local diagnostic")
        if (action.kind != "local.review" or not meta.get("review") or meta.get("step") != 1
                or (historic["run_id"], historic["task_id"], historic["action_hash"], historic["description"])
                != (run.id, run.task_id, run.action_hash, run.description) or action.hash != run.action_hash
                or historic["executor"]["revision"] not in {"local-agent-balanced-v3", "local-agent-balanced-v4"}
                or meta["snapshot"] != agent.snapshot.identity
                or meta["format"] != agent.workflow.response_schema(1, {}, inline=True)):
            raise Conflict("Frozen bounded review provenance differs")
        if set(artifacts) - {"diagnostic.json", "response.txt"} or self.core.store.all(
            "SELECT 1 FROM events WHERE run_id=? AND kind='local_agent.tool_checked'", (run.id,)
        ):
            raise Conflict("Local tool or other result may have been dispatched")
        facts = json.loads(self.core.artifacts.read(diagnostic.id))
        request = ExecutionRequest(run.task_id, run.id, run.title, run.description, action, operation_spec=spec)
        payload = agent._payload(request, json.loads(action.parameters_json))
        if (facts.get("request_id") != run.id or facts.get("request_sha256") != digest(payload)
                or facts.get("format_sha256") != digest(meta["format"])
                or facts.get("model") != payload["model"]
                or facts.get("model_digest") != historic["executor"]["configuration"]["profile"]["expected_digest"]
                or facts.get("executor_revision") != historic["executor"]["revision"]
                or facts.get("stage") != "response_validation" or facts.get("http_status") != 200
                or facts.get("response_complete") is not True or facts.get("done") is not True
                or (facts.get("done_reason"), facts.get("classification")) not in {
                    ("length", "generation_limit"), ("stop", "invalid_content")}
                or type(facts.get("response_bytes")) is not int or facts["response_bytes"] <= 0
                or not isinstance(facts.get("response_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", facts["response_sha256"])):
            raise Conflict("Ollama terminal response is unproven for this Run")
        original = {name: getattr(run, name) for name in ("status", "effect_status", "execution_stopped", "error_type",
                                                          "error_code", "action_hash", "updated_at")}
        return {"target": run.id, "original": original,
                "old_approval": approval.id, "old_spec": spec.digest,
                "diagnostic_sha256": diagnostic.sha256, "response_sha256": facts["response_sha256"],
                "done_reason": facts["done_reason"], "classification": facts["classification"],
                "provider_terminal_proven": True, "tool_dispatched": False, "no_retry": True}

    def _idle(self, exclude=None):
        if (set(self.core.active_run_ids) - {exclude}
                or self.core.store.all("SELECT 1 FROM runs WHERE status='running' AND id IS NOT ? LIMIT 1", (exclude,))
                or self.core.store.all("SELECT 1 FROM worker_jobs WHERE stop_confirmed=0 LIMIT 1")):
            raise Conflict("Active execution blocks Local recovery")

    def _quarantine_target(self, target):
        run = self.core.runs.get(target)
        agent = self.core.runs.executors.get(run.executor)
        if (run.status != "unknown" or run.effect_status != "unknown" or run.attempt != 1
                or not run.owner_generation or type(agent) is not LocalAgentExecutor or not agent.workflow):
            raise Conflict("Target is not an eligible historical Local review")
        approval = self._consumed(run)
        frozen = json.loads(self.core.approvals.spec_for(approval.id).json)
        meta = json.loads(run.description)
        action = Action.from_json(run.action_json)
        config = frozen["executor"]["configuration"]
        if (action.kind != "local.review" or frozen["action"] != json.loads(action.json)
                or (frozen["run_id"], frozen["task_id"], frozen["action_hash"], frozen["description"])
                != (run.id, run.task_id, action.hash, run.description) or run.action_hash != action.hash
                or frozen["executor"]["implementation"] != "ii_pro_local.agent.LocalAgentExecutor"
                or frozen["executor"]["revision"] not in {"local-agent-balanced-v2", "local-agent-balanced-v3", "local-agent-balanced-v4"}
                or meta.get("step") != 1 or not meta.get("review") or meta["snapshot"] != agent.snapshot.identity
                or meta["format"] != agent.workflow.response_schema(1, {}, inline=True)
                or config.get("source_write") is not False or not re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", config["profile"]["endpoint"])
                or set(self._sealed(run.id)) - {"diagnostic.json", "response.txt"}
                or self.core.store.all("SELECT 1 FROM events WHERE run_id=? AND kind='local_agent.tool_checked'", (run.id,))):
            raise Conflict("Historical operation is outside the bounded Local finish contract")
        return {"target": run.id, "action_hash": run.action_hash, "original_status": str(run.status),
                "old_owner": run.owner_generation, "old_approval": approval.id, "old_spec": approval.spec_digest,
                "original_stop_claim": bool(run.execution_stopped), "updated_at": run.updated_at}

    def _witness(self, target):
        rows = self.core.store.all("""SELECT id, payload_json FROM events WHERE kind='local.boot_witness'
            AND json_extract(payload_json,'$.target')=? ORDER BY id""", (target,))
        if len(rows) != 1:
            raise Conflict("Exactly one pre-restart boot witness is required")
        return rows[0]["id"], json.loads(rows[0]["payload_json"])

    def witness(self, target):
        """Record the current boundary before a separate, operator-controlled restart."""
        with self.core.store.transaction(admit=True) as conn:
            self._idle()
            proof = self._quarantine_target(target)
            if conn.execute("SELECT 1 FROM events WHERE kind='local.boot_witness' AND json_extract(payload_json,'$.target')=?", (target,)).fetchone():
                raise Conflict("A pre-restart witness already exists")
            self.core.store.event(conn, "local.boot_witness", **proof, boot=windows_boot(),
                                  owner=self.core.store.generation, captured_at=utc_now())

    def fence_evidence(self, target):
        original = self._quarantine_target(target)
        event_id, saved = self._witness(target)
        current = windows_boot()
        captured = timestamp(saved["captured_at"])
        now = datetime.now(UTC)
        elapsed = (now - captured).total_seconds() * 1000
        if (any(saved[k] != v for k, v in original.items()) or current["host"] != saved["boot"]["host"]
                or not 0 <= current["uptime_ms"] <= elapsed
                or not original["old_owner"] <= saved["owner"] < self.core.store.generation):
            raise Conflict("A new Windows boot and a newer exclusive Core owner must be proven")
        startup = {}
        if current["boot"] == saved["boot"]["boot"]:
            startup = {"startup": startup_proof(windows_startup(), captured, current["uptime_ms"], now)}
        return {**original, "old_boot": saved["boot"]["boot"], "new_boot": current["boot"], "host": current["host"],
                "witness": event_id, "witness_sha256": digest(saved), "new_owner": self.core.store.generation,
                "historical_outcome": "unknown", "no_retry": True, **startup}

    def prepare_action(self, target, *, fence=False):
        self._idle()
        proof = self.fence_evidence(target) if fence else self.evidence(target)
        return Action.build("local.fence" if fence else "local.recover", {"target": target, "evidence": digest(proof)})

    def _check(self, request):
        fence = request.action.kind == "local.fence"
        if request.action.kind not in {"local.recover", "local.fence"} or request.operation_spec is None:
            raise Conflict("A separate approved recovery operation is required")
        historical = json.loads(request.action.parameters_json)
        admin = self.core.runs.get(request.run_id)
        spec = json.loads(request.operation_spec.json)
        if (admin.idempotency_key != self.key(historical["target"], fence=fence) or admin.executor != self.name
                or admin.attempt != 1 or (spec["run_id"], spec["action_hash"]) != (admin.id, request.action.hash)
                or self._consumed(admin).spec_digest != request.operation_spec.digest):
            raise Conflict("Recovery Run and Approval do not match the target")
        self._idle(admin.id)
        if historical["target"] in self.closed() or any(target == historical["target"] for target, _ in self.fences()):
            raise Conflict("This Local operation already has a recovery decision")
        proof = self.fence_evidence(historical["target"]) if fence else self.evidence(historical["target"])
        if digest(proof) != historical["evidence"]:
            raise Conflict("Recovery evidence changed after Approval")
        return proof

    def prepare(self, request):
        self._check(request)

    def execute(self, request):
        proof = self._check(request)
        if request.action.kind == "local.fence":
            record = {"version": 1, "decision": "old_environment_fenced", "run": request.run_id, **proof}
            return ExecutionResult((ProducedArtifact("local-fence.json", canonical(record).encode()),), storage="sqlite")
        decision = {"version": 1, "decision": "administratively_closed_unknown", "run": request.run_id, **proof}
        return ExecutionResult((ProducedArtifact("local-closure.json", canonical(decision).encode()),), storage="sqlite")

    def closed(self):
        """Accept only a sealed receipt from a successful, separately approved recovery Run."""
        closed = set()
        for row in self.core.store.all("SELECT id FROM runs WHERE executor=? AND status='succeeded'", (self.name,)):
            admin = self.core.runs.get(row["id"])
            try:
                action = Action.from_json(admin.action_json)
                params = json.loads(action.parameters_json)
                if action.kind != "local.recover" or admin.idempotency_key != self.key(params["target"]):
                    continue
                approval = self._consumed(admin)
                scope = json.loads(self.core.approvals.spec_for(approval.id).json)
                proof = self.evidence(params["target"])
                artifact = self._sealed(admin.id).get("local-closure.json")
                if (artifact and scope["action_hash"] == action.hash and scope["run_id"] == admin.id
                        and params["evidence"] == digest(proof) and json.loads(self.core.artifacts.read(artifact.id))
                        == {"version": 1, "decision": "administratively_closed_unknown", "run": admin.id, **proof}):
                    closed.add(params["target"])
            except (CoreError, KeyError, ValueError, TypeError):
                continue
        return closed

    def fences(self):
        """Executed Core receipt is the atomic state transition and epoch identifier."""
        sealed = []
        rows = self.core.store.all("""SELECT id FROM runs WHERE executor=? AND status='succeeded'
            AND idempotency_key GLOB 'local-fence:*' ORDER BY finished_at,id""", (self.name,))
        host = windows_boot() if rows else None
        for row in rows:
            admin = self.core.runs.get(row["id"])
            action = Action.from_json(admin.action_json)
            params = json.loads(action.parameters_json)
            artifact = self._sealed(admin.id).get("local-fence.json")
            if not artifact or action.kind != "local.fence":
                raise Conflict("Local epoch receipt is missing")
            receipt = json.loads(self.core.artifacts.read(artifact.id))
            proof = {k: v for k, v in receipt.items() if k not in {"run", "decision", "version"}}
            spec = json.loads(self.core.approvals.spec_for(self._consumed(admin).id).json)
            original = self._quarantine_target(params["target"])
            startup = proof.get("startup")
            if startup:
                event_id, saved = self._witness(params["target"])
                sample = self._startup_sample or windows_startup()
                live = startup_proof(sample, timestamp(saved["captured_at"]), host["uptime_ms"], datetime.now(UTC))
                self._startup_sample = sample  # A Core process cannot survive a full Windows restart.
                boot_valid = (event_id == proof["witness"] and digest(saved) == proof["witness_sha256"]
                              and timestamp(live["started_at"]) >= timestamp(startup["started_at"]))
            else:
                boot_valid = proof.get("old_boot") != host["boot"]
            if (admin.idempotency_key != self.key(params["target"], fence=True)
                    or (receipt.get("run"), receipt.get("decision"), receipt.get("version")) != (admin.id, "old_environment_fenced", 1)
                    or spec["action_hash"] != action.hash or spec["run_id"] != admin.id or digest(proof) != params["evidence"]
                    or proof.get("host") != host["host"] or not boot_valid
                    or any(proof.get(k) != v for k, v in original.items())):
                raise Conflict("Local epoch proof differs from the approved operation")
            sealed.append((params["target"], admin.id))
        return sealed

    def epoch(self):
        return digest(self.fences())

    def quarantine_pending(self):
        witnesses = self.core.store.all("SELECT json_extract(payload_json,'$.target') FROM events WHERE kind='local.boot_witness'")
        return bool(witnesses and {row[0] for row in witnesses} - (set(dict(self.fences())) | self.closed()))
