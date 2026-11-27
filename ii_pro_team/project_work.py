"""Bounded Team sessions over the existing Core journal and executors."""

import json
from datetime import UTC, datetime, timedelta

from ii_pro_core.errors import Conflict, CoreError, RecoveryRequired, ValidationError
from ii_pro_core.security import Action, canonical, digest, public_text
from ii_pro_core.storage import utc_now
from ii_pro_local import table_probe
from ii_pro_local.engineering_executor import artifact_json, change, render_diff, sha
from ii_pro_local.skills import changed_fragments, packet, verified_reference
from ii_pro_worker.app_contract import (
    FILTER_PROFILE,
    PROFILE,
    PROFILES,
    SORT_PROFILE,
    app_package,
    validate_app_result,
)

from .project_contract import (
    EXTENDED_ENGINEER_DEADLINE, FRAGMENTS, SIZE_REJECTION, SORT_ENGINEER_DEADLINE, assess, context,
)
from .protocol import ROLES, identity, validate_request

FILTER_PROJECT = "task-manager-filter"
SORT_PROJECT = "task-manager-sort"
SORT_BASELINE = "583cf629013f0599a8e02faf3ed0652176d684251b8061597e91a30544a7c39e"


def engineer_deadline(scope, phase):
    seconds = scope.get("engineer_deadline_seconds")
    if phase == "implement" or (phase == "repair" and seconds == SORT_ENGINEER_DEADLINE):
        return seconds


class ProjectWork:
    def __init__(self, team):
        self.team, self.core, self.store = team, team.core, team.store
        from .autonomous import AutonomousSession

        self.autonomy = AutonomousSession(self)

    @property
    def local(self):
        return self.team.owner.local_engine

    @property
    def worker(self):
        return self.team.workbench.worker if self.team.workbench else None

    def _events(self, job):
        rows = self.store.all("""SELECT kind,payload_json FROM events
            WHERE kind IN ('team.project_draft','team.project_started','team.project_renewed','team.project_stopped',
                           'team.project_engineer_deadline')
            AND json_extract(payload_json,'$.job')=? ORDER BY id""", (job,))
        events = {row["kind"]: json.loads(row["payload_json"]) for row in rows}
        amendment = events.get("team.project_engineer_deadline")
        if amendment:
            draft = events["team.project_draft"]
            scope = {**draft["scope"], "engineer_deadline_seconds": EXTENDED_ENGINEER_DEADLINE}
            if (amendment["original_sha256"], amendment["sha256"]) != (draft["sha256"], digest(scope)):
                raise Conflict("Engineer deadline amendment differs from the original scope")
            events["team.project_draft"] = {**draft, "scope": scope, "sha256": amendment["sha256"]}
        return events

    def prepare_extended_engineer(self, *, job, original_sha256, seconds):
        """Trusted operator preparation only. Scope consent remains pending."""
        identity(job)
        with self.team.owner.lock:
            events = self._events(job)
            draft = events.get("team.project_draft")
            amended = events.get("team.project_engineer_deadline")
            if (self.team.owner.stopping or self.team.blocked() or not draft or self._grant(events)
                    or "team.project_stopped" in events or not draft["scope"].get("restart_of")
                    or draft["scope"]["project"] != FILTER_PROJECT
                    or type(seconds) is not int or seconds != EXTENDED_ENGINEER_DEADLINE
                    or original_sha256 != (amended or {}).get("original_sha256", draft["sha256"])):
                raise Conflict("Exact inactive filter continuation required")
            previous, _ = self._carried_context(draft["scope"])
            self._context(draft["scope"], previous)
            self.team._cloud_admission()
            adapter = self.team.text_executor.adapter
            if not adapter.ready_for_deadline(seconds):
                raise Conflict("Fresh SIWC authorization for the Engineer deadline required")
            if not amended:
                if self._run(job, "implement"):
                    raise Conflict("Cannot change an already prepared Engineer Run")
                scope = {**draft["scope"], "engineer_deadline_seconds": seconds}
                self._event("team.project_engineer_deadline", job=job, original_sha256=original_sha256,
                            sha256=digest(scope))
                draft = self._events(job)["team.project_draft"]
            if not self._run(job, "implement"):
                self._prepare(draft, "implement", previous)
            else:
                self.core.approvals.request(self._run(job, "implement").id, ttl_seconds=1800)
            return self.view(job)

    def _run(self, job, phase):
        rows = self.store.all("SELECT id FROM runs WHERE idempotency_key=?", (f"project:{job}:{phase}",))
        return self.core.runs.get(rows[0][0]) if rows else None

    def run_for_slot(self, job, slot):
        if slot == "local":
            draft = self._events(job)["team.project_draft"]
            runs = self.local.agents[draft["scope"]["project"]].runs(self.local_id(job))
            return runs[0].id if runs else None
        run = self._run(job, slot)
        return run.id if run else None

    def _event(self, kind, **values):
        with self.store.transaction(admit=True) as conn:
            self.store.event(conn, kind, **values)

    def draft(self, body):
        if set(body) == {"job", "resume_from"}:
            return self._continue_filter(identity(body["job"]), identity(body["resume_from"]))
        if set(body) - {"autonomous"} != {"job", "project", "task", "acceptance", "files"}:
            raise ValidationError("Exact project draft required")
        job = identity(body["job"])
        if not isinstance(body["project"], str) or body["project"] not in self.local.projects:
            raise ValidationError("Choose a registered Python project")
        title, snap = self.local.projects[body["project"]]
        snap.confirm_current()
        files = body["files"]
        if (type(files) is not list or not 1 <= len(files) <= 4
                or not all(isinstance(name, str) and name in snap.paths + snap.create_paths for name in files)
                or len(set(files)) != len(files) or not set(snap.paths) <= set(files)):
            raise ValidationError("Only explicitly selected registered Python files may be shared")
        profile = PROFILE if body["project"] == "task-manager" and "app.py" in files else None
        if body["project"] in {FILTER_PROJECT, SORT_PROJECT}:
            if files != ["app.py"] or "app.py" not in snap.paths:
                raise ValidationError("The application project requires its exact app.py baseline")
            profile = SORT_PROFILE if body["project"] == SORT_PROJECT else FILTER_PROFILE
        if profile == SORT_PROFILE and ("autonomous" not in body or sha(dict(snap.files)["app.py"]) != SORT_BASELINE):
            raise ValidationError("The sort fixture requires the exact accepted L.4 baseline and bounded scope")
        correction = int(profile == PROFILE)
        scope = {"project": body["project"], "snapshot": snap.identity, "files": sorted(files),
                 "task": public_text(body["task"], limit=600, empty=False),
                 "acceptance": public_text(body["acceptance"], limit=800, empty=False),
                 "models": {"cloud": "gpt-6.2-sol",
                            "local": self.local.agents[body["project"]].executor.approval_configuration()["profile"]["model"]},
                 "cloud_calls": 3 + correction, "local_steps": 1, "duration_seconds": 1800,
                 "attempts_per_phase": 1, "corrections": correction,
                 "operations": ["text_plan", "text_proposal", "local_observation", "sealed_copy", "text_review"]
                 + (["fixed_application_tests"] if profile else []) + (["one_bounded_correction"] if correction else []),
                 "functional_profile": profile}
        if profile and "app.py" in snap.paths:
            scope["local_review"] = "filter" if profile in {FILTER_PROFILE, SORT_PROFILE} else "comparison"
        if profile == SORT_PROFILE:
            scope["edit_protocol"] = FRAGMENTS
            scope["acceptance_cases"] = list(PROFILES[SORT_PROFILE])
        if "autonomous" in body:
            scope = self.autonomy.scope(scope, body["autonomous"], job=job)
        context(self._context(scope, {}), "plan")
        old = self._events(job).get("team.project_draft")
        if old:
            if old["sha256"] != digest(scope):
                raise Conflict("Project session already has a different scope")
        else:
            if self.store.one("SELECT count(*) FROM events WHERE kind='team.project_draft'")[0] >= 24:
                raise Conflict("Project session quota reached")
            if self.team._task(job, "plan"):
                raise Conflict("Job identity belongs to another Team task")
            self._event("team.project_draft", job=job, scope=scope, sha256=digest(scope), title=title)
        return self.view(job)

    def _continue_filter(self, job, parent):
        if self.team.owner.stopping or self.team.blocked():
            raise RecoveryRequired("Unresolved execution blocks continuation")
        old = self._events(parent)
        draft = old.get("team.project_draft")
        if (not draft or draft["scope"]["functional_profile"] != FILTER_PROFILE
                or "restart_of" in draft["scope"] or not self._grant(old)
                or any(self._run(parent, slot) for slot in ("apply", "test", "review"))):
            raise Conflict("Only a rejected filter proposal before application may be continued")
        scope = {**draft["scope"], "resume_from": parent, "edit_protocol": FRAGMENTS,
                 "cloud_calls": 2, "local_steps": 0, "corrections": 0,
                 "operations": ["text_proposal", "sealed_copy", "fixed_application_tests", "text_review"]}
        if "resume_from" in draft["scope"]:
            if not self._closed_fragment_run(parent):
                raise Conflict("The preceding unknown proposal has no operator disposition")
            scope.update(resume_from=draft["scope"]["resume_from"], restart_of=parent)
        self._context(scope, self._carried_context(scope)[0])
        scope_hash = digest(scope)
        with self.store.transaction(admit=True) as conn:
            existing = conn.execute("""SELECT payload_json FROM events WHERE kind='team.project_draft'
                AND (json_extract(payload_json,'$.job')=? OR
                     coalesce(json_extract(payload_json,'$.scope.restart_of'),
                              json_extract(payload_json,'$.scope.resume_from'))=?)""", (job, parent)).fetchone()
            if existing:
                value = json.loads(existing[0])
                if value["job"] != job or value["sha256"] != scope_hash:
                    raise Conflict("An independent continuation already exists")
            else:
                if self.team._task(job, "plan") or conn.execute(
                        "SELECT count(*) FROM events WHERE kind='team.project_draft'").fetchone()[0] >= 24:
                    raise Conflict("Project session identity or quota unavailable")
                self.store.event(conn, "team.project_draft", job=job, scope=scope, sha256=scope_hash, title=draft["title"])
        return self.view(job)

    def _carried_context(self, scope):
        parent = scope["resume_from"]
        plan, engineer = (self._run(parent, phase) for phase in ("plan", "implement"))
        if (not plan or plan.status != "succeeded" or not engineer
                or engineer.status != "succeeded" or not engineer.execution_stopped):
            raise Conflict("Completed historical runs are required")
        planned = artifact_json(self.core, plan.id, "engineering.json")
        rejected = artifact_json(self.core, engineer.id, "engineering.json")
        if planned["state"] != "proposed" or rejected["state"] != "rejected":
            raise Conflict("A rejected proposal with a valid plan is required")
        previous = json.loads(engineer.description)["inputs"]["context"]["previous"]
        local = self.core.runs.get(previous["local"]["run"])
        agent = self.local.agents[scope["project"]]
        if (local not in agent.runs(self.local_id(parent)) or not local.execution_stopped
                or local.status not in {"succeeded", "failed", "cancelled"}
                or previous["architect"] != {"run": plan.id, **planned["plan"]}):
            raise Conflict("Historical Local and Architect provenance differs")
        if local.status == "succeeded":
            artifact_json(self.core, local.id, "tool-result.json")
        if self._context(scope, previous) != json.loads(engineer.description)["inputs"]["context"]:
            raise Conflict("Historical project snapshot or criteria differ")
        carried = {**previous, "edit_protocol": scope["edit_protocol"], "rejected_proposal": engineer.id}
        if "restart_of" in scope:
            abandoned = self._closed_fragment_run(scope["restart_of"])
            if not abandoned:
                raise Conflict("The preceding operation is not permanently closed")
            carried["abandoned_proposal"] = abandoned.id
        return carried, local

    def text_abandonment(self, run, value):
        """Read-only eligibility for the trusted Team operator action."""
        if value["version"] != 4 or value["phase"] != "implement":
            return False
        job = value["job_id"]
        events = self._events(job)
        draft = events.get("team.project_draft")
        exact = self._run(job, "implement")
        if not (draft and self._grant(events) and exact and run.id == exact.id
                and value["inputs"]["scope"] == draft["sha256"]
                and value["inputs"]["context"]["previous"].get("edit_protocol") == FRAGMENTS
                and not any(self._run(job, slot) for slot in ("apply", "test", "review"))):
            return False
        scope = draft["scope"]
        if scope.get("resume_from") and scope.get("edit_protocol") == FRAGMENTS:
            return True
        if scope["project"] != SORT_PROJECT or not scope.get("autonomous", {}).get("table_check"):
            return False
        journal = self.autonomy._journal(job)
        if self.autonomy._status(journal) != "unknown":
            return False
        derived = [proof for entry in journal if entry["kind"] == "approval.derived"
                   and (proof := json.loads(entry["payload_json"]))["slot"] == "implement"]
        if len(derived) != 1:
            return False
        proof = derived[0]
        permit = self.core.approvals.get(proof["approval_id"])
        return (proof["scope_sha256"], permit.run_id, permit.action_hash, permit.status) == (
            draft["sha256"], run.id, run.action_hash, "consumed")

    def _closed_fragment_run(self, job):
        run = self._run(job, "implement")
        if (run and run.status == "unknown" and run.effect_status == "unknown"
                and (run.id, run.action_hash) in self.team._abandoned_text_runs()
                and self.text_abandonment(run, json.loads(run.description))):
            return run
        return None

    def _context(self, scope, previous):
        snap = self.local.projects[scope["project"]][1]
        if snap.identity != scope["snapshot"]:
            raise Conflict("Registered project snapshot changed")
        snap.confirm_current()
        source = {p: text for p, text in snap.files if p in scope["files"]}
        if rejected := previous.get("size_repair"):
            evidence = artifact_json(self.core, rejected["run"], "engineering.json")
            source = {"app.py": evidence["candidate"]}
        if previous.get("source_role") == "accepted_working_copy":
            copy = artifact_json(self.core, previous["copy_run"], "working-copy.json")
            if copy["snapshot"] != snap.identity or copy["sha256"] != previous["files"]:
                raise Conflict("Reviewer requires the exact sealed candidate")
            source = {p: text for p, text in copy["files"].items() if p in scope["files"]}
        return {"snapshot": snap.identity, "source": source,
                "allowed": scope["files"], "acceptance": scope["acceptance"], "previous": previous}

    def _size_repair(self, draft, run, output):
        scope = draft["scope"]
        if (scope["project"] != SORT_PROJECT or not scope.get("autonomous", {}).get("table_check")
                or not scope["corrections"] or output.get("reason") != SIZE_REJECTION
                or not run.execution_stopped or run != self._run(draft["job"], "implement")):
            return None
        request = json.loads(run.description)
        response = artifact_json(self.core, run.id, "result.json")
        if (request["inputs"]["scope"] != draft["sha256"]
                or output != assess(request, response["text"])):
            raise Conflict("Rejected candidate evidence has changed")
        return {"run": run.id, **{key: output[key] for key in ("reason", "size", "sha256")}}

    def _review_evidence(self, scope, copy, previous):
        backup = artifact_json(self.core, copy.id, "backup.json")
        working = artifact_json(self.core, copy.id, "working-copy.json")
        excerpt = "".join(render_diff(path, backup["files"].get(path), working["files"].get(path))
                          for path in scope["files"] if backup["files"].get(path) != working["files"].get(path))
        if scope["functional_profile"] == SORT_PROFILE:
            result = previous["functional_tests"]
            previous = {**previous, "functional_tests": {**result, "cases": {
                case["id"]: case["status"] for case in result["cases"]}}}
        # Keep the full candidate. Bound only the explanatory diff, never code or scores.
        maximum = 750
        while True:
            evidence = {**previous, "source_role": "accepted_working_copy", "diff_excerpt": excerpt[:maximum],
                        "diff_excerpt_truncated": len(excerpt) > maximum}
            if len(canonical(self._context(scope, evidence)).encode()) <= 6100 or maximum == 0:
                return evidence
            maximum = max(0, maximum - 150)

    def start(self, body):
        if set(body) != {"job", "sha256"}:
            raise ValidationError("Explicit project scope consent required")
        job = identity(body["job"])
        events = self._events(job)
        draft = events.get("team.project_draft")
        if not draft or body["sha256"] != draft["sha256"] or "team.project_stopped" in events:
            raise Conflict("Project consent scope differs")
        if draft["scope"].get("autonomous") and self._grant(events):
            raise Conflict("Autonomous scope consent is single use")
        if self.team.owner.stopping or self.team.blocked():
            raise RecoveryRequired("Unresolved execution blocks project consent")
        if "team.project_started" not in events:
            if "resume_from" in draft["scope"]:
                self._carried_context(draft["scope"])
            scope = draft["scope"]
            if scope.get("autonomous"):
                if self.autonomy.configuration(scope["project"]) != scope["autonomous"]["resources"]:
                    raise Conflict("The displayed autonomous resource profile changed")
                adapter = self.team.text_executor.adapter
                seconds = engineer_deadline(scope, "implement")
                if not adapter.ready() or (seconds and not adapter.ready_for_deadline(seconds)):
                    raise Conflict("SIWC authorization is unavailable")
                if scope["autonomous"]["resources"]["worker"].get("backend", {}).get("launch_enabled") is False:
                    raise Conflict("Worker deployment approval is unavailable")
            expires = datetime.now(UTC) + timedelta(seconds=scope["duration_seconds"])
            authorization = ({"authorization_ref": self.team.text_executor.adapter.configuration().get("authorization_ref")}
                             if scope.get("autonomous") else {})
            self._event("team.project_started", job=job, sha256=draft["sha256"],
                        expires_at=expires.isoformat(timespec="microseconds"), **authorization)
        elif self._grant(events)["expires_at"] <= utc_now():
            recovery = self._run(job, "plan_recovery")
            if "team.project_renewed" in events or not recovery or recovery.status != "created":
                raise Conflict("An expired session can only be renewed once for a prepared plan recovery")
            self._context(draft["scope"], {})
            self._event("team.project_renewed", job=job, sha256=draft["sha256"], recovery_run=recovery.id,
                        expires_at=(datetime.now(UTC) + timedelta(seconds=draft["scope"]["duration_seconds"]))
                        .isoformat(timespec="microseconds"))
        return self.autonomy.advance(job) if draft["scope"].get("autonomous") else self.progress(job)

    @staticmethod
    def _grant(events):
        return events.get("team.project_renewed") or events.get("team.project_started")

    def _can_recover_plan(self, scope, result):
        return (not scope.get("autonomous") and scope["functional_profile"] == PROFILE
                and scope["cloud_calls"] >= 4 and scope["corrections"] == 1
                and result == {"state": "rejected", "reason": "invalid_project_proposal"})

    def recover_plan(self, body):
        if set(body) != {"job", "sha256", "previous_run"}:
            raise ValidationError("Exact scope and rejected plan Run required")
        job, previous = identity(body["job"]), identity(body["previous_run"])
        events = self._events(job)
        draft = events.get("team.project_draft")
        if (not draft or draft["sha256"] != body["sha256"] or not self._grant(events)
                or "team.project_stopped" in events or self.team.owner.stopping or self.team.blocked()):
            raise Conflict("Project recovery is unavailable")
        plan = self._run(job, "plan")
        if (not plan or plan.id != previous or plan.status != "succeeded"
                or not self._can_recover_plan(draft["scope"], artifact_json(self.core, previous, "engineering.json"))
                or any(self._run(job, slot) for slot in ("implement", "repair", "review"))):
            raise Conflict("Recovery requires a completed, rejected plan and an unused correction slot")
        if not self._run(job, "plan_recovery"):
            self._prepare(draft, "plan_recovery", {"rejected_plan": previous})
        recovery = self._run(job, "plan_recovery")
        if not recovery:
            raise Conflict("Architect is unavailable")
        if recovery.status == "created":
            self.core.approvals.request(recovery.id, ttl_seconds=1800)
        return self.view(job)

    def active(self, job):
        events = self._events(job)
        draft = events.get("team.project_draft")
        grant = self._grant(events)
        if (not draft or not grant or grant["sha256"] != draft["sha256"] or grant["expires_at"] <= utc_now()
                or "team.project_stopped" in events):
            raise Conflict("Project session is not active")
        return draft

    def permits(self, run_id, value):
        if value["version"] != 4:
            return False
        draft = self.active(value["job_id"])
        phase = value["phase"]
        if phase == "implement" and any(value["inputs"]["context"]["previous"].get(key)
                                         for key in ("failed_test", "size_repair")):
            phase = "repair"
        ctx = value["inputs"]["context"]
        if phase == "plan" and ctx["previous"]:
            previous = self._run(value["job_id"], "plan")
            if not previous or ctx["previous"] != {"rejected_plan": previous.id}:
                return False
            phase = "plan_recovery"
        run = self._run(value["job_id"], phase)
        if ("resume_from" in draft["scope"] and phase == "implement"
                and ctx["previous"] != self._carried_context(draft["scope"])[0]):
            return False
        if phase in {"implement", "repair"} and draft["scope"].get("autonomous", {}).get("table_check"):
            local = self.core.runs.get(self.run_for_slot(value["job_id"], "local"))
            if ctx["previous"].get("local") != self._table_handoff(local, draft["scope"]["autonomous"]["table_check"]):
                return False
        if phase == "repair" and (rejected := ctx["previous"].get("size_repair")):
            original = self._run(value["job_id"], "implement")
            if (not original or original.status != "succeeded" or self._run(value["job_id"], "apply")
                    or rejected != self._size_repair(draft, original,
                                                     artifact_json(self.core, original.id, "engineering.json"))):
                return False
        seconds = engineer_deadline(draft["scope"], phase)
        return bool(run and run.id == run_id and value["inputs"]["scope"] == draft["sha256"]
                    and value["inputs"].get("deadline_seconds") == seconds
                    and ctx == self._context(draft["scope"], ctx["previous"]))

    def local_session(self, local_job):
        rows = self.store.all("""SELECT json_extract(payload_json,'$.job') FROM events
            WHERE kind='team.project_draft' ORDER BY id""")
        return next((r[0] for r in rows if self.local_id(r[0]) == local_job), None)

    def admit_local(self, local_job, run_id):
        job = self.local_session(local_job)
        if job is None:
            return False
        draft = self.active(job)
        self._context(draft["scope"], {})
        runs = self.local.agents[draft["scope"]["project"]].runs(local_job)
        if len(runs) != 1 or runs[0].id != run_id or self.store.all(
            """SELECT 1 FROM runs WHERE id!=? AND (status='running' OR
                (status='unknown' AND executor!='team_text'))""", (run_id,)
        ):
            raise RecoveryRequired("One exact Local step is permitted for this project")
        return True

    def follow(self, run):
        if run.idempotency_key.startswith("project:"):
            job = run.idempotency_key.split(":")[1]
        elif run.executor.startswith("local_agent_"):
            job = self.local_session(json.loads(run.description)["job"])
        else:
            return
        if job:
            draft = self._events(job).get("team.project_draft")
            if draft and draft["scope"].get("autonomous"):
                self.autonomy.advance(job, finished=run)
                return
            if run.status not in {"succeeded", "failed", "cancelled"}:
                return
            try:
                self.progress(job)
            except CoreError:
                self._event("team.project_preparation_blocked", job=job, predecessor=run.id)

    def admit_copy(self, run):
        scope = json.loads(run.description)
        draft = self.active(scope["job"])
        slot = scope["slot"]
        if slot in {"rollback", "rollback_fix"} and draft["scope"].get("autonomous"):
            suffix = "_fix" if slot == "rollback_fix" else ""
            parent = self._run(scope["job"], "apply" + suffix)
            tested = self._run(scope["job"], "test" + suffix)
            if (scope["digest"] != draft["sha256"] or self._run(scope["job"], slot) != run
                    or not parent or parent.id != scope["copy"] or not tested or tested.status != "succeeded"):
                raise Conflict("Rollback requires a proven failed package and its own copy")
            result = self.test_result(tested)
            params = json.loads(run.action_json)["parameters"]
            if result["passed"] == result["total"] or params["parent"] != parent.id or scope["test"] != tested.id:
                raise Conflict("Rollback provenance differs")
            return
        if (scope["digest"] != draft["sha256"] or slot not in {"apply", "apply_fix"}
                or self._run(scope["job"], slot) != run):
            raise Conflict("Working copy is outside the active project session")
        parent = self._run(scope["job"], "implement" if slot == "apply" else "repair")
        if scope["engineer"] != parent.id:
            raise Conflict("Engineer provenance differs")

    def admit_test(self, run_id, supplied=None):
        run = self.core.runs.get(run_id)
        desc = json.loads(run.description)
        job, slot = desc["job"], desc["slot"]
        draft = self.active(job)
        profile = draft["scope"]["functional_profile"]
        if (desc["digest"] != draft["sha256"] or profile not in PROFILES
                or slot not in {"test", "test_fix"} or self._run(job, slot) != run):
            raise Conflict("Exact application test slot required")
        copy = self._run(job, "apply" if slot == "test" else "apply_fix")
        if not copy or copy.id != desc["copy"] or copy.status != "succeeded":
            raise Conflict("Confirmed application copy required")
        working = artifact_json(self.core, copy.id, "working-copy.json")
        expected = app_package(working["files"]["app.py"], profile)
        if json.loads(run.action_json)["parameters"]["arguments"]["package"] != expected or (supplied is not None and supplied != expected):
            raise Conflict("Approved application package differs")

    def _prepare(self, draft, phase, previous, *, executor="team_text", action=None, description=None):
        job, scope = draft["job"], draft["scope"]
        key = f"project:{job}:{phase}"
        if executor == "team_text":
            adapter = self.team.text_executor.adapter
            if not adapter.ready() or not getattr(adapter, "engineering_supported", False):
                return
            self.team._cloud_admission()
            config = adapter.configuration()
            wire_phase = {"repair": "implement", "plan_recovery": "plan"}.get(phase, phase)
            if wire_phase == "implement" and scope.get("edit_protocol") == FRAGMENTS:
                previous = {**previous, "edit_protocol": FRAGMENTS}
            if wire_phase == "implement" and scope.get("autonomous", {}).get("table_check") and "architect" in previous:
                plan = previous["architect"]
                # Leave the sealed observation and original source intact.
                for size in (100, 70, 40, 20):
                    previous["architect"] = {"run": plan["run"], "excerpt": True,
                                             "plan": [plan["plan"][0][:size]], "checks": [plan["checks"][0][:size]]}
                    if len(canonical(self._context(scope, previous)).encode()) <= 6100:
                        break
            seconds = engineer_deadline(scope, phase)
            value = validate_request({"version": 4, "job_id": job, "session_id": digest([job, phase])[:32],
                "role": ROLES[wire_phase], "phase": wire_phase, "project_id": scope["project"], "text": scope["task"],
                "inputs": {"model": config["model"], "effort": config["effort"], "profile_digest": digest(config),
                           "context": self._context(scope, previous), "scope": draft["sha256"],
                           **({"deadline_seconds": seconds} if seconds else {})}})
            action, description = Action.build("team.turn", value), value
        task = self.core.tasks.create("Project · " + phase, executor=executor, description=canonical(description), idempotency_key=key)
        if task.status == "draft":
            self.core.tasks.transition(task.id, "planned")
        run = self.core.runs.create(task.id, action=action, idempotency_key=key)
        self.core.approvals.request(run.id, ttl_seconds=1800)

    @staticmethod
    def local_id(job):
        return digest(["project-local", job])[:32]

    def _copy(self, draft, slot, engineer_slot):
        job, scope = draft["job"], draft["scope"]
        current = self._run(job, slot)
        engineer = self._run(job, engineer_slot)
        if current and current.status == "created":
            self.core.approvals.request(current.id, ttl_seconds=1800)
        elif not current:
            proposed = artifact_json(self.core, engineer.id, "engineering.json")
            if proposed["state"] != "proposed":
                return None
            changes = proposed["changes"]
            if engineer_slot == "repair" and (rejected := json.loads(engineer.description)["inputs"]["context"]["previous"].get("size_repair")):
                prior = self._run(job, "implement")
                evidence = artifact_json(self.core, prior.id, "engineering.json")
                if (rejected != self._size_repair(draft, prior, evidence) or len(changes) != 1
                        or changes[0]["path"] != "app.py" or changes[0]["before"] != evidence["candidate"]):
                    raise Conflict("Corrected candidate does not match sealed provenance")
                original = self._context(scope, {})["source"]["app.py"]
                changes = [change("app.py", original, changes[0]["after"])]
            self._prepare(draft, slot, {}, executor="engineering_copy",
                action=Action.build("engineering.apply", {"project": scope["project"], "snapshot": scope["snapshot"],
                                                        "parent": None, "changes": changes}),
                description={"job": job, "phase": "project_apply", "slot": slot, "digest": draft["sha256"],
                             "engineer": engineer.id})
        return current

    def _test(self, draft, copy, slot):
        run = self._run(draft["job"], slot)
        if run and run.status == "succeeded":
            return self.test_result(run)
        if run and run.status == "created":
            self.core.approvals.request(run.id, ttl_seconds=1800)
        if not run and self.worker is not None:
            working = artifact_json(self.core, copy.id, "working-copy.json")
            try:
                packet = app_package(working["files"]["app.py"], draft["scope"]["functional_profile"])
            except (ValueError, SyntaxError, KeyError) as exc:
                raise ValidationError("Applied candidate does not expose the fixed WSGI entrypoint") from exc
            self._prepare(draft, slot, {}, executor=self.worker.name,
                          action=Action.build("worker.fixed", {"fixture": "app_test", "arguments": {"package": packet}}),
                          description={"job": draft["job"], "phase": "project_test", "slot": slot,
                                       "digest": draft["sha256"], "copy": copy.id})
        return None

    def _rollback(self, draft, copy, test_slot, slot):
        current = self._run(draft["job"], slot)
        if not current:
            backup = artifact_json(self.core, copy.id, "backup.json")
            working = artifact_json(self.core, copy.id, "working-copy.json")
            scope = draft["scope"]
            changes = [change(path, working["files"].get(path), backup["files"].get(path)) for path in scope["files"]
                       if working["files"].get(path) != backup["files"].get(path)]
            self._prepare(draft, slot, {}, executor="engineering_copy",
                          action=Action.build("engineering.rollback", {"project": scope["project"], "snapshot": scope["snapshot"],
                                                                       "parent": copy.id, "changes": changes}),
                          description={"job": draft["job"], "phase": "project_apply", "slot": slot, "copy": copy.id,
                                       "test": self._run(draft["job"], test_slot).id, "digest": draft["sha256"]})
        return current

    def test_result(self, run):
        receipt = artifact_json(self.core, run.id, "result.json")
        package = json.loads(run.action_json)["parameters"]["arguments"]["package"]
        if (receipt.get("run_id") != run.id or receipt.get("stop_confirmed") is not True
                or receipt.get("process_exit_code") != 0 or receipt["result"]["status"] != "ok"):
            raise Conflict("Application stop and integrity proof required")
        return validate_app_result(json.loads(receipt["result"]["stdout"]), package)

    def _prepare_local(self, job, scope, agent, engineer=None):
        mode = scope.get("local_review")
        binding = scope.get("autonomous", {}).get("table_check")
        if binding:
            agent.prepare(job, task=table_probe.PROJECT_TASK, requirements=table_probe.PROJECT_REQUIREMENTS,
                          project_table=binding)
        elif scope.get("autonomous"):
            snap = agent.executor.snapshot
            source = dict(snap.files).get("app.py", "# app.py will be created in the granted copy")
            if scope["functional_profile"] == SORT_PROFILE:
                lines = source.splitlines()
                anchor = [i for i, line in enumerate(lines) if "method == 'GET' and path in ('/', '/tasks.json')" in line]
                if len(anchor) != 1:
                    raise ValidationError("A unique GET route is required for the bounded sort observation")
                excerpt = "\n".join(lines[anchor[0]+1:anchor[0]+5])
                review = packet("filter", excerpt, "", digest([snap.identity, "app.py", excerpt]))
                agent.prepare(job, task="How can title sorting coexist with status filtering in this SELECT? What order for IDs 4: 'Beta', 2: 'alpha', 3: 'Alpha'?",
                              requirements="Put predicted IDs in prediction. Explain one SQL or shared GET risk and a useful check. Use finish.",
                              review=review)
                return
            anchor = next((i for i, line in enumerate(source.splitlines()) if "REQUEST_METHOD" in line), 0)
            excerpt = "\n".join(source.splitlines()[anchor:anchor+5])[:350]
            review = packet("filter", excerpt, "", digest([snap.identity, "app.py", excerpt]))
            agent.prepare(job, task=scope["task"][:180],
                          requirements="Report one concrete observation about the supplied source and a useful check. Use finish.",
                          review=review)
        elif mode == "filter":
            snap = agent.executor.snapshot
            lines = dict(snap.files)["app.py"].splitlines()
            anchor = [i for i, line in enumerate(lines) if "method == 'GET' and path in ('/', '/tasks.json')" in line]
            if len(anchor) != 1:
                raise ValidationError("A unique GET route excerpt is required for the filter review")
            i = anchor[0]
            source = "\n".join(lines[i:i + 5])
            review = packet(mode, source, "", digest([snap.identity, "app.py", source]))
            agent.prepare(job, task="For two tasks, #1 open and #2 completed, which ID should GET /tasks.json?status=completed return?",
                          requirements="Put the single ID in prediction. Explain where to filter both GET responses and name a check.",
                          review=review)
        elif mode == "comparison":
            proposal = artifact_json(self.core, engineer.id, "engineering.json")
            matches = [c for c in proposal["changes"] if c["path"] == "app.py"]
            if len(matches) != 1 or not matches[0]["before"]:
                raise ValidationError("The verified candidate and its original are required")
            item = matches[0]
            before, after = changed_fragments(item["before"], item["after"])
            from ii_pro_school.evolution_probe import structural_map

            from .evolution import deployment

            review_mode = "comparison_ast" if deployment(self.store) and structural_map(before, after) else mode
            review = packet(review_mode, before, after, digest([engineer.id, item]),
                            skill=verified_reference(self.core.memory, "compare"))
            agent.prepare(job, task="Compare the original and Engineer candidate excerpts.",
                          requirements="Identify one concrete behavioral change or risk and a check that would distinguish the versions.",
                          review=review)
        else:
            agent.prepare(job, task="Inspect the Python snapshot for one concrete risk.",
                          requirements="Choose one investigation tool allowed by the schema. This session has one step, so finish is unavailable.")

    def _table_handoff(self, run, binding):
        meta = json.loads(run.description)
        if (run.status != "succeeded" or not run.execution_stopped or meta.get("project_table") != binding
                or run.idempotency_key != f"agent-run:{self.local_id(binding['parent'])}:1"):
            raise Conflict("Confirmed in-scope Local execution required")
        record = artifact_json(self.core, run.id, "tool-result.json")
        artifact, = [item for item in self.core.artifacts.list(run.id) if item.relative_path.endswith("/tool-result.json")]
        handoff = {"run": run.id, "status": run.status, "outcome": record["status"], "task": meta["task"],
                   "engineering_assessment": "not_performed",
                   "warning": "Verify the hypothesis independently.",
                   "artifact": {"id": artifact.id, "sha256": artifact.sha256}}
        if record["status"] != "observed":
            return {**handoff, "reason": record["result"].get("reason")}
        observation = record["result"]
        parameters = record["parameters"]
        table_probe.validate(parameters, dataset=binding["dataset"])
        if (record.get("step") != 1 or record["tool"] != binding["tool"]
                or observation["dataset_sha256"] != binding["dataset_sha256"]
                or observation["dataset"] != binding["dataset"]
                or observation["count"] != len(observation["ids"])
                or record["fingerprint"] != digest([record["tool"], parameters])):
            raise Conflict("Sealed Local evidence differs from the session")
        return {**handoff, "hypothesis": record["plan"], "parameters": parameters, "observation": observation}

    def _local_progress(self, job, scope, agent, engineer=None):
        local_id = self.local_id(job)
        steps = agent.runs(local_id)
        if not steps:
            self._prepare_local(local_id, scope, agent, engineer)
        elif steps[-1].status == "created":
            self.core.approvals.request(steps[-1].id, ttl_seconds=1800)
        elif steps[-1].status in {"failed", "succeeded", "cancelled"}:
            return steps[-1]
        return None

    def progress(self, job):
        draft = self.active(job)
        if self.team.owner.stopping or self.team.blocked():
            raise RecoveryRequired("Unresolved execution blocks project advancement")
        scope = draft["scope"]
        self._context(scope, {})
        agent = self.local.agents[scope["project"]]
        local_id = self.local_id(scope.get("resume_from", job))
        local_run = None
        previous = {}
        recovery = self._run(job, "plan_recovery")
        size_repair = None
        if "resume_from" in scope:
            previous, local_run = self._carried_context(scope)
            phases = ("implement",)
        else:
            phases = ("plan_recovery" if recovery else "plan", "implement")
        for phase in phases:
            if phase == "implement" and (scope.get("local_review") == "filter" or scope.get("autonomous")) and local_run is None:
                local_run = self._local_progress(job, scope, agent)
                if not local_run:
                    return self.view(job)
                binding = scope.get("autonomous", {}).get("table_check")
                if binding:
                    previous["local"] = self._table_handoff(local_run, binding)
                else:
                    outcome = agent.results(local_id).get("1", {})
                    facts = outcome.get("result", {})
                    previous["local"] = {"run": local_run.id, "status": local_run.status,
                                         "outcome": outcome.get("status"), "reason": facts.get("reason"),
                                         "prediction": facts.get("prediction"),
                                         "matches_probe": outcome.get("status") == "observed" and facts.get("prediction", "").strip() == "2"}
                    if scope.get("autonomous"):
                        previous["local"].pop("matches_probe")
                        previous["local"]["observation"] = (facts.get("model_report") or "")[:160]
                        previous["local"]["suggested_check"] = (facts.get("distinguishing_test") or "")[:100]
            run = self._run(job, phase)
            if not run:
                self._prepare(draft, phase, previous)
                return self.view(job)
            if run.status == "created":
                self.core.approvals.request(run.id, ttl_seconds=1800)
            if run.status != "succeeded":
                return self.view(job)
            output = artifact_json(self.core, run.id, "engineering.json")
            if output["state"] != "proposed":
                size_repair = self._size_repair(draft, run, output) if phase == "implement" else None
                if not size_repair:
                    return self.view(job)
            if phase in {"plan", "plan_recovery"}:
                plan = output["plan"]
                if scope["functional_profile"] == SORT_PROFILE:
                    plan = {"excerpt": True, **{key: [item[:100] for item in plan[key][:2]] for key in ("plan", "checks")}}
                previous = {"architect": {"run": run.id, **plan}}

        engineer = self._run(job, "implement")
        local_run = local_run or self._local_progress(job, scope, agent, engineer)
        if not local_run:
            return self.view(job)

        if size_repair:
            repair = self._run(job, "repair")
            if not repair:
                self._prepare(draft, "repair", {"local": previous["local"], "size_repair": size_repair})
                return self.view(job)
            if repair.status == "created":
                self.core.approvals.request(repair.id, ttl_seconds=1800)
            if repair.status != "succeeded":
                return self.view(job)
            engineer = repair
        suffix = "_fix" if size_repair else ""
        copy = self._copy(draft, "apply" + suffix, "repair" if size_repair else "implement")
        if not copy or copy.status != "succeeded":
            return self.view(job)
        functional = "not_run"
        if scope["functional_profile"]:
            functional = self._test(draft, copy, "test" + suffix)
            if not functional:
                return self.view(job)
            failed = functional["passed"] != functional["total"]
            if failed and scope.get("autonomous"):
                rollback = self._rollback(draft, copy, "test" + suffix, "rollback" + suffix)
                if size_repair or not rollback or rollback.status != "succeeded" or not scope["corrections"]:
                    return self.view(job)
            if failed and scope["corrections"] and not recovery:
                repair = self._run(job, "repair")
                if not repair:
                    code = artifact_json(self.core, copy.id, "working-copy.json")["files"]["app.py"]
                    repair_evidence = {**previous, "failed_test": functional, "candidate": code}
                    if scope.get("autonomous"):
                        repair_evidence.update(candidate=code[:320], candidate_excerpt=True,
                                               diagnosis=self._test_diagnosis(functional))
                        # The full package receipt stays sealed; failures are the repair input.
                        repair_evidence["failed_test"] = {**functional, "cases": [
                            item for item in functional["cases"] if item["status"] != "passed"]}
                    if scope["functional_profile"] == SORT_PROFILE:
                        repair_evidence["failed_test"]["cases"] = {
                            case["id"]: case["diagnostic"] for case in repair_evidence["failed_test"]["cases"]}
                        repair_evidence.pop("architect", None)
                    if scope.get("autonomous", {}).get("table_check"):
                        failed_cases = [case["id"] for case in functional["cases"] if case["status"] != "passed"]
                        repair_evidence = {"local": previous["local"],
                                           "failed_test": {"run": self._run(job, "test").id,
                                                           "cases": failed_cases}}
                    self._prepare(draft, "repair", repair_evidence)
                    return self.view(job)
                if repair.status == "created":
                    self.core.approvals.request(repair.id, ttl_seconds=1800)
                if repair.status != "succeeded":
                    return self.view(job)
                engineer = repair
                copy = self._copy(draft, "apply_fix", "repair")
                if not copy or copy.status != "succeeded":
                    return self.view(job)
                functional = self._test(draft, copy, "test_fix")
                if not functional:
                    return self.view(job)
                if scope.get("autonomous") and functional["passed"] != functional["total"]:
                    self._rollback(draft, copy, "test_fix", "rollback_fix")
                    return self.view(job)
        review = self._run(job, "review")
        if not review:
            working = artifact_json(self.core, copy.id, "working-copy.json")
            observations = agent.results(local_id).get("1", {})
            evidence = {"copy_run": copy.id, "files": working["sha256"],
                "engineer_run": engineer.id, "local_run": local_run.id, "local_status": local_run.status,
                "local_tool": observations.get("tool"), "local_outcome": observations.get("status"),
                "functional_tests": functional}
            if size_repair:
                evidence["initial_rejection"] = size_repair
            self._prepare(draft, "review", self._review_evidence(scope, copy, evidence))
        elif review.status == "created":
            self.core.approvals.request(review.id, ttl_seconds=1800)
        return self.view(job)

    @staticmethod
    def _test_diagnosis(functional):
        failures = [case for case in functional["cases"] if case["status"] != "passed"]
        if any(case["status"] == "error" for case in failures):
            return "Application error; inspect the failing route and SQLite writes."
        if any(case["id"].startswith("filter_") for case in failures):
            return "Likely GET status selection or filtering behavior; compare HTML and JSON routes."
        if any(case["id"].startswith("sort_") for case in failures):
            return "Likely GET ordering or selector behavior; compare the reported HTML and JSON cases."
        return "Likely regression in the existing lifecycle; inspect the failed scenario."

    def stop(self, job):
        identity(job)
        events = self._events(job)
        if not events.get("team.project_draft"):
            raise ValidationError("Unknown project session")
        if "team.project_stopped" not in events:
            self._event("team.project_stopped", job=job)
            project = events["team.project_draft"]["scope"]["project"]
            agent = self.local.agents.get(project)
            local_job = self.local_id(job)
            if agent and agent.runs(local_job):
                agent.stop(local_job)
            for phase in (*ROLES, "plan_recovery"):
                run = self._run(job, phase)
                if run and run.status == "running":
                    self.team.text_executor.request_stop(run.id)
        return self.view(job)

    def command(self, command, body):
        if self.local is None:
            raise Conflict("A project registry is required")
        if command == "draft":
            return self.draft(body)
        if command == "start":
            return self.start(body)
        if command == "recover":
            return self.recover_plan(body)
        if command == "control":
            if set(body) != {"job", "control"}:
                raise ValidationError("Exact session control required")
            return self.autonomy.control(identity(body["job"]), body["control"])
        if set(body) != {"job"}:
            raise ValidationError("Exact project identity required")
        job = identity(body["job"])
        if command == "progress":
            draft = self._events(job).get("team.project_draft")
            return self.autonomy.advance(job) if draft and draft["scope"].get("autonomous") else self.progress(job)
        draft = self._events(job).get("team.project_draft")
        return self.autonomy.control(job, "stop") if draft and draft["scope"].get("autonomous") else self.stop(job)

    def view(self, job):
        events = self._events(job)
        draft = events["team.project_draft"]
        grant = self._grant(events)
        scope = draft["scope"]
        phases = {}
        for phase in ("plan", "plan_recovery", "implement", "apply", "test", "rollback", "repair", "apply_fix", "test_fix", "rollback_fix", "review"):
            run = self._run(job, phase)
            if not run:
                continue
            pending = self.store.all("SELECT id FROM approvals WHERE run_id=? ORDER BY rowid DESC LIMIT 1", (run.id,))
            approval = self.core.approvals.get(pending[0][0]) if pending else None
            consent = {k: getattr(approval, k) for k in ("id", "status", "spec_digest", "expires_at")} if approval else None
            if consent and consent["status"] == "pending" and (consent["expires_at"] <= utc_now() or not self.core.approvals.matches(
                    self.store.one("SELECT * FROM runs WHERE id=?", (run.id,)), self.core.approvals.spec_for(consent["id"]))):
                consent["status"] = "stale"
            phase_view = {"id": run.id, "status": run.status, "error": run.error_code, "approval": consent}
            if run.status == "succeeded":
                if phase in {"test", "test_fix"}:
                    phase_view["result"] = self.test_result(run)
                else:
                    name = "working-copy.json" if phase in {"apply", "apply_fix", "rollback", "rollback_fix"} else "engineering.json"
                    phase_view["result"] = artifact_json(self.core, run.id, name)
                if phase in {"implement", "repair"} and "changes" in phase_view["result"]:
                    phase_view["diff"] = "".join(render_diff(i["path"], i["before"], i["after"]) for i in phase_view["result"]["changes"])
            phases[phase] = phase_view
        local_id = self.local_id(scope.get("resume_from", job))
        agent = self.local.agents.get(scope["project"])
        local = self.local.detail(scope["project"], local_id) if agent and agent.runs(local_id) else None
        stopped, blocked = "team.project_stopped" in events, self.team.blocked()
        expired = bool(grant and grant["expires_at"] <= utc_now())
        recovery = phases.get("plan_recovery")
        return {**draft, "consent": grant, "stopped": stopped,
                "expired": expired, "blocked": blocked,
                "phases": phases, "local": local,
                "filter_continuation_available": bool(grant and not blocked
                    and scope["functional_profile"] == FILTER_PROFILE and "restart_of" not in scope
                    and (self._closed_fragment_run(job) if "resume_from" in scope
                         else phases.get("implement", {}).get("result", {}).get("state") == "rejected")
                    and not ({"apply", "test", "review"} & phases.keys())
                    and not self.store.all("""SELECT 1 FROM events WHERE kind='team.project_draft'
                        AND coalesce(json_extract(payload_json,'$.scope.restart_of'),
                                     json_extract(payload_json,'$.scope.resume_from'))=? LIMIT 1""", (job,))),
                "plan_recovery_available": bool(grant and not stopped and not blocked
                    and not ({"plan_recovery", "implement", "repair", "review"} & phases.keys())
                    and self._can_recover_plan(scope, phases.get("plan", {}).get("result"))),
                "renewal_available": bool(expired and not stopped and not blocked and "team.project_renewed" not in events
                    and recovery and recovery["status"] == "created"),
                "functional_tests": next((phases[k]["result"] for k in ("test_fix", "test")
                                          if phases.get(k, {}).get("status") == "succeeded"), "not_run"),
                "functional_blocker": ("Для испытания требуется отдельно согласованный образ Worker."
                                       if scope["functional_profile"] and self.worker is None
                                       else "Функциональный профиль не зарегистрирован для этого проекта."
                                       if not scope["functional_profile"] else None),
                "learning_status": "unreviewed" if phases.get("review", {}).get("result", {}).get("state") == "reviewed" else None,
                "autonomy": self.autonomy.view(job, scope, grant) if scope.get("autonomous") else None}

    def state(self):
        projects = [{"id": key, "title": title, "files": list(snap.paths + snap.create_paths)}
                    for key, (title, snap) in self.local.projects.items()]
        rows = self.store.all("""SELECT json_extract(payload_json,'$.job') FROM events
            WHERE kind='team.project_draft' ORDER BY id DESC""")
        return {"projects": projects, "sessions": [self.view(row[0]) for row in rows]}
