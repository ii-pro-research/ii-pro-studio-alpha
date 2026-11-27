"""Cloud-share boundary for the developer-owned pricing laboratory."""

import re

from ii_pro_core.errors import ValidationError
from ii_pro_core.security import canonical, public_text
from ii_pro_local.broker import TOOL_ARGS, execute_tool, proposal
from ii_pro_local.snapshot import Snapshot
from ii_pro_school.adapters import _strict_json

PROJECT = "pricing"
PATH = "pricing.py"
SOURCE = "def discounted(price, percent):\n    return price + price * percent / 100\n"
TASK = "Fix discount calculation."
CRITERIA = "A 10% discount on 100 must return 90. Keep the function signature."
INSTRUCTIONS = {
    "diagnostic": "You are GPT Architect. Return only the single line requested by the user. No tools or file access.",
    "architect": (
        "You are GPT Architect. Plan from the supplied public fixture only. No tools or file access. "
        'Return only JSON: {"plan":["short step"],"checks":["observable check"]}.'
    ),
    "mentor": (
        "You are GPT Architect mentoring after an incomplete Local attempt. No tools or files. "
        "Use observed facts only as evidence, explain ineffective tool choices and give coaching. "
        "Do not invent a Local conclusion or claim functional tests. Return only JSON: "
        '{"summary":"review summary","facts":["confirmed fact"],"assumptions":[],"coaching":["practice recommendation"]}.'
    ),
    "reviewer": (
        "You are GPT Architect reviewing the supplied engineering evidence. No tools or file access. "
        "Distinguish syntax checks and observations from functional execution. Return only JSON: "
        '{"verdict":"accepted or changes_requested","summary":"public assessment","checks":["remaining check"]}.'
    ),
    "mentor_tested": (
        "You are GPT Architect mentoring an incomplete Local attempt. No tools or files. "
        "Explain ineffective tool choices and distinguish sealed functional test evidence from Local observations. "
        "Do not invent a Local conclusion or generalize beyond the listed tests. Return only JSON: "
        '{"summary":"review summary","facts":["confirmed fact"],"assumptions":[],"coaching":["practice recommendation"]}.'
    ),
    "engineer": (
        "You are GPT Engineer. Use only the supplied public fixture and evidence. No tools or file access. "
        "Do not claim any changes or tests were run. Return only JSON: "
        '{"path":"pricing.py","before":"exact source fragment","after":"replacement",'
        '"explanation":"public engineering explanation","checks":["proposed verification"]}.'
    ),
}
REASONS = {
    "invalid_proposal",
    "repeated_action",
    "tool_budget_exhausted",
    "operator_stop",
    "outside_tool_scope",
    "anchor_not_unique",
}
STATES = {"created", "running", "succeeded", "failed", "cancelled", "unknown"}


def _array(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 5:
        raise ValidationError("Expected a short list of public explanations")
    return [public_text(item, limit=180, empty=False) for item in value]


def plan(value):
    if not isinstance(value, dict) or set(value) != {"plan", "checks"}:
        raise ValidationError("Expected a structured Architect plan")
    return {key: _array(value[key]) for key in ("plan", "checks")}


def validate_context(value):
    if not isinstance(value, dict) or set(value) != {
        "version",
        "snapshot",
        "revision",
        "source",
        "criteria",
        "execution_tools",
        "local_tools",
        "source_job",
        "observations",
        "unresolved",
        "architect_plan",
    }:
        raise ValidationError("Invalid engineering context")
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or value["source"] != {PATH: SOURCE}
        or value["criteria"] != [CRITERIA]
        or value["execution_tools"] != []
        or value["local_tools"] != sorted(TOOL_ARGS)
        or value["unresolved"] != ["Functional tests have not run."]
    ):
        raise ValidationError("Only the public pricing fixture is shareable")
    for key, size in (("snapshot", 64), ("revision", 40), ("source_job", 32)):
        if key == "source_job" and value[key] is None:
            continue
        if not isinstance(value[key], str) or not re.fullmatch(
            rf"[a-f0-9]{{{size}}}", value[key]
        ):
            raise ValidationError("Invalid engineering evidence identity")
    rows = value["observations"]
    if (
        not isinstance(rows, list)
        or len(rows) > 6
        or (rows and value["source_job"] is None)
    ):
        raise ValidationError("Invalid local observations")
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) != {
            "step",
            "run",
            "status",
            "tool",
            "outcome",
            "reason",
            "result",
        }:
            raise ValidationError("Invalid local observation fields")
        if (
            type(row["step"]) is not int
            or row["step"] != index
            or not isinstance(row["run"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", row["run"])
            or not isinstance(row["status"], str)
            or row["status"] not in STATES
            or (
                row["tool"] is not None
                and (not isinstance(row["tool"], str) or row["tool"] not in TOOL_ARGS)
            )
            or row["outcome"] not in (None, "observed", "rejected")
            or (
                row["reason"] is not None
                and (not isinstance(row["reason"], str) or row["reason"] not in REASONS)
            )
            or row["result"]
            not in (
                None,
                "pricing.py listed",
                "pricing.py read (see frozen source)",
                "committed snapshot inspected",
                "Git diff was empty",
                "Original Python syntax valid",
                "Original Python syntax invalid",
                "source search completed",
                "Python syntax valid",
                "Python syntax invalid",
                "candidate proposed",
                "candidate rejected",
                "report received",
            )
        ):
            raise ValidationError("Invalid local observation")
    approved = value["architect_plan"]
    if approved is not None:
        if not isinstance(approved, dict) or set(approved) != {"run", "sha256", "plan"}:
            raise ValidationError("Invalid Architect provenance")
        if any(
            not isinstance(approved[key], str)
            or not re.fullmatch(rf"[a-f0-9]{{{size}}}", approved[key])
            for key, size in (("run", 32), ("sha256", 64))
        ):
            raise ValidationError("Invalid Architect evidence hashes")
        plan(approved["plan"])
    return value


def shareable(snapshot):
    return (
        snapshot.paths == (PATH,)
        and snapshot.files == ((PATH, SOURCE),)
        and snapshot.before
        in (
            ((PATH, SOURCE),),
            ((PATH, SOURCE.replace("price +", "price -")),),
        )
    )


def context(snapshot, *, job=None, runs=(), results=None):
    if not shareable(snapshot):
        raise ValidationError("Only the exact public pricing fixture is shareable")
    observations = []
    for step, run in enumerate(runs, 1):
        stored = (results or {}).get(str(step), {})
        tool, outcome = stored.get("tool"), stored.get("status")
        data = stored.get("result", {})
        reason = data.get("reason")
        summary = None
        if outcome == "observed":
            summary = {
                "list_files": "pricing.py listed",
                "read_file": "pricing.py read (see frozen source)",
                "search_code": "source search completed",
                "git_status": "committed snapshot inspected",
                "git_diff": "Git diff was empty"
                if data.get("diff") == ""
                else "committed snapshot inspected",
                "analyze_python": "Original Python syntax valid"
                if data.get("syntax") == "valid"
                else "Original Python syntax invalid",
                "propose_patch": "candidate proposed"
                if data.get("candidate") == "proposed"
                else "candidate rejected",
                "finish": "report received",
            }.get(tool)
        observations.append(
            {
                "step": step,
                "run": run.id,
                "status": str(run.status),
                "tool": tool,
                "outcome": outcome,
                "reason": reason if reason in REASONS else None,
                "result": summary,
            }
        )
    return validate_context(
        {
            "version": 1,
            "snapshot": snapshot.identity,
            "revision": snapshot.revision,
            "source": {PATH: SOURCE},
            "criteria": [CRITERIA],
            "execution_tools": [],
            "local_tools": sorted(TOOL_ARGS),
            "source_job": job,
            "observations": observations,
            "unresolved": ["Functional tests have not run."],
            "architect_plan": None,
        }
    )


def validate_review(value):
    base = {"engineer_run", "sha256", "diff"}
    if not isinstance(value, dict) or (set(value) - {"functional", "previous_review_run"}) not in (
        base | {"local_attempt"}, base | {"local_finish_sha256"},
    ):
        raise ValidationError("Invalid sealed review evidence")
    if "functional" in value:
        checked = value["functional"]
        if (type(checked) is not dict or set(checked) != {"run", "sha256", "status", "passed", "total", "suite_digest"}
                or any(not isinstance(checked[k], str) or not re.fullmatch(rf"[a-f0-9]{{{size}}}", checked[k])
                       for k, size in (("run", 32), ("sha256", 64), ("suite_digest", 64)))
                or type(checked["passed"]) is not int or type(checked["total"]) is not int
                or not 0 <= checked["passed"] <= checked["total"] <= 16 or checked["total"] < 2
                or checked["status"] != ("passed" if checked["passed"] == checked["total"] else "failed")):
            raise ValidationError("Invalid functional evidence")
    if "local_attempt" in value:
        validate_incomplete_attempt(value["local_attempt"])
    for key, length in (
        ("engineer_run", 32),
        ("sha256", 64),
        ("local_finish_sha256", 64),
        ("previous_review_run", 32),
    ):
        if key in {"local_finish_sha256", "previous_review_run"} and key not in value:
            continue
        if not isinstance(value[key], str) or not re.fullmatch(
            rf"[a-f0-9]{{{length}}}", value[key]
        ):
            raise ValidationError("Invalid review evidence identity")
    public_text(value["diff"], limit=600, empty=False)
    return value


def validate_incomplete_attempt(attempt):
    if (
        not isinstance(attempt, dict)
        or set(attempt) != {"outcome", "reason", "conclusion", "steps"}
        or attempt["outcome"] != "incomplete"
        or attempt["conclusion"] != "not_produced"
        or attempt["reason"]
        not in (
            "operator_closed",
            "step_budget_exhausted",
            "candidate_budget_exhausted",
        )
        or not isinstance(attempt["steps"], list)
        or not 1 <= len(attempt["steps"]) <= 6
    ):
        raise ValidationError("Invalid incomplete Local attempt")
    for step in attempt["steps"]:
        if (
            not isinstance(step, dict)
            or set(step) != {"run", "status", "artifacts"}
            or not isinstance(step["run"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", step["run"])
            or step["status"] not in ("succeeded", "failed", "cancelled")
            or not isinstance(step["artifacts"], dict)
            or step["artifacts"].keys() - {"response.txt", "tool-result.json"}
            or any(
                not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha)
                for sha in step["artifacts"].values()
            )
        ):
            raise ValidationError("Invalid Local step evidence")
        if step["status"] == "succeeded" and set(step["artifacts"]) != {
            "response.txt",
            "tool-result.json",
        }:
            raise ValidationError(
                "Successful Run requires sealed response and tool evidence"
            )


def local_subtask(result):
    if (
        result.get("state") != "proposed"
        or result.get("candidate", {}).get("syntax") != "valid"
    ):
        raise ValidationError("Engineer must supply a syntax-valid proposal")
    patch = result["patch"]
    candidate = SOURCE.replace(patch["before"], patch["after"], 1)
    task = "Inspect pricing.py and review the proposed replacement. Report evidence without running code."
    requirements = (
        "Preserve the signature and check 10% of 100. Proposed source: "
        + candidate.rstrip()
    )
    public_text(requirements, limit=160)
    return task, requirements


def wire(value):
    if value["version"] == 4:
        from .project_contract import wire as project_wire

        return project_wire(value)
    if value["version"] == 2:
        return value["text"], "diagnostic"
    body = {"task": value["text"], "context": value["inputs"]["context"]}
    if value["phase"] == "review":
        review = value["inputs"]["review"]
        evidence = {
            key: item for key, item in review.items() if key != "previous_review_run"
        }
        if "functional" in evidence:
            body["context"] = {**body["context"], "unresolved": []}
        instruction = "reviewer"
        if "local_attempt" in review:
            instruction = "mentor_tested" if "functional" in evidence else "mentor"
        return canonical({**body, "review": evidence}), instruction
    return canonical(body), value["role"]


def assess(request, answer):
    """Validate proposals without applying them or executing generated Python."""
    if request.get("version") == 4:
        from .project_contract import assess as project_assess

        return project_assess(request, answer)
    role = request["role"]
    try:
        value = _strict_json(answer.encode())
        if request.get("phase") == "review":
            functional = request["inputs"]["review"].get("functional", {}).get("status", "not_run")
            if "local_attempt" in request["inputs"]["review"]:
                if not isinstance(value, dict) or set(value) != {
                    "summary",
                    "facts",
                    "assumptions",
                    "coaching",
                }:
                    raise ValidationError("Expected structured mentoring review")
                return {
                    "state": "reviewed",
                    "local_outcome": "incomplete",
                    "summary": public_text(value["summary"], limit=700, empty=False),
                    "facts": _array(value["facts"]),
                    "assumptions": []
                    if value["assumptions"] == []
                    else _array(value["assumptions"]),
                    "coaching": _array(value["coaching"]),
                    "functional_tests": functional,
                    "verified_knowledge": False,
                }
            if (
                not isinstance(value, dict)
                or set(value) != {"verdict", "summary", "checks"}
                or value["verdict"] not in ("accepted", "changes_requested")
            ):
                raise ValidationError("Expected structured evidence review")
            return {
                "state": "reviewed",
                "verdict": value["verdict"],
                "summary": public_text(value["summary"], limit=700, empty=False),
                "planned_checks": _array(value["checks"]),
                "functional_tests": functional,
                "verified_knowledge": False,
            }
        if role == "architect":
            return {"state": "proposed", "plan": plan(value)}
        if not isinstance(value, dict) or set(value) != {
            "path",
            "before",
            "after",
            "explanation",
            "checks",
        }:
            raise ValidationError("Expected an Engineer patch candidate")
        explanation = public_text(value["explanation"], limit=700, empty=False)
        checks = _array(value["checks"])
        args = {key: value[key] for key in ("path", "before", "after")}
        if args["path"] != PATH:
            raise ValidationError("Candidate path is outside the public fixture")
        proposal(canonical({"tool": "propose_patch", "args": args}), 2)
        frozen = request["inputs"]["context"]
        snapshot = Snapshot(
            frozen["revision"], frozen["revision"], (PATH,), (), ((PATH, SOURCE),)
        )
        candidate = execute_tool(snapshot, "propose_patch", args, {})
        return {
            "state": candidate["candidate"],
            "candidate": candidate,
            "patch": args,
            "explanation": explanation,
            "planned_checks": checks,
            "functional_tests": "not_run",
            "source_written": False,
            "recipient_roles": ["architect", "local"],
        }
    except (ValueError, RecursionError, ValidationError):
        return {
            "state": "invalid_format",
            "functional_tests": "not_run",
            "source_written": False,
        }
