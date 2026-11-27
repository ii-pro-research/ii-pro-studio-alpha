"""Studio study planning over the existing School, LocalAgent and Core journals."""

from hashlib import sha256
import json
from pathlib import Path

from ii_pro_core.errors import Conflict, RecoveryRequired, ValidationError
from ii_pro_core.memory_transfer import source_execution_blocked
from ii_pro_core.security import Action, digest, no_links, public_text
from ii_pro_core.storage import utc_now
from ii_pro_school.mentors import MentorFeedback
from ii_pro_school.study_curriculum import HINT, LESSON, MODES, comparative_suites
from ii_pro_team.engineering import PATH, PROJECT, SOURCE

from .local_engine import identity


ARCHIVE = {
    "План Architect": "stage-g/ARCHITECT_RESULT.json",
    "Правка Engineer": "stage-g/ENGINEER_RESULT.json",
    "Первый шаг Qwen": "stage-g/LOCAL_STEP1_RESULT.json",
    "Второй шаг Qwen": "stage-g/LOCAL_STEP2_RESULT.json",
    "Разбор наставника": "stage-g3/FOLLOWUP_RESULT.json",
    "Функциональная проверка H.1": "stage-h2/h1-evidence.json",
    "Откат H.2": "stage-h2/test-result.json",
    "Досье карточки": "stage-h2/memory-review-draft.json",
}
PILOT_ORDER = (("a", "study-sum-offset-v1"), ("b", "study-sum-offset-v1"),
               ("b", "study-count-zero-v1"), ("a", "study-count-zero-v1"))
PILOT_CARD = "pricing-h1-discount-evidence"
PILOT_CARD_SHA256 = "e69e5f39012ef93c3ef99383d4e28e987ad41d36b16789a1355ea91bb3e46bc4"


def archived_evidence():
    root = Path(__file__).resolve().parents[1] / ".local"
    sources = []
    for name, relative in ARCHIVE.items():
        path = root / relative
        no_links(path)
        if not path.is_file():
            continue
        with path.open("rb") as source:
            data = source.read(262145)
        if len(data) <= 262144:
            sources.append({"name": name, "record": relative, "sha256": sha256(data).hexdigest()})
    return sources


class Education:
    def __init__(self, owner, domain, verifier):
        self.owner, self.domain, self.verifier = owner, domain, verifier
        self.school, self.core = domain.school, domain.school.core
        self.store = self.core.store
        self.verifier.core = self.core
        self.suites = {s["id"]: s for s in comparative_suites()}
        self.domain._install(self.suites.values())

    def plan(self):
        rows = self.store.all("SELECT payload_json FROM events WHERE kind='school.study.plan'")
        return json.loads(rows[-1][0]) if rows else None

    def worker_contract(self):
        configuration = self.domain.worker.approval_configuration()
        backend = configuration["backend"]
        return digest({"protocol": configuration["protocol"], "limits": configuration["limits"],
                       "profile": backend.get("profile_digest"), "review": backend.get("review_digest")})

    def pilot_run(self, run_id):
        plan = self.plan()
        if not plan or any(
            [item["sha256"] for item in plan["memory"].get(suite, [])] != [PILOT_CARD_SHA256]
            for _, suite in PILOT_ORDER
        ):
            return False
        rows = self.store.all("""SELECT p.cycle_id,p.suite_id FROM school_v2_proposals p
            WHERE p.run_id=? OR EXISTS (
                SELECT 1 FROM school_v2_submissions s
                JOIN school_v2_packages k ON k.submission_id=s.id
                JOIN school_v2_evaluations e ON e.package_id=k.id
                WHERE s.source_run_id=p.run_id AND s.cycle_id=p.cycle_id
                  AND s.suite_id=p.suite_id AND s.origin='model_response' AND e.run_id=?)""",
            (run_id, run_id))
        if len(rows) != 1:
            return False
        return any(rows[0]["cycle_id"] == plan["arms"][arm] and rows[0]["suite_id"] == suite
                   for arm, suite in PILOT_ORDER)

    def require_pilot_order(self, run_id):
        plan = self.plan()
        if not plan or not self.pilot_run(run_id):
            return
        for arm, suite in PILOT_ORDER:
            rows = self.store.all("""SELECT r.id,r.status,r.execution_stopped
                FROM school_v2_proposals p JOIN runs r ON r.id=p.run_id
                WHERE p.cycle_id=? AND p.suite_id=?""", (plan["arms"][arm], suite))
            if rows and rows[0]["id"] == run_id:
                return
            if len(rows) != 1 or rows[0]["status"] != "succeeded" or not rows[0]["execution_stopped"]:
                raise RecoveryRequired("Finish the preceding frozen pilot slot before dispatch")

    def comparative_origin_blocked(self, *, pilot=False):
        with self.store.lock:
            return source_execution_blocked(self.store.connection, PILOT_CARD, pilot=pilot)

    def pilot_card_ready(self):
        cards = self.core.memory.recall("python debugging", allowed_ids={PILOT_CARD})
        return len(cards) == 1 and cards[0]["sha256"] == PILOT_CARD_SHA256

    def create_plan(self, body):
        if set(body) != {"id"}:
            raise ValidationError("Explicit study identity required")
        key = identity(body["id"])
        old = self.plan()
        if old:
            if key != old["id"]:
                raise Conflict("One comparative study per Studio workspace")
            return
        profile = self.school.adapter.profile
        if profile.options["num_ctx"] != 2048 or profile.options["num_predict"] > 384:
            raise Conflict("Use the bounded study profile")
        # Freeze only the reviewed teaching principle. An exam answer cannot enter this bank.
        allowed = {PILOT_CARD}
        memory = {sid: self.core.memory.recall("python debugging conditions boundary tests " + suite["public"]["requirements"],
                  language="python", category="debugging", allowed_ids=allowed) for sid, suite in self.suites.items()}
        if not all(memory.values()):
            raise Conflict("Confirm the bounded H.1 knowledge card before planning A/B")
        plan = {"id": key, "model": profile.model, "profile": profile.id, "options": profile.options,
                "generation_deadline_seconds": profile.deadline_seconds, "worker_deadline_ms": self.domain.worker.limits.runtime_ms,
                "worker_contract": self.worker_contract(), "attempts_per_task": 1, "tools": [], "memory": memory,
                "arms": {a: digest([key, a])[:32] for a in MODES},
                "suites": self.catalog(), "mentor_hints": 1}
        for cycle in plan["arms"].values():
            self.domain.create_cycle("ui-qwen", seed=profile.options["seed"], cycle_id=cycle)
        with self.store.transaction(admit=True) as conn:
            self.store.event(conn, "school.study.plan", **plan)

    def prepare_pilot(self, body):
        if body != {}:
            raise ValidationError("The pilot order and scope are fixed")
        if not self.pilot_card_ready():
            raise Conflict("Confirm the exact H.1 knowledge card before preparing the pilot")
        if self.plan() is None:
            self.create_plan({"id": digest(["comparative-pilot-v1", PILOT_ORDER])[:32]})
        for arm, suite in PILOT_ORDER:
            self.exam({"arm": arm, "suite": suite})

    def catalog(self):
        return [{"id": sid, "title": s["public"]["title"], "category": s["public"]["category"],
                 "task_sha256": s["task_digest"], "suite_sha256": s["tests_digest"], "cases": len(s["cases"])}
                for sid, s in self.suites.items()]

    def _slot(self, body):
        if (set(body) != {"arm", "suite"} or not all(isinstance(value, str) for value in body.values())
                or body["arm"] not in MODES or body["suite"] not in self.suites):
            raise ValidationError("Choose a plan arm and one frozen task")
        plan = self.plan()
        if (plan is None or plan["profile"] != self.school.adapter.profile.id
                or plan["suites"] != self.catalog() or plan.get("worker_contract") != self.worker_contract()):
            raise Conflict("Study plan is missing or its model, Worker profile or curriculum changed")
        return plan, plan["arms"][body["arm"]], body["suite"]

    def exam(self, body):
        plan, cycle, suite = self._slot(body)
        arm = body["arm"]
        memory = plan["memory"][suite] if arm == "b" else []
        hint = HINT if arm == "c" else None
        run = self.domain.prepare_model_request(cycle, suite, reference=(memory, hint))
        if run.status == "created":
            self.core.approvals.request(run.id, ttl_seconds=1800)

    def functional(self, body):
        _, cycle, suite = self._slot(body)
        run = self.store.one("SELECT run_id FROM school_v2_proposals WHERE cycle_id=? AND suite_id=?", (cycle, suite))[0]
        if self.core.runs.get(run).status != "succeeded":
            raise RecoveryRequired("Confirmed answer required")
        saved = self.store.all("SELECT id FROM school_v2_submissions WHERE cycle_id=? AND suite_id=?", (cycle, suite))
        submission = saved[0][0] if saved else self.domain.submit_model_response(cycle, suite, run)
        if json.loads(self.domain._submission(submission)["static_json"])["eligible"]:
            execution = self.domain.prepare_run(self.domain.prepare(submission))
            if execution.status == "created":
                self.core.approvals.request(execution.id, ttl_seconds=1800)

    def lesson(self, body):
        if set(body) not in ({"job", "mode"}, {"job", "mode", "after"}) or body["mode"] not in MODES.values():
            raise ValidationError("Choose a lesson mode")
        job = identity(body["job"])
        if not self.owner.local_engine or PROJECT not in self.owner.local_engine.agents:
            raise Conflict("Register the public pricing fixture")
        agent = self.owner.local_engine.agents[PROJECT]
        if dict(agent.executor.snapshot.files) != {PATH: SOURCE}:
            raise Conflict("This lesson uses only the saved pricing fixture")
        hint = "Compare the arithmetic operator and test a value for which the discount is nonzero."
        task = LESSON["task"]
        if "after" in body:
            source = agent.runs(identity(body["after"]))
            if not source or body["mode"] != "mentored":
                raise Conflict("Continue from an attributed lesson review in mentor mode")
            reviews = self.store.all("SELECT payload_json FROM events WHERE run_id=? AND kind='school.study.mentor_note'", (source[-1].id,))
            if len(reviews) != 1:
                raise Conflict("Save mentor feedback before preparing the next exercise")
            task = json.loads(reviews[0][0])["next_step"]
            job = digest(["school-study-next", source[-1].id])[:32]
        scope = {"mode": body["mode"], "source": SOURCE, "candidate": SOURCE.replace("price +", "price -"),
                 "hint": hint if body["mode"] == "mentored" else ""}
        old = agent.runs(job)
        if old:
            metadata = json.loads(old[0].description)
            if (metadata.get("study"), metadata["task"]) != (scope, task):
                raise Conflict("Lesson identity already belongs to another context")
            if old[-1].status == "created":
                self.core.approvals.request(old[-1].id)
        else:
            agent.prepare(job, task=task, requirements=LESSON["requirements"], study=scope)
        return self.owner.local_engine.detail(PROJECT, job)

    def feedback(self, body):
        if set(body) != {"job", "author", "text", "next_step"}:
            raise ValidationError("An attributed mentor note and next step are required")
        job = identity(body["job"])
        agent = self.owner.local_engine.agents.get(PROJECT)
        runs = agent.runs(job) if agent else []
        if not runs or not json.loads(runs[0].description).get("study") or runs[-1].status != "succeeded":
            raise Conflict("A completed lesson is required")
        note = MentorFeedback("gpt_engineer", public_text(body["author"], limit=80, empty=False),
                              public_text(body["text"], limit=800, empty=False))
        next_step = public_text(body["next_step"], limit=150, empty=False)
        if len(next_step.encode()) > 240:
            raise ValidationError("Mentor subtask exceeds the input byte budget")
        with self.store.transaction(admit=True) as conn:
            previous = conn.execute("SELECT 1 FROM events WHERE run_id=? AND kind='school.study.mentor_note'",
                                    (runs[-1].id,)).fetchone()
            if previous:
                raise Conflict("This attempt already has an attributed mentor note")
            self.store.event(conn, "school.study.mentor_note", run_id=runs[-1].id,
                             job=job, author=note.author, text=note.text, next_step=next_step, origin="manual")

    def knowledge(self, body):
        if body != {}:
            raise ValidationError("Only the fixed H.1 evidence dossier can be prepared")
        draft = Path(__file__).resolve().parents[1] / ".local/stage-h2/memory-review-draft.json"
        no_links(draft)
        if not draft.is_file():
            raise Conflict("Local H.1 dossier is unavailable")
        with draft.open("rb") as source:
            data = source.read(16385)
        if len(data) > 16384:
            raise ValidationError("Knowledge dossier exceeds the import limit")
        body = json.loads(data)
        if body["id"] != "pricing-h1-discount-evidence" or body["record_body"]["evidence"]["passed"] != 6:
            raise Conflict("Expected the confirmed six-test H.1 dossier")
        entry_id = body["id"]
        record = {**body["record_body"],
                  "category": "debugging", "language": "python", "tools": ["finish", "functional_tests"],
                  "task": "Diagnose a logic bug in percentage discount and verify the correction.",
                  "mistake": "Empty diff and valid syntax did not establish program behavior.",
                  "approach": "Trace the changed expression, then use a discriminating boundary or ordinary case.",
                  "observation": "An empty diff and valid syntax do not verify logic. Trace the calculation, then run a functional case that distinguishes both versions."}
        self.verifier.evidence({"body": record})
        self.core.memory.record(entry_id, record, tested=True)
        entry = self.core.memory.get(entry_id)
        key = "school-knowledge:" + entry["sha256"]
        task = self.core.tasks.create("Confirm bounded H.1 knowledge", executor=self.verifier.name, idempotency_key=key)
        if task.status == "draft":
            self.core.tasks.transition(task.id, "planned")
        run = self.core.runs.create(task.id, idempotency_key=key,
                                   action=Action.build("memory.verify", {"entry": entry_id, "sha256": entry["sha256"]}))
        if run.status == "created":
            self.core.approvals.request(run.id, ttl_seconds=1800)
        return self.knowledge_state()

    def knowledge_state(self):
        rows = self.store.all("SELECT id FROM runs WHERE executor=? ORDER BY rowid DESC LIMIT 1", (self.verifier.name,))
        if not rows:
            return None
        run = self.core.runs.get(rows[0][0])
        action = json.loads(run.action_json)["parameters"]
        entry = self.core.memory.get(action["entry"])
        permits = self.store.all("SELECT id,status,expires_at,spec_digest FROM approvals WHERE run_id=? ORDER BY rowid DESC LIMIT 1", (run.id,))
        permit = dict(permits[0]) if permits else None
        if permit and permit["status"] == "pending" and permit["expires_at"] <= utc_now():
            permit["status"] = "expired"
        elif permit and permit["status"] == "pending" and not self.core.approvals.matches(
                self.store.one("SELECT * FROM runs WHERE id=?", (run.id,)), self.core.approvals.spec_for(permit["id"])):
            permit["status"] = "invalidated"
        return {"run": run.id, "status": run.status, "entry": action["entry"],
                "sha256": action["sha256"], "body": entry["body"], "history": entry["history"], "approval": permit}

    def decide_knowledge(self, body):
        if set(body) != {"run", "approval", "digest"}:
            raise ValidationError("Exact knowledge approval required")
        view = self.knowledge_state()
        if not view or view["run"] != body["run"] or view["status"] != "created" or not view["approval"]:
            raise Conflict("No pending knowledge operation")
        permit = view["approval"]
        if permit["id"] != body["approval"] or permit["spec_digest"] != body["digest"] or permit["status"] != "pending":
            raise Conflict("Knowledge approval identity or session changed")
        blocked = self.owner.team.blocked() if self.owner.team else self.owner.functional.blocked()
        if self.owner.threads or self.owner.stopping or blocked:
            raise Conflict("Resolve active or unknown work before changing trust")
        self.core.approvals.decide(permit["id"], allow=True)
        self.core.runs.execute(view["run"], approval_id=permit["id"])
        return self.knowledge_state()

    def state(self):
        plan = self.plan()
        arms = {}
        if plan:
            for arm, cycle in plan["arms"].items():
                rows = self.store.all("""SELECT p.suite_id,p.run_id,s.static_json,e.run_id AS functional_run,
                    r.body_json AS measured FROM school_v2_proposals p
                    LEFT JOIN school_v2_submissions s ON s.source_run_id=p.run_id
                    LEFT JOIN school_v2_packages k ON k.submission_id=s.id
                    LEFT JOIN school_v2_evaluations e ON e.package_id=k.id
                    LEFT JOIN school_v2_results r ON r.package_id=k.id WHERE p.cycle_id=?""", (cycle,))
                attempts = {}
                for row in rows:
                    attempt = self.owner._attempt(row["run_id"])
                    attempt["execution_blocked"] = self.owner.functional.blocked(row["run_id"])
                    stats = attempt.get("statistics", {})
                    seconds = stats.get("eval_duration")
                    tokens = stats.get("eval_count")
                    attempt["tokens_per_second"] = tokens * 1_000_000_000 / seconds if seconds and tokens is not None else None
                    measured = json.loads(row["measured"]) if row["measured"] else None
                    static = json.loads(row["static_json"]) if row["static_json"] else None
                    attempts[row["suite_id"]] = {"attempt": attempt, "functional_run": row["functional_run"],
                                                  "functional": measured, "static": static}
                measured = [v["functional"] for v in attempts.values() if v["functional"]]
                finished = [v for v in attempts.values() if v["functional"] or (v["static"] and not v["static"]["eligible"])]
                arms[arm] = {"attempts": attempts, "completed": len(finished),
                             "solved": sum(m["passed"] == m["total"] for m in measured),
                             "passed": sum(m["passed"] for m in measured), "total": sum(m["total"] for m in measured),
                             "retries": sum(v["attempt"]["attempt"] - 1 for v in attempts.values()),
                             "tool_errors": None,
                             "hints": sum(bool(v["attempt"].get("response")) for v in attempts.values()) if arm == "c" else 0}
        lessons = []
        if self.owner.local_engine and PROJECT in self.owner.local_engine.agents:
            for row in self.owner.local_engine.state()["jobs"]:
                if row["project"] == PROJECT:
                    agent = self.owner.local_engine.agents[PROJECT]
                    runs = agent.runs(row["job"])
                    mode = json.loads(runs[0].description).get("study") if runs else None
                    if mode:
                        detail = self.owner.local_engine.detail(PROJECT, row["job"])
                        detail["mode"] = mode["mode"]
                        detail["feedback"] = [json.loads(r[0]) for r in self.store.all(
                            "SELECT payload_json FROM events WHERE run_id=? AND kind='school.study.mentor_note'", (runs[-1].id,))]
                        lessons.append(detail)
        return {"lesson": {**LESSON, "evidence": archived_evidence()}, "lessons": lessons, "catalog": self.catalog(), "model": self.school.adapter.profile.model,
                "knowledge": self.knowledge_state(), "experiment": plan["id"] if plan else None,
                "arms": arms, "modes": MODES, "pilot_order": PILOT_ORDER,
                "pilot_card_verified": self.pilot_card_ready(),
                "execution_blocked": self.owner.functional.pilot_blocked()}
