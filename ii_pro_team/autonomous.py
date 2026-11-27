"""Bounded session grants for the existing project workflow and approval manager."""

import json
from datetime import UTC, datetime, timedelta
from threading import Thread

from ii_pro_core.errors import Conflict, CoreError, ValidationError
from ii_pro_core.security import Action, digest
from ii_pro_core.storage import utc_now
from ii_pro_local import table_probe

from .project_contract import SORT_ENGINEER_DEADLINE, SORT_ENGINEER_IDLE
from .project_work import SORT_PROJECT, engineer_deadline
from .protocol import validate_request

SLOTS = {
    "plan": ("cloud", "team.turn"), "local": ("local", "local.review"),
    "implement": ("cloud", "team.turn"), "apply": ("copies", "engineering.apply"),
    "test": ("worker", "worker.fixed"), "repair": ("cloud", "team.turn"),
    "apply_fix": ("copies", "engineering.apply"), "test_fix": ("worker", "worker.fixed"),
    "rollback": ("rollbacks", "engineering.rollback"), "rollback_fix": ("rollbacks", "engineering.rollback"),
    "review": ("cloud", "team.turn"),
}
CONTROLS = ("pause", "resume", "revoke", "stop")


class AutonomousSession:
    def __init__(self, project):
        self.project = project
        self.team, self.core, self.store = project.team, project.core, project.store
        self.core.approvals.session_authorizer = self.authorize
        self.core.approvals.session_required = self.required

    def configuration(self, key):
        agent = self.project.local.agents[key]
        local = agent.executor.approval_configuration()
        text = self.team.text_executor.adapter.configuration()
        worker = self.project.worker.approval_configuration()
        # The actual OAuth identity and model profile are additionally bound by each OperationSpec.
        technical = {k: v for k, v in text.items() if k != "authorization_ref"}
        deadlines = {"cloud": text.get("deadline_seconds", 60) + text.get("bridge_watchdog_grace_seconds", 0) + 8,
                     "local": int(local["profile"]["deadline_seconds"]) + 8,
                     "worker": 20, "copies": 5, "rollbacks": 5}
        return {"balanced": local["workflow"], "local_profile": local["profile"], "local_profile_sha256": digest(local),
                "cloud_profile": technical, "cloud_profile_sha256": digest(technical),
                "worker": worker, "worker_sha256": digest(worker), "dispatch_reserve_seconds": deadlines}

    def scope(self, scope, limits, *, job=None):
        if (type(limits) is not dict or set(limits) - {"local_tool", "engineer_deadline_seconds"} != {"duration_seconds", "corrections"}
                or type(limits["duration_seconds"]) is not int or not 60 <= limits["duration_seconds"] <= 1800
                or type(limits["corrections"]) is not int or limits["corrections"] not in {0, 1}
                or scope["project"] not in {"task-manager", "task-manager-filter", SORT_PROJECT}
                or scope["files"] != ["app.py"] or not scope["functional_profile"]
                or not self.project.worker or not self.team.text_executor):
            raise ValidationError("Autonomous v0.1 requires the fixed application profile and a bounded budget")
        instrumented = "local_tool" in limits
        if instrumented and (limits["local_tool"] != "table_check" or scope["project"] != SORT_PROJECT or not job):
            raise ValidationError("The table probe is only available in the bounded sort session")
        extended = "engineer_deadline_seconds" in limits
        if extended and (not instrumented or type(limits["engineer_deadline_seconds"]) is not int
                         or limits["engineer_deadline_seconds"] != SORT_ENGINEER_DEADLINE):
            raise ValidationError("The experimental deadline requires the instrumented sort session")
        caps = limits["corrections"]
        config = self.configuration(scope["project"])
        limits_by_kind = {"cloud": 3 + caps, "local": 1, "copies": 1 + caps,
                          "worker": 1 + caps, "rollbacks": 1 + caps}
        return {**scope, **limits, "cloud_calls": 3 + caps,
                "operations": ["text_plan", "local_observation", "text_proposal", "sealed_copy",
                               "fixed_application_tests", "isolated_rollback", "text_review"]
                              + (["one_bounded_correction"] if caps else []),
                "autonomous": {"version": 2 if instrumented else 1, "budget": limits_by_kind,
                    "tools": ["table_check"] if instrumented else ["finish"],
                    **({"table_check": table_probe.project_binding(job),
                        "local_epoch": self.project.local.agents[scope["project"]].executor._epoch()} if instrumented else {}),
                    **({"engineer_text_idle_seconds": SORT_ENGINEER_IDLE} if extended else {}),
                    "resources": config, "stops": ["unknown", "failed_dispatch", "budget", "deadline", "scope",
                                                    "paused", "revoked", "operator_stop"],
                    "allowed_slots": [slot for slot in SLOTS if caps or slot not in
                                      {"repair", "apply_fix", "test_fix", "rollback_fix"}]}}

    def _journal(self, job, *, conn=None):
        query = """SELECT kind,created_at,payload_json FROM events
            WHERE kind IN ('team.project_auto_control','team.project_auto_end','approval.derived')
            AND json_extract(payload_json,'$.job')=? ORDER BY id"""
        rows = conn.execute(query, (job,)) if conn is not None else self.store.all(query, (job,))
        return [dict(row) for row in rows]

    @staticmethod
    def _status(journal):
        for entry in reversed(journal):
            if entry["kind"] == "team.project_auto_end":
                return json.loads(entry["payload_json"])["reason"]
            if entry["kind"] == "team.project_auto_control":
                mode = json.loads(entry["payload_json"])["control"]
                return {"pause": "paused", "resume": "active", "revoke": "revoked", "stop": "stopped"}[mode]
        return "active"

    def required(self, run_id):
        rows = self.store.all("SELECT idempotency_key,description FROM runs WHERE id=?", (run_id,))
        if not rows:
            return False
        key = rows[0]["idempotency_key"]
        if key.startswith("project:"):
            job = key.split(":")[1]
        elif key.startswith("agent-run:"):
            job = self.project.local_session(json.loads(rows[0]["description"])["job"])
        else:
            return False
        draft = self.project._events(job).get("team.project_draft") if job else None
        return bool(draft and draft["scope"].get("autonomous"))

    def _used(self, journal):
        count = dict.fromkeys({bucket for bucket, _ in SLOTS.values()}, 0)
        for entry in journal:
            if entry["kind"] == "approval.derived":
                bucket, _ = SLOTS[json.loads(entry["payload_json"])["slot"]]
                count[bucket] += 1
        return count

    def authorize(self, conn, run, proof, *, reserve):
        job, slot = proof["job"], proof["slot"]
        draft = self.project.active(job)
        scope = draft["scope"]
        auto = scope.get("autonomous")
        if not auto or slot not in auto["allowed_slots"] or proof["scope_sha256"] != draft["sha256"]:
            raise Conflict("Operation outside the autonomous grant")
        if self.configuration(scope["project"]) != auto["resources"]:
            raise Conflict("A frozen executor resource profile changed")
        events = self.project._events(job)
        grant = self.project._grant(events)
        if self.team.text_executor.adapter.configuration().get("authorization_ref") != grant.get("authorization_ref"):
            raise Conflict("The consented cloud authorization changed")
        expires = grant["expires_at"]
        journal = self._journal(job, conn=conn)
        bucket, kind = SLOTS[slot]
        if self._status(journal) != "active":
            raise Conflict("Session dispatch is stopped")
        reserve_seconds = auto["resources"]["dispatch_reserve_seconds"][bucket]
        if (seconds := engineer_deadline(scope, slot)) is not None:
            profile = auto["resources"]["cloud_profile"]
            reserve_seconds += seconds - profile.get("deadline_seconds", 60)
        if datetime.now(UTC) + timedelta(seconds=reserve_seconds) >= datetime.fromisoformat(expires):
            raise Conflict("Insufficient time for this operation")
        refs = [json.loads(x["payload_json"]) for x in journal if x["kind"] == "approval.derived"]
        related = [entry for entry in refs if entry["slot"] == slot]
        used = self._used(journal)[bucket]
        if (reserve and (related or used >= auto["budget"][bucket])
                or not reserve and (related != [proof] or used > auto["budget"][bucket])):
            raise Conflict("Session slot or budget is exhausted")
        action = Action.from_json(run["action_json"])
        if action.kind != kind or action.hash != run["action_hash"] or run["attempt"] != 1:
            raise Conflict("Operation differs from its granted slot")
        if slot == "local":
            local_job = self.project.local_id(job)
            meta = json.loads(run["description"])
            agent = self.project.local.agents[scope["project"]]
            if (run["idempotency_key"] != f"agent-run:{local_job}:1" or meta["snapshot"] != scope["snapshot"]
                    or agent.stopped(local_job)
                    or run["executor"] != agent.executor.name):
                raise Conflict("Local step outside the bounded observation")
            binding = auto.get("table_check")
            if binding:
                params = json.loads(action.parameters_json)
                expected_prompt = {"task": table_probe.PROJECT_TASK, "requirements": table_probe.PROJECT_REQUIREMENTS,
                                   "step": 1, "files": [], "table_probe": True, "source": table_probe.source()}
                if (params.get("memory") or json.loads(params["prompt"]) != expected_prompt
                        or auto["tools"] != ["table_check"] or auto["budget"]["local"] != 1
                        or binding != table_probe.project_binding(job)
                        or meta.get("project_table") != binding or meta.get("review")
                        or meta.get("table_probe") != binding["dataset_sha256"] or meta.get("step") != 1
                        or meta["format"] != agent.response_schema(1, {}, table=binding["dataset"])
                        or auto.get("local_epoch") != agent.executor._epoch()):
                    raise Conflict("Instrumented Local differs from the frozen grant")
            elif auto["tools"] != ["finish"] or meta.get("project_table") or meta.get("review", {}).get("mode") != "filter":
                raise Conflict("Local step outside the approved finish contract")
        else:
            if run["idempotency_key"] != f"project:{job}:{slot}":
                raise Conflict("Run is outside the granted slot")
            if kind == "team.turn":
                value = validate_request(json.loads(action.parameters_json))
                role_phase = {"repair": "implement"}.get(slot, slot)
                if value["phase"] != role_phase or not self.project.permits(run["id"], value):
                    raise Conflict("Cloud context or role differs")
            elif kind == "worker.fixed":
                self.project.admit_test(run["id"])
            else:
                applied = json.loads(action.parameters_json)
                if ({x["path"] for x in applied["changes"]} - set(scope["files"])
                        or applied["snapshot"] != scope["snapshot"] or applied["project"] != scope["project"]):
                    raise Conflict("Copy outside the granted snapshot or paths")
                self.project.admit_copy(self.core.runs.get(run["id"]))
        return expires

    def finish(self, job, reason):
        if self._status(self._journal(job)) not in {"paused", "revoked", "stopped", "completed", "failed",
                                                    "expired", "unknown", "constraint"}:
            self.project._event("team.project_auto_end", job=job, reason=reason)

    def control(self, job, control):
        events = self.project._events(job)
        draft = events.get("team.project_draft")
        if (control not in CONTROLS or not draft or not draft["scope"].get("autonomous")
                or not self.project._grant(events)):
            raise ValidationError("Unknown autonomous control")
        status = self._status(self._journal(job))
        if ((control == "resume" and status != "paused") or (control == "pause" and status != "active")
                or (control in {"stop", "revoke"} and status not in {"active", "paused"})):
            raise Conflict("Autonomous session control is unavailable")
        self.project._event("team.project_auto_control", job=job, control=control)
        return self.advance(job) if control == "resume" else self.project.view(job)

    def advance(self, job, *, finished=None):
        status = self._status(self._journal(job))
        if finished and finished.status == "unknown":
            if status in {"active", "paused"}:
                self.project._event("team.project_auto_end", job=job, reason="unknown")
        elif status != "active":
            return self.project.view(job)
        elif finished and finished.status != "succeeded":
            self.finish(job, "failed")
        elif not self.team.owner.stopping and not self.team.blocked():
            try:
                self.project.active(job)
            except Conflict:
                self.finish(job, "expired")
            else:
                try:
                    view = self.project.progress(job)
                    self._dispatch(view)
                except (CoreError, KeyError, ValueError, TypeError):
                    self.finish(job, "constraint")
        return self.project.view(job)

    def _dispatch(self, view):
        job = view["job"]
        phases = view["phases"]
        pending = [(slot, v["id"], v["approval"]) for slot, v in phases.items() if v["status"] == "created"]
        local = view["local"]
        if local and local["status"] == "awaiting_approval":
            pending.append(("local", local["approval"]["run"], local["approval"]))
        if not pending:
            review = phases.get("review", {}).get("result", {})
            terminal = "completed" if review.get("state") == "reviewed" else "failed"
            if any(phases.get(k, {}).get("status") == "succeeded" for k in ("rollback", "rollback_fix")):
                terminal = "budget" if not review else terminal
            self.finish(job, terminal)
            return
        if len(pending) != 1:
            raise Conflict("Multiple autonomous slots pending")
        slot, run_id, permit = pending[0]
        approval = self.core.approvals.derive_for_session(permit["id"], job=job, slot=slot, scope_sha256=view["sha256"])
        thread = Thread(target=self.team._execute, args=(run_id, approval.id), name="team-session-" + run_id)
        self.team.owner.threads[run_id] = thread
        try:
            thread.start()
        except BaseException:
            self.team.owner.threads.pop(run_id)
            self.core.approvals.revoke(approval.id, reason="session_dispatch_not_started")
            raise

    def view(self, job, scope, grant):
        history = self._journal(job)
        active_runs = [row["id"] for row in self.store.all("SELECT id FROM runs WHERE status='running'")]
        current = next((json.loads(x["payload_json"])["slot"] for x in reversed(history)
                        if x["kind"] == "approval.derived" and any(
                            self.project.run_for_slot(job, json.loads(x["payload_json"])["slot"]) == r for r in active_runs)), None)
        config = scope["autonomous"]
        used = self._used(history)
        status = self._status(history) if grant else "draft"
        if grant and status in {"active", "paused"} and grant["expires_at"] <= utc_now():
            status = "expired"
        return {"status": status, "current": current, "remaining": {k: v - used[k] for k, v in config["budget"].items()},
                "journal": [{**e, "payload_json": json.loads(e["payload_json"])} for e in history]}
