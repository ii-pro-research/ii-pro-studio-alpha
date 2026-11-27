"""Studio commands over Local Agent, sharing the Training Core and owner lock."""

import json
import re
from dataclasses import asdict
from pathlib import Path
from threading import Thread

from ii_pro_core.errors import Conflict, CoreError, ValidationError
from ii_pro_core.security import no_links, public_text
from ii_pro_core.storage import utc_now
from ii_pro_local import table_probe
from ii_pro_local.agent import MAX_STEPS, LocalAgent, LocalAgentExecutor
from ii_pro_local.skills import CATALOG, skill_is_verified
from ii_pro_local.snapshot import Snapshot
from ii_pro_school import evolution_probe, skill_pilot
from ii_pro_school.adapters import _strict_json


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValidationError("Invalid Local Engineer identifier")
    return value


def load_projects(path):
    """Only a trusted startup file can introduce a repository."""
    path = Path(path).absolute()
    no_links(path)
    with path.open("rb") as file:
        raw = file.read(16385)
    try:
        entries = _strict_json(raw)
    except (ValueError, RecursionError) as exc:
        raise ValidationError("Invalid project registry") from exc
    if len(raw) > 16384 or not isinstance(entries, list) or not 1 <= len(entries) <= 8:
        raise ValidationError("Invalid project registry size")
    projects = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.keys() - {"create_files"} != {
            "id",
            "title",
            "root",
            "base",
            "revision",
            "files",
        }:
            raise ValidationError("Invalid project fields")
        key = entry["id"]
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,23}", key)
            or key in projects
        ):
            raise ValidationError("Invalid or duplicate project ID")
        title = public_text(entry["title"], limit=80, empty=False)
        if not isinstance(entry["root"], str) or not Path(entry["root"]).is_absolute():
            raise ValidationError("Project registry requires an absolute root")
        snapshot = Snapshot.register(
            entry["root"],
            base=entry["base"],
            revision=entry["revision"],
            paths=entry["files"],
            create_paths=entry.get("create_files", ()),
        )
        projects[key] = (title, snapshot)
    return projects


def build_executors(projects, profile):
    executors = {}
    for key, (_, snapshot) in projects.items():
        executor = LocalAgentExecutor(profile, snapshot)
        executor.name = "local_agent_" + key
        executors[key] = executor
    return executors


class LocalEngine:
    def __init__(self, owner, core, projects, executors, *, live):
        self.owner = owner
        self.core = core
        self.projects = projects
        self.live = live
        self.table_probe_project = None
        self.table_probe_job = table_probe.JOB
        self.recovery = core.runs.executors.get("local_recovery")
        self.agents = {
            key: LocalAgent(core, executor) for key, executor in executors.items()
        }

    def _agent(self, key):
        if not isinstance(key, str) or key not in self.agents:
            raise ValidationError("Unknown registered project")
        return self.agents[key]

    def _job(self, key, job):
        agent = self._agent(key)
        runs = agent.runs(identity(job))
        if not runs:
            raise ValidationError("Unknown job in project")
        return agent, runs, json.loads(runs[0].description)

    def state(self):
        team = self.owner.team
        gpt = bool(team and team.text_state()["auth_ready"])
        jobs = []
        for row in self.core.store.all(
            """SELECT executor, idempotency_key, description FROM tasks
               WHERE executor GLOB 'local_agent_*' AND idempotency_key GLOB 'agent:*:1'
               ORDER BY rowid DESC LIMIT 30"""
        ):
            key = row["executor"].removeprefix("local_agent_")
            job = row["idempotency_key"].split(":")[1]
            if key in self.agents:
                # Include created tasks even if an interruption preceded Run creation.
                jobs.append(
                    {
                        "project": key,
                        "job": job,
                        "task": json.loads(row["description"])["task"],
                    }
                )
        return {
            "projects": [
                {"id": key, "title": title} for key, (title, _) in self.projects.items()
            ],
            "jobs": jobs,
            "live": self.live,
            "max_steps": MAX_STEPS,
            "roles": {
                "local": "enabled" if self.live and self.projects else "disabled",
                "architect": "text_ready" if gpt else "not_ready",
                "engineer": "text_ready"
                if team and team.engineering_state()["auth_ready"]
                else "not_ready",
            },
            "memory": [
                self._memory(r[0])
                for r in self.core.store.all(
                    "SELECT id FROM engineering_memory ORDER BY rowid DESC LIMIT 24"
                )
            ],
            "unreviewed_materials": self.core.memory.unreviewed_artifacts(self.core.artifacts),
            **self.skill_state(),
        }

    def skill_state(self):
        anchor = self.agents.get(skill_pilot.ANCHOR)
        pilot = skill_pilot.outcomes(anchor) if anchor else []
        skills = []
        for skill in CATALOG:
            history = self.core.memory.history(skill.key)
            status = history[-1]["status"] if history else "unreviewed"
            if status == "verified" and not skill_is_verified(self.core.memory, skill):
                status = "stale"
            skills.append({**asdict(skill), "sha256": skill.sha256, "status": status, "trust_history": history})
        apps = []
        for row in self.core.store.all("""SELECT id,status,description FROM runs
                 WHERE executor GLOB 'local_agent_*' AND json_type(description,'$.review.skill')='object'
                 ORDER BY rowid DESC LIMIT 24"""):
            review = json.loads(row["description"])["review"]
            info = review["skill"]
            apps.append({"run": row["id"], "status": row["status"], "mode": review["mode"],
                         "skill": {key: info[key] for key in ("id", "version", "sha256")}})
        return {"skills": skills, "skill_uses": apps, "skill_pilot": pilot}

    def prepare_skill_pilot(self, body):
        if body != {} or self.owner.stopping or (self.owner.team and self.owner.team.blocked()):
            raise Conflict("Skill pilot preparation is blocked")
        agent = self._agent(skill_pilot.ANCHOR)
        slot = skill_pilot.next_slot(agent)
        if not slot:
            raise Conflict("All skill pilot slots are complete")
        runs = agent.runs(slot["job"])
        if runs:
            self.core.approvals.request(runs[0].id)
        else:
            agent.prepare(slot["job"], task=slot["task"], requirements=skill_pilot.REQUIREMENTS, review=slot["review"])
        return self.detail(skill_pilot.ANCHOR, slot["job"])

    def _memory(self, key):
        entry = self.core.memory.get(key)
        return {"id": key, "body": entry["body"], "history": entry["history"]}

    def detail(self, key, job):
        with self.core.store.lock:
            return self._detail(key, job)

    def _detail(self, key, job):
        agent, runs, original = self._job(key, job)
        view = agent.status(job)
        view.update(
            task=original["task"], requirements=original["requirements"], project=key
        )
        for item, run in zip(view["runs"], runs, strict=True):
            item["error_code"] = run.error_code
            item["memory"] = json.loads(run.action_json)["parameters"].get("memory", [])
            item["proposal"] = None
            if run.status == "unknown" and self.recovery:
                item["recovery"] = self._recovery_view(run)
            item["approvals"] = [
                dict(row)
                for row in self.core.store.all(
                    "SELECT id, status, expires_at FROM approvals WHERE run_id=? ORDER BY rowid DESC LIMIT 12",
                    (run.id,),
                )
            ]
            for artifact in item["artifacts"]:
                if artifact["name"] == "response.txt":
                    item["proposal"] = self.core.artifacts.read(artifact["id"]).decode()
                elif artifact["name"] == "receipt.json":
                    receipt = json.loads(self.core.artifacts.read(artifact["id"]))
                    stats = receipt.get("statistics", {})
                    item["statistics"] = stats
                    item["elapsed_seconds"] = receipt.get("elapsed_seconds")
                    item["tokens_per_second"] = (stats["eval_count"] * 1_000_000_000 / stats["eval_duration"]
                        if stats.get("eval_duration") and "eval_count" in stats else None)
                elif artifact["name"] == "handoff.json":
                    packet = json.loads(self.core.artifacts.read(artifact["id"]))
                    consumed = self.core.store.one(
                        "SELECT id FROM approvals WHERE run_id=? AND status='consumed'",
                        (run.id,),
                    )
                    config = self.core.approvals.spec_for(
                        consumed[0]
                    ).executor_configuration
                    view["handoff"] = {
                        "version": 1,
                        "recipient": "gpt_engineer",
                        "status": "waiting_for_engineer",
                        "source_job": job,
                        "project": key,
                        "snapshot": config["snapshot"],
                        "revision": packet["revision"],
                        "files": config["files"],
                        "tools": config["tools"],
                        "task": packet["task"],
                        "criteria": [packet["requirements"]],
                        "evidence": packet["observations"],
                        "functional_tests": packet["functional_tests"],
                        "unresolved": packet["unresolved"],
                    }
        latest = runs[-1]
        permit = None
        rows = self.core.store.all(
            "SELECT id FROM approvals WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
            (latest.id,),
        )
        if rows:
            approval = self.core.approvals.get(rows[0][0])
            spec = self.core.approvals.spec_for(approval.id)
            status = str(approval.status)
            pending = status in {"pending", "approved"}
            if pending and approval.expires_at <= utc_now():
                status = "expired"
            elif (
                pending
                and latest.status == "created"
                and not self.core.approvals.matches(
                    self.core.store.one("SELECT * FROM runs WHERE id=?", (latest.id,)),
                    spec,
                )
            ):
                status = "stale"
            operation = json.loads(spec.json)
            permit = {
                "id": approval.id,
                "digest": spec.digest,
                "status": status,
                "expires_at": approval.expires_at,
                "run": latest.id,
                "operation": operation,
            }
        view["approval"] = permit
        view["scope_changed"] = original["snapshot"] != agent.executor.snapshot.identity
        view["dispatch_pending"] = (
            latest.id in self.owner.threads and latest.status == "created"
        )
        view["can_approve"] = bool(
            self.live
            and (job not in table_probe.JOBS or (self.table_probe_project, self.table_probe_job) == (key, job))
            and not (self.recovery and self.recovery.quarantine_pending())
            and view["status"] == "awaiting_approval"
            and not self.owner.threads
            and not self.owner.stopping
            and not view["scope_changed"]
            and permit
            and permit["status"] == "pending"
        )
        return view

    def _recovery_view(self, original):
        receipt = {"target": original.id, "original_status": str(original.status), "no_retry": True,
                   "confirmed_stop": bool(original.execution_stopped), "error_code": original.error_code,
                   "evidence": None, "approval": None, "eligible": False,
                   "closed": original.id in self.recovery.closed()}
        try:
            fenced = dict(self.recovery.fences()).get(original.id)
            if fenced:
                receipt["fence"] = {"status": "fenced", "epoch_run": fenced}
            elif not receipt["closed"]:
                self.recovery._quarantine_target(original.id)
                self.recovery._idle()
                if not self.owner.threads and not self.owner.stopping:
                    receipt["fence"] = self._fence_view(original.id)
        except (CoreError, KeyError, ValueError, TypeError):
            pass
        try:
            receipt["evidence"] = self.recovery.evidence(original.id)
            self.recovery._idle()
            receipt["eligible"] = not (receipt["closed"] or self.owner.threads or self.owner.stopping)
        except (CoreError, KeyError, ValueError, TypeError):
            receipt["reason"] = ("terminal_proof_unavailable" if receipt["evidence"] is None else "active_execution")
        if not receipt["eligible"]:
            return receipt
        rows = self.core.store.all("""SELECT a.id FROM approvals a JOIN runs r ON r.id=a.run_id
            WHERE r.idempotency_key=? AND r.status='created' AND a.status='pending' AND a.expires_at>?""",
            (self.recovery.key(original.id), utc_now()))
        if rows:
            approval = self.core.approvals.get(rows[0][0])
            receipt["approval"] = {"id": approval.id, "run": approval.run_id, "digest": approval.spec_digest,
                                   "expires_at": approval.expires_at}
        return receipt

    def _fence_view(self, target):
        try:
            self.recovery._witness(target)
        except CoreError:
            return {"status": "needs_witness"}
        try:
            proof = self.recovery.fence_evidence(target)
        except CoreError:
            return {"status": "restart_unproven"}
        view = {"status": "ready", "evidence": {key: proof[key] for key in ("old_boot", "new_boot", "old_owner",
                                                                           "new_owner", "witness_sha256")}}
        if "startup" in proof:
            view["evidence"]["startup"] = proof["startup"]
        rows = self.core.store.all("""SELECT a.id FROM approvals a JOIN runs r ON r.id=a.run_id
            WHERE r.idempotency_key=? AND r.status='created' AND a.status='pending' AND a.expires_at>?""",
            (self.recovery.key(target, fence=True), utc_now()))
        if not rows:
            return view
        permit = self.core.approvals.get(rows[0][0])
        return {**view, "approval": {"id": permit.id, "run": permit.run_id, "digest": permit.spec_digest,
                                      "expires_at": permit.expires_at}}

    def recover(self, body, *, decide):
        fields = {"project", "job", "target"}
        mode = body.get("mode", "closure")
        if (not self.recovery or body.keys() - {"mode"} != (fields | {"run", "approval", "digest"} if decide else fields)
                or mode not in ("closure", "fence", "witness") or decide and mode == "witness"):
            raise ValidationError("Exact Local recovery action required")
        project, job = body["project"], identity(body["job"])
        _, runs, _ = self._job(project, job)
        target = identity(body["target"])
        if (not any(run.id == target for run in runs) or target in self.recovery.closed()
                or target in dict(self.recovery.fences())):
            raise Conflict("The historical Local Run does not match or is already closed")
        if self.owner.stopping or self.owner.threads:
            raise Conflict("Studio has an active operation")
        if mode == "witness":
            self.recovery.witness(target)
            return self.detail(project, job)
        fence = mode == "fence"
        action = self.recovery.prepare_action(target, fence=fence)
        key = self.recovery.key(target, fence=fence)
        if decide:
            run = self.core.runs.get(identity(body["run"]))
            permit = self.core.approvals.get(identity(body["approval"]))
            if (run.idempotency_key != key or run.action_hash != action.hash or run.status != "created"
                    or permit.run_id != run.id or permit.spec_digest != body["digest"] or permit.status != "pending"):
                raise Conflict("Exact unused Local recovery Approval required")
            self.core.approvals.decide(permit.id, allow=True)
            self.core.runs.execute(run.id, approval_id=permit.id)
        else:
            task = self.core.tasks.create("Local · смена поколения" if fence else "Local · административное закрытие", executor=self.recovery.name,
                                          description=action.parameters_json, idempotency_key=key)
            if task.status == "draft":
                self.core.tasks.transition(task.id, "planned")
            run = self.core.runs.create(task.id, action=action, idempotency_key=key)
            self.core.approvals.request(run.id)
        return self.detail(project, job)

    def prepare(self, body):
        if body.keys() != {"project", "job", "task", "requirements"}:
            raise ValidationError("Expected project, job, task and requirements")
        key, job = body["project"], identity(body["job"])
        if job in table_probe.JOBS and ((self.table_probe_project, self.table_probe_job) != (key, job)
                                       or not self.owner.team or self.owner.team.blocked()):
            raise Conflict("The table probe is disabled or blocked")
        if evolution_probe.reserved(job):
            raise Conflict("Reserved evolution slot requires the experimental entry point")
        if self.owner.team and self.owner.team.projects.local_session(job):
            raise Conflict("Project sessions prepare their own Local step")
        agent = self._agent(key)
        if self.owner.stopping:
            raise Conflict("Studio is stopping")
        runs = agent.runs(job)
        if runs and runs[-1].id in self.owner.threads:
            raise Conflict("Step already dispatched")
        if runs and runs[-1].status == "created":
            view = self.detail(key, job)
            if (body["task"], body["requirements"]) != (
                view["task"],
                view["requirements"],
            ):
                raise Conflict("Job identity belongs to another task")
            if view["status"] == "stopped":
                raise Conflict("Job was stopped")
            if view["scope_changed"]:
                raise Conflict("Registered project snapshot changed")
            agent._guard(job, runs[:-1])
            self.core.approvals.request(runs[-1].id)
        else:
            agent.prepare(job, task=body["task"], requirements=body["requirements"])
        return self.detail(key, job)

    def decision(self, body):
        if (
            body.keys() != {"project", "job", "run", "approval", "digest", "allow"}
            or type(body["allow"]) is not bool
        ):
            raise ValidationError("Exact Local Engineer decision required")
        key, job = body["project"], identity(body["job"])
        agent, runs, original = self._job(key, job)
        run = runs[-1]
        approval = self.core.approvals.get(identity(body["approval"]))
        if (
            run.id != identity(body["run"])
            or approval.run_id != run.id
            or approval.spec_digest != body["digest"]
            or run.status != "created"
            or run.id in self.owner.threads
            or agent.stopped(job)
            or original["snapshot"] != agent.executor.snapshot.identity
        ):
            raise Conflict("Operation does not match a pending step")
        agent._guard(job, runs[:-1])
        if body["allow"] and job in table_probe.JOBS and (
            (self.table_probe_project, self.table_probe_job) != (key, job) or not self.owner.team or self.owner.team.blocked()
        ):
            raise Conflict("The table probe is disabled or blocked")
        review = original.get("review")
        if body["allow"] and review and review["mode"] == "pilot":
            if self.owner.team and self.owner.team.blocked():
                raise Conflict("Skill pilot dispatch is blocked")
            slot = skill_pilot.next_slot(agent)
            if key != skill_pilot.ANCHOR or not slot or (slot["job"], slot["review"]) != (job, review):
                raise Conflict("The exact next skill pilot slot is required")
        if body["allow"] and review and review["mode"] in {"evolution_a", "evolution_b"}:
            if not self.owner.team or self.owner.team.blocked():
                raise Conflict("Evolution pilot dispatch is blocked")
            slot = evolution_probe.next_slot(agent)
            if key != evolution_probe.ANCHOR or not slot or (slot["job"], slot["review"]) != (job, review):
                raise Conflict("The exact next evolution slot is required")
        project_permit = False
        if body["allow"] and self.owner.team and self.owner.team.projects.local_session(job):
            if self.owner.team.blocked():
                raise Conflict("Project Local dispatch is blocked")
            project_permit = self.owner.team.projects.admit_local(job, run.id)
        if body["allow"] and (
            not (self.live or project_permit) or self.owner.threads or self.owner.stopping
            or self.recovery and self.recovery.quarantine_pending()
        ):
            raise Conflict("Local generation is disabled or Studio is busy")
        self.core.approvals.decide(approval.id, allow=body["allow"])
        if body["allow"]:
            thread = Thread(
                target=self._execute,
                args=(run.id, approval.id),
                name="local-engine-" + run.id,
            )
            self.owner.threads[run.id] = thread
            try:
                thread.start()
            except BaseException:
                self.owner.threads.pop(run.id)
                self.core.approvals.revoke(
                    approval.id, reason="local_engine_dispatch_not_started"
                )
                raise
        return self.detail(key, job)

    def _execute(self, run_id, approval_id):
        try:
            self.core.runs.execute(run_id, approval_id=approval_id)
        except Exception:
            current = self.core.runs.get(run_id)
            permit = self.core.approvals.get(approval_id)
            if current.status == "created" and permit.status == "approved":
                self.core.approvals.revoke(
                    approval_id, reason="local_engine_dispatch_unconfirmed"
                )
            with self.core.store.transaction() as conn:
                self.core.store.event(
                    conn, "local_engine.dispatch_unconfirmed", run_id=run_id
                )
        finally:
            with self.owner.lock:
                self.owner.threads.pop(run_id, None)
                if self.owner.team:
                    self.owner.team.projects.follow(self.core.runs.get(run_id))

    def stop(self, body):
        if body.keys() != {"project", "job"}:
            raise ValidationError("Exact job required")
        agent, _, _ = self._job(body["project"], body["job"])
        agent.stop(body["job"])
        return self.detail(body["project"], body["job"])
