"""Small closed action vocabulary and scoped artifact I/O; no shell facility."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from .errors import ArtifactMismatch, UnsafePath, ValidationError

MAX_ARTIFACT_BYTES = 1024 * 1024
_SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|"
    r"Bearer\s+\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(?:api[_-]?key|password|access[_-]?token|secret)\s*[=:]\s*\S+)", re.I
)
# The fixed public Worker profile contains a token-shaped substring. Only
# exempt the complete identifier, never a longer credential containing it.
_PUBLIC_WORKER_PROFILE = re.compile(r"(?<![A-Za-z0-9_-])task-manager-filter-wsgi-v1(?![A-Za-z0-9_-])")


def public_text(value: str, *, limit: int = 16000, empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ValidationError("Invalid public text")
    if not empty and not value.strip():
        raise ValidationError("A nonempty value is required")
    if _SECRET.search(_PUBLIC_WORKER_PROFILE.sub(lambda m: "x" * len(m[0]), value)):
        raise ValidationError("Credential-like data is not accepted")
    return value


def canonical(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Invalid JSON data") from exc


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Action:
    # JSON is immutable, so mutating the caller's dict cannot change approval scope.
    kind: str
    parameters_json: str

    @classmethod
    def build(cls, kind: str = "mock.echo", parameters: dict | None = None) -> Action:
        if kind in {"local.recover", "local.fence"}:
            if type(parameters) is not dict or set(parameters) != {"target", "evidence"} or any(
                not isinstance(parameters[key], str) or not re.fullmatch(r"[a-f0-9]{%d}" % length, parameters[key])
                for key, length in (("target", 32), ("evidence", 64))
            ):
                raise ValidationError("Exact Local recovery target and evidence are required")
            return cls(kind, canonical(parameters))
        if kind == "memory.verify":
            if not isinstance(parameters, dict) or set(parameters) != {"entry", "sha256"}:
                raise ValidationError("Invalid knowledge operation")
            if (not isinstance(parameters["entry"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", parameters["entry"])
                    or not isinstance(parameters["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", parameters["sha256"])):
                raise ValidationError("Invalid knowledge identity")
            return cls(kind, canonical(parameters))
        if kind in {"engineering.apply", "engineering.delete", "engineering.rollback"}:
            from ii_pro_local.engineering_executor import validate_change
            validate_change(parameters, kind)
            return cls(kind, canonical(parameters))
        if kind == "local.review":
            if not isinstance(parameters, dict) or set(parameters) not in (
                {"profile_digest", "prompt"}, {"profile_digest", "prompt", "memory"}
            ):
                raise ValidationError("Invalid local review fields")
            profile = parameters["profile_digest"]
            if not isinstance(profile, str) or not re.fullmatch(r"[a-f0-9]{64}", profile):
                raise ValidationError("Invalid local model profile")
            public_text(parameters["prompt"], limit=3000, empty=False)
            if "memory" in parameters:
                memory = parameters["memory"]
                if not isinstance(memory, list) or not 1 <= len(memory) <= 2:
                    raise ValidationError("Invalid local memory context")
                for entry in memory:
                    if not isinstance(entry, dict) or set(entry) != {"id", "sha256", "observation"}:
                        raise ValidationError("Invalid local memory snapshot")
                    public_text(entry["id"], limit=100, empty=False)
                    if not isinstance(entry["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"]):
                        raise ValidationError("Invalid memory digest")
                    public_text(entry["observation"], limit=160, empty=False)
                # Reserve space for the fixed system message, context and output.
                if len(canonical(memory).encode()) > 512 or len(parameters["prompt"].encode()) > 800:
                    raise ValidationError("Local memory prompt budget exceeded")
            return cls(kind, canonical(parameters))
        if kind == "codex.diagnose":
            if not isinstance(parameters, dict) or set(parameters) != {"request_id", "profile_digest"}:
                raise ValidationError("Invalid diagnostic action")
            for key, length in (("request_id", 32), ("profile_digest", 64)):
                if not isinstance(parameters[key], str) or not re.fullmatch(r"[a-f0-9]{%d}" % length, parameters[key]):
                    raise ValidationError("Invalid diagnostic identity")
            return cls(kind, canonical(parameters))
        if kind == "team.turn":
            from ii_pro_team.protocol import validate_request
            validate_request(parameters)
            return cls(kind, canonical(parameters))
        if kind == "worker.fixed":
            from ii_pro_worker.wire import ProtocolError, validate_task
            try:
                validate_task(parameters)
                public_text(canonical(parameters), limit=12000 if parameters["fixture"] in {"python_test", "app_test"} else 4096)
            except ProtocolError as exc:
                raise ValidationError("Invalid fixed Worker task") from exc
            return cls(kind, canonical(parameters))
        if kind == "school.propose":
            keys = {"session_id", "lesson_id", "revision", "lesson_digest", "profile_digest", "prompt"}
            if not isinstance(parameters, dict) or set(parameters) - {"memory"} != keys:
                raise ValidationError("Invalid school action fields")
            if not isinstance(parameters["session_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", parameters["session_id"]):
                raise ValidationError("Invalid school session")
            if not isinstance(parameters["lesson_id"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", parameters["lesson_id"]):
                raise ValidationError("Invalid lesson ID")
            if type(parameters["revision"]) is not int or not 1 <= parameters["revision"] <= 10000:
                raise ValidationError("Invalid lesson revision")
            for key in ("lesson_digest", "profile_digest"):
                if not isinstance(parameters[key], str) or not re.fullmatch(r"[a-f0-9]{64}", parameters[key]):
                    raise ValidationError("Invalid school digest")
            public_text(parameters["prompt"], limit=12000, empty=False)
            if "memory" in parameters:
                cls.build("local.review", {"prompt": "school review", "profile_digest": parameters["profile_digest"],
                                           "memory": parameters["memory"]})
            return cls(kind, canonical(parameters))
        if kind not in {"mock.echo", "mock.protected_echo"}:
            raise ValidationError("Unsupported action")
        params = {"text": "Mock task result"} if parameters is None else parameters
        if not isinstance(params, dict) or set(params) != {"text"}:
            raise ValidationError("Action requires exactly one public text parameter")
        public_text(params["text"])
        return cls(kind, canonical(params))

    @classmethod
    def from_json(cls, text: str) -> Action:
        try:
            value = json.loads(text)
            if not isinstance(value, dict) or set(value) != {"kind", "parameters"}:
                raise ValueError
            return cls.build(value["kind"], value["parameters"])
        except (ValueError, TypeError, KeyError) as exc:
            raise ValidationError("Invalid action record") from exc

    @property
    def json(self) -> str:
        # Revalidate even directly constructed dataclasses / database content.
        try:
            checked = Action.build(self.kind, json.loads(self.parameters_json))
        except (ValueError, TypeError) as exc:
            raise ValidationError("Invalid action parameters") from exc
        return canonical({"kind": checked.kind, "parameters": json.loads(checked.parameters_json)})

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.json.encode("utf-8")).hexdigest()

    @property
    def requires_approval(self) -> bool:
        self.json
        return self.kind in {"mock.protected_echo", "worker.fixed", "team.turn", "local.review",
                             "engineering.apply", "engineering.delete", "engineering.rollback", "memory.verify",
                             "local.recover", "local.fence"}


def no_links(path: Path) -> None:
    """Reject existing symlinks and Windows reparse points, including junctions."""
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise UnsafePath("Symlinks and reparse points are not allowed")


def safe_root(path: str | Path) -> Path:
    root = Path(path).absolute()
    no_links(root)
    root.mkdir(parents=True, exist_ok=True)
    no_links(root)
    return root.resolve(strict=True)


class ArtifactFiles:
    def __init__(self, root: Path):
        self.root = safe_root(root)

    def path(self, relative: str) -> Path:
        if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
            raise UnsafePath("Invalid artifact path")
        win = PureWindowsPath(relative)
        segments = relative.split("/")
        if win.drive or win.root or PurePosixPath(relative).is_absolute():
            raise UnsafePath("Only relative paths are allowed")
        devices = {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)],
                   *[f"LPT{i}" for i in range(1, 10)]}
        for name in segments:
            if (name in {"", ".", ".."} or name.endswith((" ", "."))
                    or any(ord(ch) < 32 or ch in ':<>"|?*' for ch in name)
                    or name.split(".")[0].upper() in devices):
                raise UnsafePath("Invalid artifact path component")
        target = self.root.joinpath(*segments)
        no_links(target)
        if not target.resolve().is_relative_to(self.root):
            raise UnsafePath("Artifact is outside its workspace")
        return target

    def fingerprint(self, relative: str) -> tuple[str, int]:
        target = self.path(relative)
        if not target.is_file():
            raise ArtifactMismatch("Artifact file is missing")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(target, flags), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_BYTES or info.st_nlink != 1:
                raise ArtifactMismatch("Artifact is not a bounded regular file")
            data = stream.read(MAX_ARTIFACT_BYTES + 1)
        if len(data) > MAX_ARTIFACT_BYTES:
            raise ArtifactMismatch("Artifact is too large")
        no_links(target)
        return hashlib.sha256(data).hexdigest(), len(data)

    def write(self, relative: str, content: bytes) -> None:
        if not isinstance(content, bytes) or len(content) > MAX_ARTIFACT_BYTES:
            raise ValidationError("Invalid or oversized artifact")
        # v0.1 accepts UTF-8 public text artifacts only; no credentials or binary dumps.
        try:
            public_text(content.decode("utf-8"), limit=MAX_ARTIFACT_BYTES)
        except UnicodeDecodeError as exc:
            raise ValidationError("Only public UTF-8 artifacts are supported") from exc
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        no_links(target)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(target, flags, 0o600), "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
