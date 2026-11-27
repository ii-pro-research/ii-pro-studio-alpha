"""Apply approved changes to a logical working copy, never to the checkout."""

import ast
from difflib import unified_diff
import hashlib
import json
import re

from ii_pro_core.errors import Conflict, ValidationError
from ii_pro_core.executors import ExecutionResult, Executor, ProducedArtifact
from ii_pro_core.security import Action, canonical, digest, public_text
from .snapshot import source_path


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def syntax(text):
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise ValidationError("The candidate is not valid Python") from exc


def render_diff(path, before, after):
    return "".join(unified_diff(
        (before or "").splitlines(keepends=True), (after or "").splitlines(keepends=True),
        fromfile="/dev/null" if before is None else "a/" + path,
        tofile="/dev/null" if after is None else "b/" + path,
    ))


def validate_change(params, kind):
    if type(params) is not dict or set(params) != {"project", "snapshot", "changes", "parent"}:
        raise ValidationError("Invalid working-copy operation")
    if (not isinstance(params["project"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,23}", params["project"])
            or not isinstance(params["snapshot"], str) or not re.fullmatch(r"[a-f0-9]{64}", params["snapshot"])
            or params["parent"] is not None and (
                not isinstance(params["parent"], str) or not re.fullmatch(r"[a-f0-9]{32}", params["parent"]))):
        raise ValidationError("Invalid working-copy identity")
    if (params["parent"] is not None) != (kind == "engineering.rollback"):
        raise ValidationError("Rollback requires its own confirmed parent")
    changes = params["changes"]
    if type(changes) is not list or not 1 <= len(changes) <= 8:
        raise ValidationError("Working-copy change quota exceeded")
    seen = set()
    for item in changes:
        if type(item) is not dict or set(item) != {"path", "before", "before_sha256", "after"}:
            raise ValidationError("Invalid source change")
        path = source_path(item["path"])
        before, after = item["before"], item["after"]
        if (path in seen or before == after or kind != "engineering.rollback" and
                (after is None) != (kind == "engineering.delete")):
            raise ValidationError("Deletion requires a distinct operation")
        seen.add(path)
        for value in (before, after):
            if value is not None:
                public_text(value, limit=4096)
        if item["before_sha256"] != (sha(before) if before is not None else None):
            raise ValidationError("Original source hash differs")
        if after is not None:
            syntax(after)
    public_text(canonical(params), limit=14000)
    return params


def change(path, before, after):
    return {"path": path, "before": before, "before_sha256": sha(before) if before is not None else None,
            "after": after}


def artifact_json(core, run_id, name):
    if not core.store.all("SELECT 1 FROM artifact_seals WHERE run_id=?", (run_id,)):
        raise Conflict("SQLite artifact seal is required")
    core.artifacts.validate_run(run_id, require_any=True)
    found = [a for a in core.artifacts.list(run_id) if a.relative_path.rsplit("/", 1)[-1] == name]
    if len(found) != 1:
        raise Conflict("Required sealed evidence is missing")
    return json.loads(core.artifacts.read(found[0].id))


class EngineeringExecutor(Executor):
    name = "engineering_copy"
    revision = "engineering-copy-v1"

    def __init__(self, projects):
        self.projects = dict(projects)
        self.core = None

    def approval_configuration(self):
        return {"profile": "sealed-python-working-copy-v1", "host_writes": False,
                "registry_digest": digest({key: [str(snapshot.root), snapshot.identity]
                                           for key, (_, snapshot) in self.projects.items()})}

    def _copy(self, request):
        if request.action.kind not in {"engineering.apply", "engineering.delete", "engineering.rollback"}:
            raise ValidationError("Unsupported working-copy action")
        params = validate_change(json.loads(request.action.parameters_json), request.action.kind)
        if params["project"] not in self.projects:
            raise ValidationError("Project is not registered")
        snapshot = self.projects[params["project"]][1]
        if snapshot.identity != params["snapshot"]:
            raise Conflict("The registered snapshot changed")
        snapshot.confirm_current()
        original = dict(snapshot.files)
        if request.action.kind == "engineering.rollback":
            parent = self.core.runs.get(params["parent"])
            if (parent.status != "succeeded" or parent.executor != self.name
                    or Action.from_json(parent.action_json).kind not in {"engineering.apply", "engineering.delete"}):
                raise Conflict("A confirmed application is required")
            saved = artifact_json(self.core, parent.id, "working-copy.json")
            backup = artifact_json(self.core, parent.id, "backup.json")
            if saved["snapshot"] != snapshot.identity or backup["files"] != original or backup["sha256"] != hashes(original):
                raise Conflict("Rollback proof differs from the registered baseline")
            current = saved["files"].copy()
        else:
            current = original.copy()
        initial = current.copy()
        for item in params["changes"]:
            path = item["path"]
            if path not in snapshot.paths + snapshot.create_paths:
                raise ValidationError("File is outside the registered project scope")
            if current.get(path) != item["before"]:
                raise Conflict("The exact original content changed")
            if item["after"] is None:
                del current[path]
            else:
                current[path] = item["after"]
        if params["parent"] is not None and current != original:
            raise Conflict("Rollback must recover the exact baseline")
        return snapshot, initial, current

    def prepare(self, request):
        self._outputs(request)  # admission checks the full serialized quota before execution
        self.core.store.verify_schema()
        if self.core.store.one("SELECT COALESCE(SUM(length(content)),0) FROM artifact_blobs")[0] + 65536 > self.core.store.one(
            "SELECT blob_bytes FROM school_v2_policy WHERE id=1"
        )[0]:
            raise Conflict("Insufficient artifact storage quota")

    def _outputs(self, request):
        snapshot, initial, current = self._copy(request)
        backup = {"snapshot": snapshot.identity, "files": initial, "sha256": hashes(initial)}
        result = {"snapshot": snapshot.identity, "revision": snapshot.revision,
                  "files": current, "sha256": hashes(current)}
        outputs = tuple(ProducedArtifact(name, canonical(body).encode()) for name, body in (
            ("backup.json", backup), ("working-copy.json", result)
        ))
        if any(len(item.content) > 32768 for item in outputs):
            raise ValidationError("The working copy exceeds the artifact budget")
        return outputs

    def execute(self, request):
        return ExecutionResult(self._outputs(request), storage="sqlite")


def hashes(files):
    return {path: sha(code) for path, code in files.items()}
