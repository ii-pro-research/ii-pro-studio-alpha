"""Team application facade over existing Task/Run/Approval/Event/Artifact APIs.

No new execution database, workflow status table, approval engine or queue.
"""

import hashlib
import json
import threading

from ii_pro_core.errors import Conflict, RecoveryRequired, ValidationError
from ii_pro_core.security import Action, canonical, public_text, digest
from ii_pro_core.storage import utc_now
from . import engineering
from .protocol import ROLES, identity, validate_request
from .workspace import CONTRACT


class Team:
    def __init__(self, owner, executors, laboratory, text_executor=None):
        self.owner, self.executors, self.laboratory = owner, executors, laboratory
        self.text_executor = text_executor
        self.workbench = None
        self.core = owner.school.core
        self.store = self.core.store
        for executor in executors.values():
            executor.core = self.core
        if text_executor:
            text_executor.core = self.core
        from .project_work import ProjectWork
        from .evolution import Evolution

        self.projects = ProjectWork(self)
        self.evolution = Evolution(self)
        if text_executor:
            text_executor.project_permit = self.projects.permits

    def _abandoned_text_runs(self):
        abandoned = set()
        for row in self.store.all(
            "SELECT payload_json FROM events WHERE kind='team.unknown_abandoned'"
        ):
            value = json.loads(row["payload_json"])
            if (
                set(value)
                == {
                    "abandoned_run_id",
                    "action_hash",
                    "accept_unknown_cost",
                    "no_retry",
                }
                and value["accept_unknown_cost"] is True
                and value["no_retry"] is True
            ):
                abandoned.add((value["abandoned_run_id"], value["action_hash"]))
        return abandoned

    def cloud_wait(self):
        row = self.store.all("""
            SELECT id, run_id FROM events
            WHERE kind='team.agent_event'
              AND json_extract(payload_json, '$.agent_event.data.sdk_error_code')
                = 'subscription_sharing_usage_limit_exceeded'
            ORDER BY id DESC LIMIT 1
        """)
        if row:
            acknowledged = self.store.all(
                """
                SELECT payload_json FROM events
                WHERE kind='team.cloud_quota_acknowledged'
                  AND json_extract(payload_json, '$.observed_event') = ?
                ORDER BY id DESC LIMIT 1
                """,
                (row[0]["id"],),
            )
            if not acknowledged:
                return {"status": "waiting_for_cloud_quota", "run_id": row[0]["run_id"]}
            evidence = json.loads(acknowledged[0]["payload_json"])
            if evidence.get("source") == "user_reported_chatgpt_reset":
                return {
                    "status": "ready_to_prepare",
                    "run_id": None,
                    "source": evidence["source"],
                    "provider_verified": False,
                }
        return {"status": "ready_to_prepare", "run_id": None}

    def acknowledge_cloud_quota(self, run_id, *, source="operator_confirmed"):
        """Record an operator acknowledgement, including unverified user reports."""
        if source not in ("operator_confirmed", "user_reported_chatgpt_reset"):
            raise ValidationError("Unknown quota acknowledgement source")
        with self.owner.lock, self.store.transaction(admit=True) as conn:
            if self.cloud_wait()["run_id"] != identity(run_id):
                raise Conflict("Quota acknowledgement must match the recorded refusal")
            observed = conn.execute(
                """
                SELECT MAX(id) FROM events WHERE run_id=? AND kind='team.agent_event'
                  AND json_extract(payload_json, '$.agent_event.data.sdk_error_code')
                    = 'subscription_sharing_usage_limit_exceeded'
            """,
                (run_id,),
            ).fetchone()[0]
            self.store.event(
                conn,
                "team.cloud_quota_acknowledged",
                observed_event=observed,
                source=source,
                provider_verified=False,
            )

    def _cloud_admission(self):
        if self.cloud_wait()["run_id"]:
            raise RecoveryRequired("Waiting for the ChatGPT quota to recover")

    def blocked(self):
        abandoned = self._abandoned_text_runs()
        recovery = self.core.runs.executors.get("local_recovery")
        closed_local = recovery.closed() if recovery else set()
        if recovery:
            try:
                closed_local.update(target for target, _ in recovery.fences())
            except (CoreError, KeyError, ValueError, TypeError):
                return True
        return bool(
            self.owner.threads
            or any(
                row["id"] not in closed_local and not (
                    row["status"] == "unknown"
                    and row["executor"] == "team_text"
                    and row["effect_status"] == "unknown"
                    and (row["id"], row["action_hash"]) in abandoned
                )
                for row in self.store.all(
                    "SELECT id,action_hash,executor,status,effect_status FROM runs WHERE status IN ('running','unknown') OR effect_status IN ('unknown','partial') OR (status='failed' AND effect_status='applied')"
                )
            )
            or self.store.all(
                "SELECT 1 FROM worker_jobs WHERE stop_confirmed=0 LIMIT 1"
            )
        )

    def abandon_unknown(
        self, run_id, *, action_hash, accept_unknown_cost=False, no_retry=False
    ):
        """Explicit trusted-operator decision, never an HTTP/model command.

        Only independent text tasks may proceed. This does not reconcile the old
        Run, prove remote termination, permit retry, or change its history.
        """
        if accept_unknown_cost is not True or no_retry is not True:
            raise ValidationError("Explicit acceptance and permanent no-retry required")
        with self.owner.lock, self.store.transaction(admit=True) as conn:
            run = self.core.runs.get(identity(run_id), conn=conn)
            value = validate_request(json.loads(run.description))
            if (
                run.status != "unknown"
                or run.effect_status != "unknown"
                or run.executor != "team_text"
                or not (value["version"] in {2, 3} or self.projects.text_abandonment(run, value))
                or run.action_hash != action_hash
                or run.id in self.owner.threads
            ):
                raise RecoveryRequired(
                    "Only the exact inactive unknown text Run may be abandoned"
                )
            payload = {
                "abandoned_run_id": run.id,
                "action_hash": action_hash,
                "accept_unknown_cost": True,
                "no_retry": True,
            }
            if not any(
                json.loads(r["payload_json"]) == payload
                for r in conn.execute(
                    "SELECT payload_json FROM events WHERE kind='team.unknown_abandoned'"
                )
            ):
                # No run_id association: preserve all original Run events exactly.
                self.store.event(conn, "team.unknown_abandoned", **payload)
            return payload

    def _requests(self):
        return [
            (r["id"], validate_request(json.loads(r["description"])))
            for r in self.store.all(
                "SELECT id,description FROM tasks WHERE executor IN ('team_architect','team_engineer','team_text') ORDER BY rowid"
            )
        ]

    def _task(self, job, phase):
        return next(
            (
                key
                for key, request in self._requests()
                if request["job_id"] == job and request["phase"] == phase
            ),
            None,
        )

    def _run(self, job, phase):
        task = self._task(job, phase)
        if not task:
            raise ValidationError("Team phase is not prepared")
        runs = self.core.runs.list(task)
        if not runs:
            raise ValidationError("Team phase has no Run yet")
        return runs[-1]

    def _outputs(self, run):
        if run.status != "succeeded":
            raise RecoveryRequired("A confirmed phase result is required")
        self.core.artifacts.validate_run(run.id, require_any=True)
        return {
            a.relative_path.rsplit("/", 1)[-1]: json.loads(
                self.core.artifacts.read(a.id)
            )
            for a in self.core.artifacts.list(run.id)
        }

    def _prepare(self, job, phase, text, inputs):
        role = ROLES[phase]
        session = hashlib.sha256(
            ("team-session-v1:" + job + ":" + role).encode()
        ).hexdigest()[:32]
        value = validate_request(
            {
                "version": 1,
                "job_id": job,
                "session_id": session,
                "role": role,
                "phase": phase,
                "project_id": "studio-lab",
                "text": text,
                "inputs": inputs,
            }
        )
        task = self.core.tasks.create(
            "GPT Team · " + phase,
            description=canonical(value),
            executor="team_" + role,
            idempotency_key="team-task:" + job + ":" + phase,
        )
        if phase == "plan":
            # Durable intent/quota precedes allocation of workspace files.
            self.laboratory.seed(job)
        if task.status == "draft":
            self.core.tasks.transition(task.id, "planned")
        run = self.core.runs.create(
            task.id,
            action=Action.build("team.turn", value),
            idempotency_key="team-run:" + job + ":" + phase,
        )
        if run.status == "created":
            self.core.approvals.request(run.id, ttl_seconds=1800)
        return self.job(job)

    def prepare(self, body):
        if body.get("project_id") == engineering.PROJECT:
            return self.prepare_engineering(body)
        if body.get("project_id") == "gpt-text":
            return self.prepare_text(body)
        if (
            set(body) != {"request_id", "project_id", "text"}
            or body["project_id"] != "studio-lab"
        ):
            raise ValidationError(
                "Only a registered project and a public task are accepted"
            )
        job = identity(body["request_id"])
        text = public_text(body["text"], limit=1800, empty=False)
        with self.owner.lock:
            if self.owner.stopping or self.blocked():
                raise RecoveryRequired(
                    "Active or unknown execution blocks Team preparation"
                )
            jobs = {r["job_id"] for _, r in self._requests()}
            if any(
                r["job_id"] == job and r["project_id"] != "studio-lab"
                for _, r in self._requests()
            ):
                raise Conflict("Request identity already belongs to another mode")
            if job not in jobs and len(jobs) >= 32:
                raise Conflict("Team laboratory quota reached")
            return self._prepare(
                job, "plan", text, {"contract": CONTRACT, "fixture": "slug-words-v1"}
            )

    def advance(self, body):
        if set(body) != {"job_id", "phase"} or body["phase"] not in {
            "implement",
            "local",
            "close_local",
            "review",
            "review_followup",
            "apply",
            "test",
            "rollback",
        }:
            raise ValidationError("Explicit Team phase required")
        job, phase = identity(body["job_id"]), body["phase"]
        with self.owner.lock:
            if self.owner.stopping or self.blocked():
                raise RecoveryRequired("Unresolved execution blocks the next phase")
            plan_run = self._run(job, "plan")
            if phase in {"apply", "test", "rollback"}:
                if not self.workbench or plan_run.executor != "team_text" or json.loads(plan_run.description)["version"] != 3:
                    raise Conflict("Engineering work requires a sealed Team job")
                self.workbench.prepare(job, phase)
                return self.job(job)
            if (
                plan_run.executor == "team_text"
                and json.loads(plan_run.description)["version"] == 3
            ):
                if phase == "implement":
                    return self._prepare_from_plan(job, plan_run)
                if phase == "local":
                    return self._prepare_local(job)
                if phase == "close_local":
                    return self._close_local(job)
                if phase == "review_followup":
                    previous = self._run(job, "review")
                    if not self._review_abandoned(
                        previous, self._abandoned_text_runs()
                    ):
                        raise RecoveryRequired(
                            "The original unknown review requires explicit closure"
                        )
                    return self._prepare_team_review(job, previous=previous.id)
                return self._prepare_team_review(job)
            if phase in {"local", "close_local", "review_followup"}:
                raise Conflict("Local needs a sealed engineering task")
            if plan_run.executor == "team_text":
                raise Conflict(
                    "Text generation does not authorize Engineer or computer work"
                )
            plan = self._outputs(plan_run)["result.json"]
            text = json.loads(plan_run.description)["text"]
            inputs = {
                "plan": plan,
                "source_runs": {"plan": plan_run.id},
                "evidence": [
                    {
                        "run_id": plan_run.id,
                        "artifact_id": a.id,
                        "sha256": a.sha256,
                        "size": a.size,
                    }
                    for a in self.core.artifacts.list(plan_run.id)
                ],
            }
            if phase == "review":
                engineer_run = self._run(job, "implement")
                inputs.update(
                    engineer=self._outputs(engineer_run)["result.json"],
                    provider_thread_id=plan["provider_thread_id"],
                )
                inputs["source_runs"]["implement"] = engineer_run.id
                inputs["evidence"] += [
                    {
                        "run_id": engineer_run.id,
                        "artifact_id": a.id,
                        "sha256": a.sha256,
                        "size": a.size,
                    }
                    for a in self.core.artifacts.list(engineer_run.id)
                ]
            return self._prepare(job, phase, text, inputs)

    def _scoped(self, body, fields):
        if set(body) != set(fields):
            raise ValidationError("Exact Team command fields required")
        run = self.core.runs.get(identity(body["run_id"]))
        if run.executor not in {"team_architect", "team_engineer", "team_text"}:
            if run.executor in {"engineering_copy", "engineering_worker"}:
                description = json.loads(run.description)
                if description.get("phase") in {"project_apply", "project_test"}:
                    return run, {"job_id": description["job"], description["phase"]: True}
            scope = self.workbench.scope(run) if self.workbench else None
            if scope is not None:
                return run, {"job_id": scope["job"], "engineering_operation": scope}
            raise ValidationError("Not a Team Run")
        request = validate_request(json.loads(run.description))
        return run, request

    def decide(self, body):
        with self.owner.lock:
            run, request = self._scoped(
                body, ("run_id", "approval_id", "spec_digest", "allow")
            )
            if type(body["allow"]) is not bool:
                raise ValidationError("Explicit decision required")
            permit = self.core.approvals.get(identity(body["approval_id"]))
            if (permit.run_id, permit.spec_digest) != (run.id, body["spec_digest"]):
                raise Conflict("Team Approval identity differs")
            if run.status == "unknown":
                raise RecoveryRequired("Unknown cannot be replayed")
            if run.id in self.owner.threads or run.status != "created":
                return self.job(request["job_id"])
            if self.owner.stopping or self.blocked():
                raise RecoveryRequired("Team dispatch is blocked")
            if body["allow"] and "engineering_operation" in request:
                self.workbench.admit(request["engineering_operation"], run)
            if body["allow"] and "project_apply" in request:
                self.projects.admit_copy(run)
            if body["allow"] and "project_test" in request:
                self.projects.admit_test(run.id)
            if body["allow"] and run.executor == "team_text":
                self._cloud_admission()
            if (
                body["allow"]
                and run.executor == "team_text"
                and not self.text_executor.permitted(run.id, request)
            ):
                raise Conflict(
                    "An exact operator Text Run permit is required before approving"
                )
            self.core.approvals.decide(permit.id, allow=body["allow"])
            if body["allow"]:
                thread = threading.Thread(
                    target=self._execute,
                    args=(run.id, permit.id),
                    name="team-" + run.id,
                )
                self.owner.threads[run.id] = thread
                try:
                    thread.start()
                except BaseException:
                    self.owner.threads.pop(run.id, None)
                    self.core.approvals.revoke(
                        permit.id, reason="team_dispatch_not_started"
                    )
                    raise
            return self.job(request["job_id"])

    def revoke(self, body):
        with self.owner.lock:
            run, request = self._scoped(body, ("run_id", "approval_id", "spec_digest"))
            permit = self.core.approvals.get(identity(body["approval_id"]))
            if (permit.run_id, permit.spec_digest) != (run.id, body["spec_digest"]):
                raise Conflict("Team Approval identity differs")
            self.core.approvals.revoke(permit.id, reason="team_operator_request")
            return self.job(request["job_id"])

    def stop(self, body):
        with self.owner.lock:
            run, request = self._scoped(body, ("run_id",))
            if any(key in request for key in ("engineering_operation", "project_apply", "project_test")):
                raise ValidationError("Engineering operations have a fixed lifecycle")
            executor = (
                self.text_executor
                if run.executor == "team_text"
                else self.executors[request["role"]]
            )
            accepted = executor.request_stop(run.id)
            with self.store.transaction() as conn:
                self.store.event(
                    conn,
                    "team.stop_requested",
                    run_id=run.id,
                    task_id=run.task_id,
                    signal_accepted=accepted,
                )
            # A signal is neither a stopped process nor a resolved effect.
            return self.job(request["job_id"])

    def _execute(self, run_id, approval_id):
        try:
            self.core.runs.execute(run_id, approval_id=approval_id)
        except Exception:
            with self.store.transaction() as conn:
                self.store.event(conn, "team.dispatch_unconfirmed", run_id=run_id)
        finally:
            with self.owner.lock:
                self.owner.threads.pop(run_id, None)
                self.projects.follow(self.core.runs.get(run_id))

    @staticmethod
    def _review_abandoned(run, abandoned):
        return (
            run.status == "unknown"
            and run.effect_status == "unknown"
            and (run.id, run.action_hash) in abandoned
        )

    def job(self, job):
        if self.projects._events(job).get("team.project_draft"):
            return self.projects.view(job)
        phases = []
        abandoned = self._abandoned_text_runs()
        for task_id, value in self._requests():
            if value["job_id"] != job:
                continue
            runs = self.core.runs.list(task_id)
            if not runs:
                continue
            run = runs[-1]
            approvals = self.store.all(
                "SELECT id FROM approvals WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
                (run.id,),
            )
            permit = self.core.approvals.get(approvals[0][0]) if approvals else None
            approval_view = None
            if permit:
                approval_view = {
                    k: getattr(permit, k)
                    for k in ("id", "status", "spec_digest", "expires_at")
                }
                if value["version"] == 3 and permit.status in {"pending", "approved"}:
                    if permit.expires_at <= utc_now():
                        approval_view["status"] = "expired"
                    elif run.status == "created" and not self.core.approvals.matches(
                        self.store.one("SELECT * FROM runs WHERE id=?", (run.id,)),
                        self.core.approvals.spec_for(permit.id),
                    ):
                        approval_view["status"] = "invalidated"
            output = self._outputs(run) if run.status == "succeeded" else {}
            events = [
                json.loads(r[0])
                for r in self.store.all(
                    "SELECT payload_json FROM events WHERE run_id=? AND kind='team.agent_event' ORDER BY id",
                    (run.id,),
                )
            ]
            phases.append(
                {
                    "phase": value["phase"],
                    "role": value["role"],
                    "session_id": value["session_id"],
                    "run_id": run.id,
                    "attempt": run.attempt,
                    **(
                        {
                            "previous_review_run": value["inputs"]["review"].get(
                                "previous_review_run"
                            ),
                            "abandoned_unknown": self._review_abandoned(run, abandoned),
                        }
                        if value["version"] == 3 and value["phase"] == "review"
                        else {}
                    ),
                    "status": str(run.status),
                    "effect_status": str(run.effect_status),
                    "execution_stopped": bool(run.execution_stopped),
                    "error_code": run.error_code,
                    "active": run.id in self.owner.threads,
                    "approval": approval_view,
                    "outputs": output,
                    "approved_profile": self.core.approvals.spec_for(
                        permit.id
                    ).executor_configuration.get("adapter")
                    if permit and value["version"] in {2, 3}
                    else None,
                    **(
                        {
                            "text_operation": {
                                "prompt": engineering.wire(value)[0],
                                "instructions": engineering.INSTRUCTIONS[
                                    engineering.wire(value)[1]
                                ],
                                "context": value["inputs"]["context"],
                                "operator_permit": (
                                    self.text_executor.adapter.mock
                                    or self.text_executor.authorized_run == run.id
                                ),
                            },
                        }
                        if value["version"] == 3
                        else {}
                    ),
                    "events": events,
                    "artifacts": [
                        {"id": a.id, "sha256": a.sha256, "size": a.size}
                        for a in self.core.artifacts.list(run.id)
                    ],
                }
            )
        first = next(
            (value for _, value in self._requests() if value["job_id"] == job), None
        )
        if first is None:
            raise ValidationError("Team job not found")
        text_profile = (
            phases[0]["approved_profile"]
            if first["version"] in {2, 3} and phases
            else None
        )
        local = None
        local_available = False
        if first["version"] == 3 and first["phase"] == "plan":
            for phase in phases:
                if phase["phase"] == "implement" and phase["status"] == "succeeded":
                    try:
                        local = self._local_evidence(job)
                        local_available = True
                    except ValidationError:
                        pass  # Invalid proposal or unavailable fixture.
                    except Conflict:
                        pass  # Scope changed; history remains viewable.
                    break
        reviews = [phase for phase in phases if phase["phase"] == "review"]
        review = reviews[-1] if reviews else None
        report = None
        if first["version"] == 3 and review and review["status"] == "succeeded":
            assessment = review["outputs"]["engineering.json"]
            scope = review["outputs"]["request.json"]["inputs"]
            report = {
                "status": assessment["state"],
                "phases": {p["phase"]: p["run_id"] for p in phases},
                "local_job": local["job"]
                if local
                else review["text_operation"]["context"]["source_job"],
                "plan": scope["context"]["architect_plan"],
                "engineer_candidate": scope["review"],
                "local_observations": scope["context"]["observations"],
                "local_outcome": "incomplete"
                if "local_attempt" in scope["review"]
                else "model_reported",
                "assessment": assessment,
                "functional_tests": assessment["functional_tests"],
                "knowledge": "unreviewed",
            }
        working_copy = self.workbench.view(job) if self.workbench and first["version"] == 3 and local_available else None
        if report is not None and working_copy and working_copy["test"]:
            report["execution_evidence"] = working_copy["test"]
        return {
            "job_id": job,
            "project_id": first["project_id"],
            "team_local": local,
            "team_local_available": local_available,
            "team_report": report,
            "working_copy": working_copy,
            "review_followup_available": len(reviews) == 1
            and review["abandoned_unknown"]
            if review and review.get("abandoned_unknown")
            else False,
            "text": first["text"],
            "backend": (text_profile or {}).get("backend", "unverified_text")
            if first["version"] in {2, 3}
            else "mock",
            "text_mode": first["version"] in {2, 3},
            "engineering": first["version"] == 3,
            "text_profile": text_profile,
            "prompt_sha256": hashlib.sha256(first["text"].encode()).hexdigest(),
            "can_execute": first["version"] not in {2, 3}
            or bool(
                phases
                and (
                    self.text_executor.adapter.mock
                    or self.text_executor.authorized_run == phases[0]["run_id"]
                )
            ),
            "real_inference": any(
                p["outputs"].get("result.json", {}).get("real_inference") is True
                for p in phases
            ),
            "contract": "Только текст; без файлов, инструментов и автоматического повтора"
            if first["version"] in {2, 3}
            else CONTRACT,
            "phases": phases,
            "workspace": {
                "architect": "architect",
                "engineer": "engineer",
                "qwen": "qwen",
                "host_project_access": False,
            },
        }

    def state(self):
        with self.owner.lock:
            jobs = list(dict.fromkeys(v["job_id"] for _, v in self._requests() if v["version"] != 4))
            return {
                "version": 1,
                "backend": "mock",
                "real_connected": False,
                "auth_status": self.owner.diagnostics.state()["auth"]
                if self.owner.diagnostics
                else "not_probed_by_app_server",
                "real_launch_enabled": False,
                "roles": [
                    {
                        "id": role,
                        "label": "GPT " + role.title(),
                        "backend": "mock",
                        "connected": False,
                    }
                    for role in ("architect", "engineer")
                ],
                "projects": [
                    {
                        "id": "studio-lab",
                        "title": "Лаборатория Studio · фиксированный Mock-пример",
                        "user_project": False,
                    }
                ],
                "blocked": self.blocked(),
                "abandoned_unknowns": sorted(
                    key for key, _ in self._abandoned_text_runs()
                ),
                "text": self.text_state(),
                "engineering": self.engineering_state(),
                "cloud": self.cloud_wait(),
                "jobs": [self.job(key) for key in reversed(jobs)],
                **({"project_work": self.projects.state()} if getattr(self.owner, "local_engine", None) else {}),
            }

    def text_state(self):
        from .text import DIAGNOSTIC_PROMPT

        adapter = self.text_executor.adapter if self.text_executor else None
        return {
            "model": adapter.model if adapter else None,
            "auth_ready": bool(adapter and adapter.ready()),
            "auth_error": bool(adapter and adapter.auth_error),
            "authorization_ref": adapter.configuration()["authorization_ref"]
            if adapter
            else None,
            "backend": adapter.configuration()["backend"] if adapter else "disabled",
            "billing": "chatgpt_plan",
            "prompt": DIAGNOSTIC_PROMPT,
            "computer_access": False,
            "generation_automatically_enabled": False,
        }

    def prepare_text(self, body):
        if (
            set(body) != {"request_id", "project_id", "text"}
            or body["project_id"] != "gpt-text"
        ):
            raise ValidationError("Exact Text preparation required")
        from .text import FIRST_PROMPT, DIAGNOSTIC_PROMPT

        job = identity(body["request_id"])
        if body["text"] not in (FIRST_PROMPT, DIAGNOSTIC_PROMPT):
            raise ValidationError("First Text experiment has a fixed public prompt")
        with self.owner.lock:
            if self.owner.stopping or self.blocked():
                raise RecoveryRequired("Unresolved execution blocks Text preparation")
            self._cloud_admission()
            executor = self.text_executor
            if not executor or not executor.adapter.ready():
                raise Conflict(
                    "Sign in with ChatGPT and verify the account catalog first"
                )
            if any(
                v["job_id"] == job and v["project_id"] != "gpt-text"
                for _, v in self._requests()
            ):
                raise Conflict("Request identity already belongs to another mode")
            if len(self._requests()) >= 96 and not self._task(job, "plan"):
                raise Conflict("Team task quota reached")
            config = executor.adapter.configuration()
            value = validate_request(
                {
                    "version": 2,
                    "job_id": job,
                    "session_id": hashlib.sha256(
                        ("gpt-text-session:" + job).encode()
                    ).hexdigest()[:32],
                    "role": "architect",
                    "phase": "plan",
                    "project_id": "gpt-text",
                    "text": body["text"],
                    "inputs": {
                        "model": config["model"],
                        "effort": config["effort"],
                        "profile_digest": digest(config),
                    },
                }
            )
            task = self.core.tasks.create(
                "GPT Architect · Text",
                description=canonical(value),
                executor="team_text",
                idempotency_key="team-text-task:" + job,
            )
            if task.status == "draft":
                self.core.tasks.transition(task.id, "planned")
            run = self.core.runs.create(
                task.id,
                action=Action.build("team.turn", value),
                idempotency_key="team-text-run:" + job,
            )
            if run.status == "created":
                self.core.approvals.request(run.id, ttl_seconds=1800)
            return self.job(job)

    def engineering_state(self):
        local = self.owner.local_engine
        registered = local.projects.get(engineering.PROJECT) if local else None
        adapter = self.text_executor.adapter if self.text_executor else None
        supported = bool(adapter and getattr(adapter, "engineering_supported", False))
        return {
            "supported": supported,
            "fixture_available": bool(
                registered and engineering.shareable(registered[1])
            ),
            "auth_ready": bool(supported and adapter.ready()),
            "model": adapter.model if supported else None,
            "task": engineering.TASK,
            "criteria": engineering.CRITERIA,
        }

    def prepare_engineering(self, body):
        if set(body) != {"project_id", "request_id", "phase", "source_job"} or (
            body["project_id"] != engineering.PROJECT
            or body["phase"] not in ("plan", "implement")
        ):
            raise ValidationError("Exact public fixture and text role required")
        job, phase = identity(body["request_id"]), body["phase"]
        with self.owner.lock:
            state = self.engineering_state()
            if not state["fixture_available"]:
                raise ValidationError(
                    "The registered public pricing snapshot is required"
                )
            local = self.owner.local_engine
            _, snapshot = local.projects[engineering.PROJECT]
            source = body["source_job"]
            if source is None:
                context = engineering.context(snapshot)
            else:
                source = identity(source)
                view = local.detail(engineering.PROJECT, source)
                packet = view.get("handoff")
                if not packet or (
                    packet["snapshot"] != snapshot.identity
                    or packet["revision"] != snapshot.revision
                    or packet["task"] != engineering.TASK
                    or packet["criteria"] != [engineering.CRITERIA]
                ):
                    raise ValidationError("A matching sealed Local handoff is required")
                agent, runs, _ = local._job(engineering.PROJECT, source)
                if any(
                    json.loads(run.description).get("snapshot") != snapshot.identity
                    or run.status in {"created", "running", "unknown"}
                    for run in runs
                ):
                    raise Conflict(
                        "Local handoff has unresolved execution or changed scope"
                    )
                context = engineering.context(
                    snapshot, job=source, runs=runs, results=agent.results(source)
                )
            return self._prepare_engineering(job, phase, context)

    def _prepare_from_plan(self, job, run):
        context = json.loads(run.description)["inputs"]["context"]
        outputs = self._outputs(run)
        result = outputs["engineering.json"]
        if result["state"] != "proposed" or (
            outputs["result.json"]["real_inference"] == self.text_executor.adapter.mock
        ):
            raise Conflict(
                "A confirmed Architect plan from the same backend is required"
            )
        artifact = next(
            item
            for item in self.core.artifacts.list(run.id)
            if item.relative_path.endswith("/engineering.json")
        )
        return self._prepare_engineering(
            job,
            "implement",
            {
                **context,
                "architect_plan": {
                    "run": run.id,
                    "sha256": artifact.sha256,
                    "plan": result["plan"],
                },
            },
        )

    @staticmethod
    def _local_job_id(job):
        return hashlib.sha256(("team-local-v1:" + job).encode()).hexdigest()[:32]

    def _local_evidence(self, job, *, prepare=False):
        run = self._run(job, "implement")
        request = validate_request(json.loads(run.description))
        if (
            request["version"] != 3
            or not request["inputs"]["context"]["architect_plan"]
        ):
            raise Conflict(
                "A confirmed Engineer proposal from the team plan is required"
            )
        result = self._outputs(run)["engineering.json"]
        task, requirements = engineering.local_subtask(result)
        engine = self.owner.local_engine
        project = engineering.PROJECT
        if not engine or not self.engineering_state()["fixture_available"]:
            raise Conflict("The registered public Local project is missing")
        agent = engine.agents[project]
        if agent.executor.snapshot.identity != request["inputs"]["context"]["snapshot"]:
            raise Conflict("Local project snapshot differs from the Engineer snapshot")
        key = self._local_job_id(job)
        runs = agent.runs(key)
        if prepare and (
            not runs
            or engine.detail(project, key)["status"]
            in {"awaiting_approval", "ready_for_next_step"}
        ):
            engine.prepare(
                {
                    "job": key,
                    "project": project,
                    "task": task,
                    "requirements": requirements,
                }
            )
        if not prepare and not runs:
            return None
        view = engine.detail(project, key)
        if (view["task"], view["requirements"]) != (task, requirements) or view[
            "scope_changed"
        ]:
            raise Conflict("Local work differs from the Engineer handoff")
        has_report = any(
            row["tool"] == "finish" and row["status"] == "observed"
            for row in view["results"].values()
        )
        reason = None if has_report else self._incomplete_local_reason(view["status"])
        view["attempt_outcome"] = (
            "model_reported"
            if has_report
            else "incomplete"
            if reason
            else "in_progress"
        )
        view["completion_reason"] = reason
        view["can_close_attempt"] = view["status"] in {
            "ready_for_next_step",
            "awaiting_approval",
        } and any(run.status in {"succeeded", "failed"} for run in agent.runs(key))
        return view

    @staticmethod
    def _incomplete_local_reason(status):
        return {
            "stopped": "operator_closed",
            "budget_exhausted": "step_budget_exhausted",
            "waiting_for_engineer": "candidate_budget_exhausted",
        }.get(status)

    def _close_local(self, job):
        local = self._local_evidence(job)
        if local is None or (
            not local["can_close_attempt"] and not local["completion_reason"]
        ):
            raise Conflict("Only an inactive, attempted Local task may be closed")
        engine = self.owner.local_engine
        agent, runs, _ = engine._job(engineering.PROJECT, local["job"])
        if local["can_close_attempt"]:
            agent.stop(local["job"])
        for run in runs:
            if run.status == "created":
                self.core.runs.cancel(run.id)
        return self.job(job)

    def _prepare_local(self, job):
        existing = self._local_evidence(job)
        if existing and existing["status"] not in {
            "awaiting_approval",
            "ready_for_next_step",
        }:
            raise Conflict("Local attempt is closed")

        self._local_evidence(job, prepare=True)
        return self.job(job)

    def _prepare_team_review(self, job, *, previous=None):
        engineer_run = self._run(job, "implement")
        local = self._local_evidence(job)
        if not local or local["attempt_outcome"] == "in_progress":
            raise Conflict("Close the Local attempt or obtain its report before review")
        project = engineering.PROJECT
        agent, runs, _ = self.owner.local_engine._job(project, local["job"])
        if any(
            run.status in {"created", "running", "unknown"} or not run.execution_stopped
            for run in runs
        ):
            raise RecoveryRequired("Unsettled Local execution blocks review")
        if not any(run.status in {"succeeded", "failed"} for run in runs):
            raise Conflict("No attempted Local step to review")
        for run in runs:
            if run.status == "succeeded":
                self.core.artifacts.validate_run(run.id, require_any=True)
        plan_scope = json.loads(engineer_run.description)["inputs"]["context"]
        context = engineering.context(
            agent.executor.snapshot,
            job=local["job"],
            runs=runs,
            results=agent.results(local["job"]),
        )
        outcome = self._outputs(engineer_run)
        if outcome["result.json"]["real_inference"] == self.text_executor.adapter.mock:
            raise Conflict("Architect must review evidence from the same cloud backend")
        sealed = next(
            a
            for a in self.core.artifacts.list(engineer_run.id)
            if a.relative_path.endswith("/engineering.json")
        )
        evidence = (
            {
                "local_attempt": {
                    "outcome": "incomplete",
                    "reason": local["completion_reason"],
                    "conclusion": "not_produced",
                    "steps": [
                        {
                            "run": step["id"],
                            "status": step["status"],
                            "artifacts": {
                                a["name"]: a["sha256"]
                                for a in step["artifacts"]
                                if a["name"] in {"response.txt", "tool-result.json"}
                            },
                        }
                        for step in local["runs"]
                    ],
                }
            }
            if local["completion_reason"]
            else {
                "local_finish_sha256": next(
                    a.sha256
                    for a in self.core.artifacts.list(runs[-1].id)
                    if a.relative_path.endswith("/tool-result.json")
                )
            }
        )
        context = {**context, "architect_plan": plan_scope["architect_plan"]}
        review = {
            "engineer_run": engineer_run.id,
            "sha256": sealed.sha256,
            "diff": outcome["engineering.json"]["candidate"]["diff"],
            **evidence,
        }
        if previous:
            old_request = validate_request(
                json.loads(self.core.runs.get(previous).description)
            )
            if "functional" in old_request["inputs"]["review"]:
                functional = self.workbench.review_evidence(job)
                if functional is None:
                    raise Conflict("The existing functional evidence is unavailable")
                review["functional"] = functional
            if (
                old_request["inputs"]["context"] != context
                or old_request["inputs"]["review"] != review
            ):
                raise Conflict("Follow-up evidence differs from the closed review")
            review["previous_review_run"] = previous
        elif self.workbench:
            result = self.workbench.review_evidence(job)
            if result is not None:
                review["functional"] = result
        return self._prepare_engineering(job, "review", context, review=review)

    def _prepare_engineering(self, job, phase, context, *, review=None):
        if self.owner.stopping or self.blocked():
            raise RecoveryRequired(
                "Unresolved execution blocks engineering preparation"
            )
        self._cloud_admission()
        if not self.engineering_state()["auth_ready"]:
            raise Conflict("A ready official DevKit text connection is required")
        existing = [r for _, r in self._requests() if r["job_id"] == job]
        if any(r["version"] != 3 for r in existing):
            raise Conflict("Request identity already belongs to another mode")
        if existing and phase == "plan" and existing[0]["phase"] == "implement":
            raise Conflict("Engineer-first requests have no Architect predecessor")
        if (
            any(r["phase"] == "plan" for r in existing)
            and phase == "implement"
            and not context["architect_plan"]
        ):
            raise Conflict("Explicitly select the completed Architect plan")
        previous = review.get("previous_review_run") if review else None
        if len(self._requests()) >= 96 and not any(
            r["phase"] == phase
            and r["inputs"].get("review", {}).get("previous_review_run") == previous
            for r in existing
        ):
            raise Conflict("Team task quota reached")
        config = self.text_executor.adapter.configuration()
        role = ROLES[phase]
        value = validate_request(
            {
                "version": 3,
                "job_id": job,
                "phase": phase,
                "role": role,
                "session_id": hashlib.sha256(
                    (
                        "gpt-engineering:"
                        + job
                        + ":"
                        + ("reviewer" if review else role)
                        + (":" + previous if previous else "")
                    ).encode()
                ).hexdigest()[:32],
                "project_id": engineering.PROJECT,
                "text": engineering.TASK,
                "inputs": {
                    "model": config["model"],
                    "effort": config["effort"],
                    "profile_digest": digest(config),
                    "context": context,
                    **({"review": review} if review is not None else {}),
                },
            }
        )
        # One distinct namespace per Team review. Later calls reuse the same Task/Run.
        key = (
            "team-engineering:" + job + ":" + ("review_followup" if previous else phase)
        )
        task = self.core.tasks.create(
            "GPT " + role.title() + " · Engineering Text",
            description=canonical(value),
            executor="team_text",
            idempotency_key=key,
        )
        if task.status == "draft":
            self.core.tasks.transition(task.id, "planned")
        run = self.core.runs.create(
            task.id,
            action=Action.build("team.turn", value),
            idempotency_key=key,
        )
        if run.status == "created":
            self.core.approvals.request(run.id, ttl_seconds=1800)
        return self.job(job)
