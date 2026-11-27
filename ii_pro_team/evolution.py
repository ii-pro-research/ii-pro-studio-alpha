"""Controlled evolution over the existing Core journal and School slots."""

import json
from collections import Counter
from uuid import uuid4

from ii_pro_core.errors import Conflict, CoreError, ValidationError
from ii_pro_core.security import digest
from ii_pro_local.engineering_executor import artifact_json
from ii_pro_school import evolution_probe as probe

HYPOTHESES = (
    ("J2-H1", "Одни ограничения в инструкции и валидаторе", "STAGE_J2_CONTRACT_REPORT.md", "wrapper"),
    ("tool-choice", "Релевантное наблюдение вместо проверки неподходящего объекта", "STAGE_G1_INCOMPLETE_LOCAL.md", "inconclusive"),
    ("memory-card", "Память проверяется на независимых задачах", "STAGE_I2_STUDY_ISOLATION.md", "inconclusive"),
    ("compare-v1", "Сравнение с навыком compare@1 не показало прироста", "STAGE_K1_PILOT_REPORT.md", "inconclusive"),
    ("candidate-context", "Local получает релевантный кандидат до анализа", "STAGE_J_FINAL_REPORT.md", "wrapper"),
    ("fragment-budget", "Точечные правки уменьшают размер ответа", "STAGE_L0_EVOLUTION_REPORT.md", "inconclusive"),
    ("L1-deadline", "Разделять время до первого фрагмента и длительность потока", "STAGE_L1_ENGINEER_REPORT.md", "inconclusive"),
    (probe.KEY, "Карта изменений AST для самостоятельного разбора Local", "STAGE_L2_EVOLUTION_REPORT.md", "experiment"),
)
EDITABLE = ("instructions", "skills", "context", "tool_profiles", "decomposition", "algorithms",
            "edit_format", "generation_parameters", "deadline")
L1_RUN = "f83596deb7be4e5393a02292a402fd8c"


def entries(store, kind):
    return [json.loads(row[0]) for row in store.all("SELECT payload_json FROM events WHERE kind=? ORDER BY id", (kind,))]


def _active_record(store):
    history = entries(store, "evolution.decision")
    last = next((e for e in reversed(history) if e["operation"] in {"accept", "rollback"}), None)
    return last if last and last["operation"] == "accept" else None


def deployment(store):
    last = _active_record(store)
    return probe.KEY if last and last["implementation"] == probe.implementation_identity() else None


def evaluate(plan, rows):
    if (len(rows) != len(plan["order"]) or
            [f'{r["case"]}:{r["arm"]}' for r in rows] != plan["order"] or
            any(r["status"] != "succeeded" or type(r["correct"]) is not bool for r in rows)):
        return {"verdict": "inconclusive", "reason": "incomplete_or_unverified_slots"}
    pairs = {}
    for row in rows:
        pairs.setdefault(row["case"], {})[row["arm"]] = row["correct"]
    gain = sum(int(x["b"]) - int(x["a"]) for x in pairs.values())
    regression = any(x["a"] and not x["b"] for x in pairs.values())
    return {"verdict": "supported" if gain >= plan["criteria"]["minimum_gain_paired_correct_answers"]
            and not regression else "not_supported", "gain": gain, "regression": regression,
            "a": sum(x["a"] for x in pairs.values()), "b": sum(x["b"] for x in pairs.values()),
            "pairs": len(pairs)}


class Evolution:
    def __init__(self, team):
        self.team, self.core, self.store = team, team.core, team.store

    def _agent(self):
        local = getattr(self.team.owner, "local_engine", None)
        return local.agents.get(probe.ANCHOR) if local else None

    def _frozen(self):
        history = entries(self.store, "evolution.plan")
        return history[0]["manifest"] if history else None

    def freeze(self, body):
        agent = self._agent()
        if not agent or set(body) != {"sha256"}:
            raise ValidationError("Exact experiment identity required")
        manifest = probe.manifest(agent.executor)
        if body["sha256"] != manifest["sha256"]:
            raise Conflict("Experimental plan changed")
        prior = self._frozen()
        if prior and prior != manifest:
            raise Conflict("The frozen experiment identity differs")
        if not prior:
            with self.store.transaction(admit=True) as conn:
                self.store.event(conn, "evolution.plan", manifest=manifest)
        return self.state()

    def prepare(self, body):
        if body != {} or self.team.blocked() or self.team.owner.stopping:
            raise Conflict("Experiment execution is blocked")
        agent = self._agent()
        if not agent:
            raise Conflict("Registered School anchor required")
        slot = probe.next_slot(agent)
        if not slot:
            raise Conflict("Experimental budget is exhausted")
        runs = agent.runs(slot["job"])
        if runs:
            self.core.approvals.request(runs[0].id)
        else:
            agent.prepare(slot["job"], task=slot["task"], requirements=probe.REQUIREMENTS, review=slot["review"])
        return self.team.owner.local_engine.detail(probe.ANCHOR, slot["job"])

    def _results(self, agent, plan):
        if not agent:
            return []
        if plan != probe.manifest(agent.executor):
            # History remains visible after a runtime upgrade. Do not re-grade it
            # or authorize deployment with different executable inputs.
            stale = []
            for label in plan["order"]:
                case, arm = label.split(":")
                job = probe.job_id(case, arm)
                runs = agent.runs(job)
                stale.append({"case": case, "arm": arm, "job": job, "status": "stale",
                              "historical_status": str(runs[0].status) if runs else None,
                              "run": runs[0].id if runs else None, "correct": None})
            return stale
        rows = []
        for slot in probe.slots(agent):
            runs = agent.runs(slot["job"])
            row = {"case": slot["case"], "arm": slot["arm"], "job": slot["job"],
                   "status": "planned", "correct": None}
            if runs:
                run = runs[0]
                row.update(run=run.id, status=str(run.status), error=run.error_code)
                if (len(runs) == 1 and run.status == "succeeded" and run.execution_stopped and
                        json.loads(run.description).get("review") == slot["review"]):
                    try:
                        observation = artifact_json(self.core, run.id, "tool-result.json")
                        receipt = artifact_json(self.core, run.id, "receipt.json")
                    except (CoreError, KeyError, ValueError):
                        row["evidence"] = "unconfirmed"
                    else:
                        row.update(correct=(observation["tool"] == "finish" and observation["status"] == "observed"
                                            and observation["result"].get("prediction", "").strip() == slot["expected"]),
                                   tool=observation["tool"], seconds=receipt["elapsed_seconds"],
                                   tokens=receipt["statistics"], owned_result=observation["status"] == "observed",
                                   input_bytes=sum(len(message["content"].encode()) for message in
                                                   agent.executor._messages(json.loads(run.action_json)["parameters"])),
                                   artifacts={a.relative_path.rsplit("/", 1)[-1]: a.sha256 for a in self.core.artifacts.list(run.id)})
            rows.append(row)
        return rows

    def _assessment(self):
        frozen = self._frozen()
        rows = self._results(self._agent(), frozen) if frozen else []
        return rows, evaluate(frozen, rows) if frozen else {"verdict": "inconclusive", "reason": "plan_not_frozen"}

    def _decision(self, operation):
        rows, result = self._assessment()
        history = entries(self.store, "evolution.decision")
        active = _active_record(self.store)
        frozen = self._frozen()
        if (operation not in {"accept", "reject", "rollback"} or not frozen or
                (operation == "accept" and (active or result["verdict"] != "supported" or history)) or
                (operation == "rollback" and not active) or
                (operation == "reject" and (active or history))):
            raise Conflict("This deployment decision is unavailable")
        return {"operation": operation, "manifest": frozen["sha256"], "evaluation": result,
                "implementation": frozen["implementation_sha256"],
                "evidence_sha256": digest(rows), "previous_decision": digest(history[-1]) if history else None}

    def decision(self, body):
        if self.team.blocked() or self.team.owner.stopping:
            raise Conflict("Deployment decision blocked")
        if set(body) == {"operation"}:
            decision = self._decision(body["operation"])
            request = {"id": uuid4().hex, **decision}
            request["sha256"] = digest(request)
            # One pending preview at a time. Reopening it is idempotent.
            previous = entries(self.store, "evolution.decision_prepared")
            done = entries(self.store, "evolution.decision")
            if previous and (not done or previous[-1]["id"] != done[-1]["id"]):
                if all(previous[-1].get(k) == v for k, v in decision.items()):
                    return self.state()
                raise Conflict("A different pending decision exists")
            with self.store.transaction(admit=True) as conn:
                self.store.event(conn, "evolution.decision_prepared", **request)
        elif set(body) == {"id", "sha256", "confirm"} and body["confirm"] is True:
            queued = entries(self.store, "evolution.decision_prepared")
            completed = entries(self.store, "evolution.decision")
            if (not queued or (queued[-1]["id"], queued[-1]["sha256"]) != (body["id"], body["sha256"])
                    or any(r["id"] == body["id"] for r in completed)):
                raise Conflict("The decision is stale or already used")
            request = queued[-1]
            if (digest({k: v for k, v in request.items() if k != "sha256"}) != request["sha256"]
                    or self._decision(request["operation"]) != {k: v for k, v in request.items() if k not in {"id", "sha256"}}):
                raise Conflict("Deployment evidence changed")
            with self.store.transaction(admit=True) as conn:
                self.store.event(conn, "evolution.decision", **request)
        else:
            raise ValidationError("Exact deployment confirmation required")
        return self.state()

    def observations(self):
        rows = self.store.all("""SELECT id,executor,status,error_code,description,action_json,started_at,finished_at
            FROM runs WHERE executor='team_text' OR executor GLOB 'local_agent_*' OR executor='engineering_worker'
            ORDER BY rowid DESC LIMIT 36""")
        result = []
        for row in rows:
            run = row["id"]
            data = {"run": run, "executor": row["executor"], "status": row["status"],
                    "generation": "unconfirmed" if row["status"] == "unknown" else None,
                    "request_bytes": len(row["action_json"].encode()), "solution": None, "category": None,
                    "issue": row["error_code"], "approval_consumed": self.store.one(
                        "SELECT count(*) FROM approvals WHERE run_id=? AND consumed_at IS NOT NULL", (run,))[0],
                    "manual_actor": None}
            facts = self.store.all("""SELECT payload_json FROM events WHERE run_id=? AND kind='team.agent_event'
                AND json_extract(payload_json,'$.agent_event.data.type')='transport.diagnostic' ORDER BY id DESC LIMIT 1""", (run,))
            if facts:
                diag = json.loads(facts[0][0])["agent_event"]["data"]
                data["transport"] = diag.get("termination_reason")
                data["seconds"] = diag.get("elapsed_seconds")
                if diag.get("api_error_code"):
                    data.update(category="provider", issue=diag["api_error_code"])
                elif diag.get("termination_reason") == "deadline":
                    data.update(category="undetermined", issue="deadline")
                messages = self.store.all("""SELECT payload_json FROM events WHERE run_id=? AND kind='team.agent_event'
                    AND json_extract(payload_json,'$.agent_event.data.provisional')=1 ORDER BY id""", (run,))
                if messages:
                    data["provisional_response_bytes"] = sum(len(json.loads(message[0])["agent_event"]["data"]
                                                                 .get("delta", "").encode()) for message in messages)
            if row["error_code"] in {"artifact_failed", "execution_interrupted"}:
                data["category"] = "infrastructure"
            if row["status"] == "succeeded":
                try:
                    self._observed_success(row, data)
                except (CoreError, KeyError, ValueError, TypeError):
                    data["evidence"] = "unavailable"
            result.append(data)
        return result

    def _observed_success(self, row, data):
        run = row["id"]
        if row["executor"] == "team_text":
            answer = artifact_json(self.core, run, "result.json")
            data.update(generation="completed", seconds=answer.get("elapsed_seconds"), tokens=answer.get("usage"),
                        response_bytes=len(answer["text"].encode()))
            if json.loads(row["description"]).get("version") == 4:
                assessment = artifact_json(self.core, run, "engineering.json")
                data["solution"] = assessment["state"]
                if assessment["state"] == "rejected":
                    data.update(issue=assessment["reason"], category="undetermined")
        elif row["executor"].startswith("local_agent_"):
            receipt = artifact_json(self.core, run, "receipt.json")
            observation = artifact_json(self.core, run, "tool-result.json")
            fact = observation["result"]
            data.update(generation="completed", seconds=receipt["elapsed_seconds"], tokens=receipt["statistics"],
                        solution=observation["status"], contribution={k: fact.get(k) for k in ("defect", "prediction", "distinguishing_test")},
                        tool=observation["tool"])
            responses = [a for a in self.core.artifacts.list(run) if a.relative_path.endswith("/response.txt")]
            if len(responses) == 1:
                data["response_bytes"] = len(self.core.artifacts.read(responses[0].id))
            if observation["status"] == "rejected":
                issue = fact.get("reason")
                data.update(issue=issue, category="model_output" if issue in {"invalid_proposal", "repeated_action"} else "undetermined")
        else:
            description = json.loads(row["description"])
            if description.get("phase") == "project_test":
                tests = self.team.projects.test_result(self.core.runs.get(run))
                data["functional"] = {"passed": tests["passed"], "total": tests["total"]}

    def state(self):
        agent = self._agent()
        frozen = self._frozen()
        rows, result = self._assessment()
        decisions = entries(self.store, "evolution.decision")
        previews = entries(self.store, "evolution.decision_prepared")
        pending = previews[-1] if previews and (not decisions or decisions[-1]["id"] != previews[-1]["id"]) else None
        observations = self.observations()
        counts = Counter((r["category"] or "undetermined", r["issue"]) for r in observations if r["issue"])
        return {"plan": frozen or (probe.manifest(agent.executor) if agent else None),
                "frozen": frozen is not None, "slots": rows, "evaluation": result, "pending_decision": pending,
                "active": deployment(self.store), "decisions": decisions, "blocked": self.team.blocked(),
                "rollback_available": bool(_active_record(self.store)),
                "local_enabled": bool(agent and self.team.owner.local_engine.live), "anchor": probe.ANCHOR,
                "observations": observations, "problems": [{"category": c, "issue": i, "count": n}
                                                            for (c, i), n in counts.most_common()],
                "hypotheses": [{"id": k, "title": t, "evidence": p, "status": s} for k, t, p, s in HYPOTHESES],
                "design_domains": EDITABLE, "registered_candidate_domains": ["context", "algorithms"]}
