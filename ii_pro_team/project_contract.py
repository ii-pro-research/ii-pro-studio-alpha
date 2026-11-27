"""Bounded project handoffs. Model output is data for Core and the copy executor."""

import ast
import json
import re
from pathlib import Path

from ii_pro_core.errors import ValidationError
from ii_pro_core.security import canonical, public_text
from ii_pro_local.engineering_executor import change, sha, syntax, validate_change
from ii_pro_local.snapshot import source_path
from ii_pro_school.adapters import _strict_json
from ii_pro_worker.app_contract import SORT_PROFILE, app_package

_OUTPUT = json.loads(Path(__file__).with_name("devkit_bridge").joinpath("project-output.json").read_bytes())
LIMITS = _OUTPUT["limits"]
INSTRUCTIONS = {role: re.sub(r"@(\w+)@", lambda m: str(LIMITS[m[1]]), template)
                for role, template in _OUTPUT["templates"].items()}
WIRE_ROLES = {"plan": "project_architect", "implement": "project_engineer", "review": "project_reviewer"}
FRAGMENTS = "fragments-v1"
EXTENDED_ENGINEER_DEADLINE = 120
SORT_ENGINEER_DEADLINE = 180
SORT_ENGINEER_IDLE = 90
CONTEXT_BYTES = 6100
SIZE_REJECTION = "candidate_exceeds_size"


def candidate_size(source):
    return {"characters": len(source), "bytes": len(source.encode()),
            "max_characters": LIMITS["file_chars"], "max_bytes": LIMITS["total_bytes"]}


def _items(value):
    if type(value) is not list or not 1 <= len(value) <= LIMITS["list_items"]:
        raise ValidationError("Expected a short list of public explanations")
    return [public_text(item, limit=LIMITS["item_chars"], empty=False) for item in value]


def context(value, phase):
    if type(value) is not dict or set(value) != {"snapshot", "source", "allowed", "acceptance", "previous"}:
        raise ValidationError("Invalid project context")
    if not isinstance(value["snapshot"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["snapshot"]):
        raise ValidationError("Frozen snapshot required")
    files, allowed = value["source"], value["allowed"]
    if (type(files) is not dict or type(allowed) is not list or not 1 <= len(allowed) <= 4
            or not all(isinstance(name, str) for name in allowed) or len(set(allowed)) != len(allowed)
            or not files.keys() <= set(allowed)):
        raise ValidationError("Invalid approved Python file list")
    for name in allowed:
        source_path(name)
    previous = value["previous"]
    rejected = previous.get("size_repair") if isinstance(previous, dict) else None
    for contents in files.values():
        public_text(contents, limit=CONTEXT_BYTES if rejected else LIMITS["file_chars"])
    if rejected is not None and (
            phase != "implement" or allowed != ["app.py"] or set(files) != {"app.py"}
            or type(rejected) is not dict or set(rejected) != {"run", "reason", "sha256", "size"}
            or not isinstance(rejected["run"], str) or not re.fullmatch(r"[a-f0-9]{32}", rejected["run"])
            or rejected["reason"] != SIZE_REJECTION or previous.get("edit_protocol") != FRAGMENTS
            or "failed_test" in previous or rejected["sha256"] != sha(files["app.py"])
            or rejected["size"] != candidate_size(files["app.py"])
            or (len(files["app.py"]) <= LIMITS["file_chars"]
                and len(files["app.py"].encode()) <= LIMITS["total_bytes"])):
        raise ValidationError("Invalid size correction evidence")
    public_text(value["acceptance"], limit=800, empty=False)
    if type(previous) is not dict or (phase != "plan" and not previous):
        raise ValidationError("Invalid phase evidence")
    if phase == "plan" and previous and (
            set(previous) != {"rejected_plan"} or not isinstance(previous["rejected_plan"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", previous["rejected_plan"])):
        raise ValidationError("Invalid phase evidence")
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":")) if rejected else canonical(value)
    if len(serialized.encode()) > CONTEXT_BYTES:
        raise ValidationError("Project context is too large")
    return value


def wire(value):
    phase = value["phase"]
    ctx = context(value["inputs"]["context"], phase)
    if phase == "plan":
        ctx = {**ctx, "previous": {}}
    if phase == "implement" and ctx["previous"].get("edit_protocol") == FRAGMENTS:
        budgets = {path: {"sha256": sha(code), "chars_remaining": LIMITS["file_chars"] - len(code),
                          "bytes_remaining": LIMITS["total_bytes"] - len(code.encode())}
                   for path, code in ctx["source"].items()}
        packet = {"task": value["text"], "context": ctx, "source_budgets": budgets}
        prompt = (json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
                  if ctx["previous"].get("size_repair") else canonical(packet))
        return prompt, "project_engineer_fragments"
    return canonical({"task": value["text"], "context": ctx}), WIRE_ROLES[phase]


def _fragment_changes(edits, ctx):
    if type(edits) is not list or not 1 <= len(edits) <= LIMITS["fragments"]:
        raise ValidationError("Fragment count outside budget")
    patches = {}
    for edit in edits:
        if type(edit) is not dict or set(edit) != {"path", "before_sha256", "before", "after"}:
            raise ValidationError("Invalid fragment")
        path = source_path(edit["path"])
        if path not in ctx["allowed"] or path not in ctx["source"]:
            raise ValidationError("Fragment outside the frozen file list")
        source = ctx["source"][path]
        if edit["before_sha256"] != sha(source):
            raise ValidationError("Fragment source hash differs")
        before = public_text(edit["before"], limit=LIMITS["file_chars"], empty=False)
        after = public_text(edit["after"], limit=LIMITS["file_chars"])
        start = source.find(before)
        if start < 0 or source.find(before, start + 1) >= 0 or before == after:
            raise ValidationError("Fragment must match at exactly one position and change the source")
        patches.setdefault(path, []).append((start, start + len(before), after))
    changes = []
    for path, spans in sorted(patches.items()):
        source = ctx["source"][path]
        cursor = 0
        pieces = []
        for start, end, replacement in sorted(spans):
            if start < cursor:
                raise ValidationError("Fragments overlap in the frozen source")
            pieces.extend((source[cursor:start], replacement))
            cursor = end
        pieces.append(source[cursor:])
        changes.append(change(path, source, "".join(pieces)))
    return changes


def assess(request, answer):
    try:
        data = _strict_json(answer.encode())
        phase = request["phase"]
        if type(data) is not dict:
            raise ValidationError("Structured object required")
        if phase == "plan" and set(data) == {"plan", "checks"}:
            return {"state": "proposed", "plan": {key: _items(data[key]) for key in ("plan", "checks")}}
        if phase == "implement":
            ctx = request["inputs"]["context"]
            edit_protocol = ctx.get("previous", {}).get("edit_protocol")
            correcting_size = bool(ctx.get("previous", {}).get("size_repair"))
            if edit_protocol == FRAGMENTS and set(data) == {"edits", "explanation"}:
                changes = _fragment_changes(data["edits"], ctx)
            elif edit_protocol is None and set(data) == {"changes", "explanation"}:
                if (type(data["changes"]) is not list or not 1 <= len(data["changes"]) <= LIMITS["changes"]
                        or any(type(item) is not dict or set(item) != {"path", "after"}
                               or item["path"] not in ctx["allowed"] for item in data["changes"])):
                    raise ValidationError("Change outside project scope")
                changes = [change(i["path"], ctx["source"].get(i["path"]),
                                  public_text(i["after"], limit=LIMITS["file_chars"])) for i in data["changes"]]
            else:
                raise ValidationError("Unexpected Engineer response format")
            explanation = public_text(data["explanation"], limit=LIMITS["explanation_chars"], empty=False)
            if any(len(item["after"]) > LIMITS["file_chars"] for item in changes) or (
                    sum(len(item["after"].encode()) for item in changes) > LIMITS["total_bytes"]):
                if (request["project_id"] == "task-manager-sort" and edit_protocol == FRAGMENTS
                        and not correcting_size and not ctx["previous"].get("failed_test")
                        and [item["path"] for item in changes] == ["app.py"]):
                    candidate = public_text(changes[0]["after"], limit=CONTEXT_BYTES, empty=False)
                    tree = syntax(candidate)
                    if len(candidate.encode()) <= CONTEXT_BYTES and any(
                            isinstance(node, ast.FunctionDef) and node.name == "create_app" for node in tree.body):
                        return {"state": "rejected", "reason": SIZE_REJECTION, "candidate": candidate,
                                "sha256": sha(candidate), "size": candidate_size(candidate)}
                raise ValidationError("Candidate exceeds project budget")
            # A size repair is anchored to the rejected code; the copy operation
            # will apply the resulting source against the frozen original.
            if correcting_size:
                app_package(changes[0]["after"], SORT_PROFILE)
            else:
                validate_change({"project": request["project_id"], "snapshot": ctx["snapshot"],
                                 "parent": None, "changes": changes}, "engineering.apply")
            return {"state": "proposed", "changes": changes,
                    "explanation": explanation}
        if phase == "review" and set(data) == {"summary", "coaching"}:
            return {"state": "reviewed", "summary": public_text(data["summary"], limit=LIMITS["summary_chars"], empty=False),
                    "coaching": _items(data["coaching"]),
                    "functional_tests": request["inputs"]["context"]["previous"]["functional_tests"]}
        raise ValidationError("Unexpected project response")
    except (ValidationError, ValueError, TypeError, KeyError, RecursionError):
        return {"state": "rejected", "reason": "invalid_project_proposal"}
