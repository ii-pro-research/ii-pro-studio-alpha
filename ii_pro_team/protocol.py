"""Small versioned public Team envelope, independent of provider wire formats."""

import re

from ii_pro_core.errors import ValidationError
from ii_pro_core.security import canonical, public_text
from . import engineering

ROLES = {"plan": "architect", "implement": "engineer", "review": "architect"}
ROLE_INSTRUCTIONS = {
    "architect": "Plan the task, define observable acceptance criteria and review evidence. Return concise technical explanations, never hidden reasoning. Do not approve your own tools.",
    "engineer": "Implement only the agreed task in the assigned workspace. Report actual changes, test evidence and failures. No access outside the workspace or self approval.",
}


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValidationError("Expected a fixed Team identity")
    return value


def validate_request(value):
    if not isinstance(value, dict) or set(value) != {
        "version",
        "job_id",
        "session_id",
        "role",
        "phase",
        "project_id",
        "text",
        "inputs",
    }:
        raise ValidationError("Invalid Team request fields")
    if type(value["version"]) is not int or value["version"] not in {1, 2, 3, 4}:
        raise ValidationError("Unsupported Team protocol")
    identity(value["job_id"])
    identity(value["session_id"])
    if (
        not isinstance(value["phase"], str)
        or value["phase"] not in ROLES
        or value["role"] != ROLES[value["phase"]]
    ):
        raise ValidationError("Invalid role/phase binding")
    if value["version"] == 2:
        inputs = value["inputs"]
        if (
            value["project_id"] != "gpt-text"
            or value["phase"] != "plan"
            or type(inputs) is not dict
            or set(inputs) != {"model", "effort", "profile_digest"}
            or not isinstance(inputs["model"], str)
            or not re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", inputs["model"])
            or inputs["effort"] not in ("low", "sdk_default")
            or not isinstance(inputs["profile_digest"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", inputs["profile_digest"])
        ):
            raise ValidationError("Only a frozen Architect Text plan is supported")
    elif value["version"] == 3:
        inputs = value["inputs"]
        if (
            value["project_id"] != engineering.PROJECT
            or value["text"] != engineering.TASK
            or not isinstance(inputs, dict)
            or set(inputs)
            != (
                {"model", "effort", "profile_digest", "context", "review"}
                if value["phase"] == "review"
                else {"model", "effort", "profile_digest", "context"}
            )
            or inputs["model"] != "gpt-6.2-sol"
            or inputs["effort"] != "sdk_default"
            or not isinstance(inputs["profile_digest"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", inputs["profile_digest"])
        ):
            raise ValidationError("Invalid frozen engineering request")
        context = engineering.validate_context(inputs["context"])
        if value["phase"] == "review":
            engineering.validate_review(inputs["review"])
            if not context["architect_plan"] or not context["source_job"]:
                raise ValidationError("Review requires Architect and Local provenance")
        if value["phase"] == "plan" and context["architect_plan"] is not None:
            raise ValidationError("Architect receives no prior session")
    elif value["version"] == 4:
        from .project_contract import EXTENDED_ENGINEER_DEADLINE, FRAGMENTS, SORT_ENGINEER_DEADLINE, context

        cfg = value["inputs"]
        fields = {"model", "effort", "profile_digest", "context", "scope"}
        if (not isinstance(value["project_id"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,23}", value["project_id"])
                or type(cfg) is not dict or set(cfg) - {"deadline_seconds"} != fields
                or cfg["model"] != "gpt-6.2-sol" or cfg["effort"] != "sdk_default"
                or not all(isinstance(cfg[k], str) and re.fullmatch(r"[a-f0-9]{64}", cfg[k]) for k in ("profile_digest", "scope"))):
            raise ValidationError("Invalid bounded project request")
        ctx = context(cfg["context"], value["phase"])
        if "deadline_seconds" in cfg:
            previous = ctx["previous"]
            abandoned = previous.get("abandoned_proposal")
            sort_experiment = (cfg["deadline_seconds"] == SORT_ENGINEER_DEADLINE
                               and value["project_id"] == "task-manager-sort")
            filter_continuation = (cfg["deadline_seconds"] == EXTENDED_ENGINEER_DEADLINE
                                   and value["project_id"] == "task-manager-filter"
                                   and not previous.get("failed_test") and isinstance(abandoned, str)
                                   and re.fullmatch(r"[a-f0-9]{32}", abandoned))
            if (type(cfg["deadline_seconds"]) is not int or value["phase"] != "implement"
                    or previous.get("edit_protocol") != FRAGMENTS
                    or not (sort_experiment or filter_continuation)):
                raise ValidationError("Extended deadline requires the exact Engineer scope")
    elif value["project_id"] != "studio-lab" or type(value["inputs"]) is not dict:
        raise ValidationError("Only the registered Studio laboratory is available")
    public_text(value["text"], limit=1800, empty=False)
    public_text(canonical(value), limit=11000)
    return value


def event(kind, data):
    if kind not in {
        "session",
        "message",
        "tool",
        "approval_request",
        "usage",
        "status",
    }:
        raise ValidationError("Unknown public agent event")
    if not isinstance(data, dict):
        raise ValidationError("Invalid agent event")
    public_text(canonical(data), limit=8000)
    return {"kind": kind, "data": data}
