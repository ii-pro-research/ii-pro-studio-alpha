"""Closed, in-memory tools. Model proposals never reach an OS command."""

import ast
import json
from difflib import unified_diff

from ii_pro_core.errors import ValidationError
from ii_pro_core.security import public_text
from ii_pro_school.adapters import _strict_json, json_error_kind

from . import table_probe
from .snapshot import source_path


def _string(limit, minimum=1):
    return {"type": "string", "minLength": minimum, "maxLength": limit}


PATH = _string(96)
PLAN = _string(200)
LINE = {"type": "integer", "minimum": 1, "maximum": 4096}
TOOL_ARGS = {
    "list_files": {},
    "read_file": {"path": PATH, "line": LINE},
    "search_code": {"query": _string(60)},
    "git_status": {},
    "git_diff": {"path": PATH},
    "analyze_python": {"path": PATH},
    "propose_patch": {"path": PATH, "before": _string(300), "after": _string(400, 0)},
    "finish": {"report": _string(700), "evidence": {"type": "array"}},
}


def _object(properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def proposal_schema(step, snapshot, observed, *, inline_review=False, table=False):
    """Build the constrained generation schema from the broker's tool contract."""
    inline_review = inline_review or table
    current = sorted(name for name, _ in snapshot.files)
    branches = []
    tools = TOOL_ARGS
    if table and step == 1:
        tools = {"table_check": table_probe.arguments_schema(table if type(table) is str else table_probe.DATASET)}
    elif table:
        tools = {"finish": TOOL_ARGS["finish"]}
    for tool, fields in tools.items():
        if inline_review and not table and tool != "finish":
            continue
        if tool == "finish" and not inline_review and (step == 1 or not observed):
            continue
        properties = dict(fields)
        if "path" in properties:
            paths = list(snapshot.paths) if tool == "git_diff" else current
            if not paths:
                continue
            properties["path"] = {**PATH, "enum": paths}
        if tool == "finish":
            if inline_review:
                properties.update(INLINE_REVIEW_FIELDS)
            properties["evidence"] = {
                **fields["evidence"],
                "minItems": 0 if inline_review and not table else 1,
                "maxItems": len(observed),
                "items": {"type": "integer", **({"enum": list(observed)} if observed else {})},
            }
        envelope = {
            "tool": {"type": "string", "enum": [tool]},
            "args": _object(properties),
        }
        if step == 1:
            envelope["plan"] = PLAN
        branches.append(_object(envelope))
    return {"oneOf": branches}


class ProposalError(ValidationError):
    def __init__(self, code, diagnostic="schema_violation"):
        self.code = code
        self.diagnostic = diagnostic
        super().__init__(code)


INLINE_REVIEW_FIELDS = {
    "defect": _string(240), "comparison": _string(240),
    "prediction": _string(80), "distinguishing_test": _string(240),
}


PROPOSAL_HINTS = {
    "markdown_fence": "Return one bare JSON object, without Markdown.",
    "invalid_json": "Return one valid JSON object with unique keys.",
    "envelope_fields": "Use only tool and args. Include plan only on step 1.",
    "unknown_tool": "Choose a tool from the declared catalog.",
    "argument_fields": "Use exactly the argument keys for the chosen tool.",
    "invalid_line": "read_file.line must be an integer from 1 to 4096.",
    "invalid_values": "Check argument types, nonempty text and evidence step numbers.",
}


def clip(text, limit):
    raw = text.encode()
    return raw[:limit].decode("utf-8", errors="ignore") + (
        "…" if len(raw) > limit else ""
    )


def proposal(text, step, *, inline_review=False, table=False):
    inline_review = inline_review or table
    try:
        value = _strict_json(text.encode())
    except (ValueError, RecursionError) as exc:
        code = "markdown_fence" if text.lstrip().startswith("```") else "invalid_json"
        diagnostic = json_error_kind(exc) if isinstance(exc, json.JSONDecodeError) and code == "invalid_json" else "invalid_json"
        raise ProposalError(code, diagnostic) from None
    fields = {"tool", "args"} | ({"plan"} if step == 1 else set())
    if not isinstance(value, dict) or set(value) != fields:
        raise ProposalError("envelope_fields", "missing_required_field" if isinstance(value, dict) and fields - value.keys() else "schema_violation")
    tool, args = value["tool"], value["args"]
    if table and step == 1:
        if tool != "table_check":
            raise ProposalError("unknown_tool")
        table_probe.validate(args, dataset=table if type(table) is str else table_probe.DATASET)
        public_text(value["plan"], limit=PLAN["maxLength"], empty=False)
        return value
    if table and tool != "finish":
        raise ProposalError("unknown_tool")
    if not isinstance(tool, str) or tool not in TOOL_ARGS:
        raise ProposalError("unknown_tool")
    if inline_review and tool != "finish":
        raise ProposalError("unknown_tool")
    contract = {**TOOL_ARGS[tool], **INLINE_REVIEW_FIELDS} if inline_review else TOOL_ARGS[tool]
    if not isinstance(args, dict) or args.keys() != contract.keys():
        raise ProposalError("argument_fields", "missing_required_field" if isinstance(args, dict) and contract.keys() - args.keys() else "schema_violation")
    for key, schema in contract.items():
        if schema["type"] == "string":
            public_text(
                args[key], limit=schema["maxLength"], empty=schema["minLength"] == 0
            )
    if step == 1:
        public_text(value["plan"], limit=PLAN["maxLength"], empty=False)
        if tool == "finish" and not inline_review:
            raise ValidationError("First step requires investigation")
    if "path" in args:
        source_path(args["path"])
    if tool == "read_file" and (
        type(args["line"]) is not int
        or not LINE["minimum"] <= args["line"] <= LINE["maximum"]
    ):
        raise ProposalError("invalid_line")
    if tool == "propose_patch" and args["before"] == args["after"]:
        raise ValidationError("Patch has no change")
    if tool == "finish":
        refs = args["evidence"]
        if (
            not isinstance(refs, list)
            or not (0 if inline_review and not table else 1) <= len(refs) < step
            or any(type(ref) is not int or not 1 <= ref < step for ref in refs)
            or len(set(refs)) != len(refs)
        ):
            raise ValidationError("Invalid evidence references")
    return value


def _syntax(text):
    try:
        ast.parse(text)
    except (SyntaxError, ValueError, RecursionError) as exc:
        return {"syntax": "invalid", "line": getattr(exc, "lineno", None)}
    return {"syntax": "valid"}


def _diff(path, before, after):
    return "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
        for line in unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="a/" + path,
            tofile="b/" + path,
            n=2,
        )
    )


def execute_tool(snapshot, tool, args, prior, *, table=False):
    if table and tool == "table_check" and not prior:
        return table_probe.evaluate(args, dataset=table if type(table) is str else table_probe.DATASET)
    if table:
        first = prior.get("1", {})
        if (tool != "finish" or (first.get("tool"), first.get("status")) != ("table_check", "observed")
                or args.get("evidence") != [1]):
            raise ValidationError("Tool outside the table probe scope")
    files, before = dict(snapshot.files), dict(snapshot.before)
    if "path" in args:
        path = args["path"]
        allowed = set(snapshot.paths) if tool == "git_diff" else set(files)
        if path not in allowed:
            raise ValidationError("Path is not registered")
    if tool == "list_files":
        return {"files": list(files)}
    if tool == "read_file":
        lines = files[path].splitlines()
        start = args["line"] - 1
        return {
            "path": path,
            "line": start + 1,
            "text": clip("\n".join(lines[start : start + 12]), 430),
        }
    if tool == "search_code":
        matches = [
            {"path": path, "line": number, "text": clip(line, 100)}
            for path, text in files.items()
            for number, line in enumerate(text.splitlines(), 1)
            if args["query"] in line
        ]
        return {"matches": matches[:5], "truncated": len(matches) > 5}
    if tool == "git_status":
        changed = {
            path: "added"
            if path not in before
            else "deleted"
            if path not in files
            else "modified"
            for path in snapshot.paths
            if before.get(path) != files.get(path)
        }
        return {
            "scope": "committed_sources",
            "revision": snapshot.revision,
            "changes": changed,
        }
    if tool == "git_diff":
        return {
            "path": path,
            "diff": clip(_diff(path, before.get(path, ""), files.get(path, "")), 1100),
        }
    if tool == "analyze_python":
        return {"path": path, **_syntax(files[path])}
    if tool == "propose_patch":
        if files[path].count(args["before"]) != 1:
            return {
                "path": path,
                "candidate": "rejected",
                "reason": "anchor_not_unique",
            }
        candidate = files[path].replace(args["before"], args["after"], 1)
        syntax = _syntax(candidate)
        if syntax["syntax"] != "valid":
            return {"path": path, "candidate": "rejected", **syntax}
        return {
            "path": path,
            "candidate": "proposed",
            **syntax,
            "functional_tests": "not_run",
            "diff": _diff(path, files[path], candidate),
        }
    if tool == "finish":
        if any(
            str(ref) not in prior or prior[str(ref)]["status"] != "observed"
            for ref in args["evidence"]
        ):
            raise ValidationError(
                "Evidence does not name an observed prior tool result"
            )
        return {
            "model_report": args["report"],
            **{field: args[field] for field in INLINE_REVIEW_FIELDS if field in args},
            "evidence": args["evidence"],
            "functional_tests": "not_run",
            "source_written": False,
        }
    raise ValidationError("Unsupported tool")
