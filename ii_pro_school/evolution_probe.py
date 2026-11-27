"""Frozen independent Local probes for a deterministic AST context transform."""

import ast
import json
from pathlib import Path
from textwrap import dedent

from ii_pro_core.errors import Conflict, RecoveryRequired
from ii_pro_core.security import digest
from ii_pro_local.engineering_executor import artifact_json
from ii_pro_local.skills import packet

KEY = "local-ast-map-v1"
BASELINE = "bounded-review-v1"
ANCHOR = "pricing"
MODES = {"evolution_a", "evolution_b"}
REQUIREMENTS = "Put the exact Python result alone in prediction. Explain your reasoning separately."
# Separate from the development examples (pricing and task-manager routing).
CASES = (
    ("window", "def window(n):\n    return list(range(1, n))",
     "def window(n):\n    return list(range(1, n + 1))", "Predict window(3) for the candidate.", "[1, 2, 3]"),
    ("fee", "def fee(x):\n    return x * 2 if x > 3 else x + 1",
     "def fee(x):\n    return x * 2 if x >= 3 else x + 1", "Predict fee(3) for the candidate.", "6"),
)
ORDER = ((0, "a"), (0, "b"), (1, "b"), (1, "a"))
CRITERIA = {"minimum_gain_paired_correct_answers": 1, "require_no_pair_regression": True,
            "require_all_slots_complete": True, "max_candidate_input_bytes": 1470,
            "expected_generations": 4, "report_time_and_tokens": True}


def structural_map(source, candidate):
    """Return changed AST node types, without running code or producing an answer."""
    def signature(code):
        tree = ast.parse(dedent(code))
        return [type(node).__name__ for node in ast.walk(tree)
                if isinstance(node, (ast.operator, ast.cmpop, ast.boolop, ast.unaryop))]
    try:
        before, after = signature(source), signature(candidate)
    except (SyntaxError, ValueError, RecursionError):
        return None
    for i in range(min(len(before), len(after))):
        if before[i] != after[i]:
            return {"source_operation": before[i], "candidate_operation": after[i]}
    if len(before) != len(after):
        index = min(len(before), len(after))
        return {"source_operation": before[index] if index < len(before) else None,
                "candidate_operation": after[index] if index < len(after) else None}
    return None


def manifest(executor):
    cfg = executor.approval_configuration()
    plan = {"id": KEY, "baseline": BASELINE, "candidate": KEY,
            "hypothesis": "A deterministic map of changed AST operations improves independent Local predictions.",
            "development": ["G.pricing", "J.task-manager"],
            "cases": [{"id": item[0], "sha256": digest(item)} for item in CASES],
            "order": [f"{CASES[i][0]}:{arm}" for i, arm in ORDER],
            "criteria": CRITERIA, "snapshot": executor.snapshot.identity,
            "model": cfg["profile"], "executor_dependencies": cfg["dependencies"],
            "implementation_sha256": implementation_identity(),
            "budget": {"local_generations": 4, "steps_per_slot": 1, "cloud": 0, "worker": 0},
            "tools": ["finish"]}
    return {**plan, "sha256": digest(plan)}


def implementation_identity():
    return digest(Path(__file__).read_text(encoding="utf-8"))


def job_id(case, arm):
    return digest([KEY, case, arm])[:32]


def reserved(job):
    return any(job == job_id(case[0], arm) for case in CASES for arm in ("a", "b"))


def slots(agent):
    plan = manifest(agent.executor)
    for index, arm in ORDER:
        key, source, candidate, task, expected = CASES[index]
        job = job_id(key, arm)
        review = packet(f"evolution_{arm}", source, candidate, digest([plan["sha256"], key, arm]))
        yield {"job": job, "case": key, "arm": arm, "task": task, "expected": expected, "review": review}


def next_slot(agent):
    plan = manifest(agent.executor)
    frozen = agent.core.store.all("SELECT payload_json FROM events WHERE kind='evolution.plan' ORDER BY id LIMIT 1")
    if not frozen or json.loads(frozen[0][0])["manifest"] != plan:
        raise Conflict("The exact experimental plan must be frozen before preparation")
    if agent.core.store.all("SELECT 1 FROM events WHERE kind IN ('evolution.decision_prepared','evolution.decision') LIMIT 1"):
        raise Conflict("Experimental decisions close further probes")
    for slot in slots(agent):
        runs = agent.runs(slot["job"])
        if not runs:
            return slot
        if len(runs) != 1 or json.loads(runs[0].description).get("review") != slot["review"]:
            raise Conflict("Experimental slot identity changed")
        if runs[0].status == "created":
            return slot
        if runs[0].status != "succeeded" or not runs[0].execution_stopped:
            raise RecoveryRequired("Previous experimental outcome is not confirmed")
        artifact_json(agent.core, runs[0].id, "tool-result.json")
        artifact_json(agent.core, runs[0].id, "receipt.json")
    return None
