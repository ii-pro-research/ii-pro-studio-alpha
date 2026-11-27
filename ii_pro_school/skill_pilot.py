"""Two independent paired review tasks. The grader stays outside model input."""

import json
from hashlib import sha256
from pathlib import Path

from ii_pro_core.errors import Conflict, RecoveryRequired
from ii_pro_core.security import digest
from ii_pro_local.skills import BY_ID, packet


# Expectations are trusted, fixed observations about the supplied snippets.
CASES = (
    ("threshold", "def accepted(x):\n    return x > 4", "def accepted(x):\n    return x >= 4",
     "Predict accepted(4) in the candidate.", "True"),
    ("retry-count", "def retries(x):\n    return min(x, 2)", "def retries(x):\n    return max(x, 2)",
     "Predict retries(5) in the candidate.", "5"),
)
ORDER = ((0, "a"), (0, "b"), (1, "b"), (1, "a"))
REQUIREMENTS = "Write the exact Python value alone in prediction. Explain the difference separately."
ANCHOR = "pricing"


def plan(agent):
    cfg = agent.executor.approval_configuration()
    cases = [{"id": case[0], "sha256": digest(case)} for case in CASES]
    baseline = {"executor": agent.executor.revision, "dependencies": cfg["dependencies"], "profile": cfg["profile"]}
    spec = {"model": cfg["profile"], "baseline": digest(baseline), "skill": BY_ID["compare"].reference(),
            "snapshot": agent.executor.snapshot.identity, "cases": cases, "order": ORDER, "steps_per_slot": 1,
            "tools": ["finish"], "grader": digest([CASES, REQUIREMENTS, sha256(Path(__file__).read_bytes()).hexdigest()])}
    return {**spec, "sha256": digest(spec)}


def slots(agent):
    frozen = plan(agent)
    # Keep slot IDs stable when the runtime changes. An old series must be
    # rechecked, not silently replaced with four new model calls.
    for index, arm in ORDER:
        name, before, after, task, expected = CASES[index]
        job = digest(["skill-pilot-v1", name, arm])[:32]
        provenance = digest([frozen["sha256"], name, arm])
        review = packet("pilot", before, after, provenance,
                        skill=BY_ID["compare"].reference() if arm == "b" else None)
        yield {"job": job, "case": name, "arm": arm, "task": task, "review": review, "expected": expected}


def next_slot(agent):
    for slot in slots(agent):
        history = agent.runs(slot["job"])
        if not history:
            return slot
        run = history[0]
        if json.loads(run.description).get("review") != slot["review"]:
            raise Conflict("The frozen skills experiment changed")
        if run.status == "created":
            return slot
        if run.status != "succeeded" or not run.execution_stopped:
            raise RecoveryRequired("The preceding skill experiment slot did not complete")
    return None


def score(agent, job, review, observation):
    for slot in slots(agent):
        if (slot["job"], slot["review"]) == (job, review):
            result = observation["result"]
            passed = (observation["tool"] == "finish" and observation["status"] == "observed"
                      and result["prediction"].strip() == slot["expected"])
            return {"case": slot["case"], "arm": slot["arm"], "passed": passed,
                    "error": None if passed else result.get("reason", "prediction_mismatch"),
                    "plan_sha256": plan(agent)["sha256"]}
    raise Conflict("The result is outside the frozen experiment")


def outcomes(agent):
    history = []
    for slot in slots(agent):
        result = {"job": slot["job"], "case": slot["case"], "arm": slot["arm"], "status": "planned",
                  "pass": None, "error": None, "run": None, "seconds": None, "tokens": None}
        runs = agent.runs(slot["job"])
        if runs:
            run = runs[0]
            result.update(status=run.status, error=run.error_code, run=run.id)
            if run.status == "succeeded":
                obs = agent.results(slot["job"]).get("1", {})
                grade = obs.get("skill_pilot", {})
                result["error"] = grade.get("error")
                result["pass"] = grade.get("passed")
                result["plan_sha256"] = grade.get("plan_sha256")
                for art in agent.core.artifacts.list(run.id):
                    if art.relative_path.endswith("/receipt.json"):
                        receipt = json.loads(agent.core.artifacts.read(art.id))
                        result.update(seconds=receipt["elapsed_seconds"], tokens=receipt["statistics"])
        history.append(result)
    return history
