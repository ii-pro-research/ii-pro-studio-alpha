"""Bounded registration of explicitly selected files from loose Git objects."""

from dataclasses import dataclass, field
from hashlib import sha1
import os
from pathlib import Path
import re
import stat
import zlib

from ii_pro_core.errors import ValidationError
from ii_pro_core.security import digest, no_links, public_text

OID = re.compile(r"[a-f0-9]{40}")
SEGMENT = re.compile(r"[a-z0-9_][a-z0-9_.-]{0,31}")
RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    "config",
    "credentials",
    "secret",
    "secrets",
    "token",
} | {f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(10)}


def source_path(name):
    if not isinstance(name, str) or len(name) > 96:
        raise ValidationError("Invalid source path")
    parts = name.split("/")
    if (
        not 1 <= len(parts) <= 5
        or not name.endswith(".py")
        or any(
            not SEGMENT.fullmatch(part)
            or part.endswith(".")
            or set(re.split(r"[_.-]", part)) & RESERVED
            for part in parts
        )
    ):
        raise ValidationError("Source path is outside the Python allowlist")
    return name


def _read_object(objects, oid, kind, cap):
    if not OID.fullmatch(oid):
        raise ValidationError("Invalid Git object identity")
    path = objects / oid[:2] / oid[2:]
    try:
        no_links(path)
        stamp = path.lstat()
        if not stat.S_ISREG(stamp.st_mode) or stamp.st_nlink != 1:
            raise ValidationError("Git object must be a private regular file")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not os.path.samestat(stamp, opened)
                or opened.st_nlink != 1
                or not stat.S_ISREG(opened.st_mode)
            ):
                raise ValidationError("Git object changed during registration")
            raw = stream.read(cap + 1025)
        no_links(path)
        if len(raw) > cap + 1024:
            raise ValidationError("Compressed Git object exceeds quota")
        decoder = zlib.decompressobj()
        unpacked = decoder.decompress(raw, cap + 65)
        if not decoder.eof or decoder.unused_data or sha1(unpacked).hexdigest() != oid:
            raise ValidationError("Git object size or hash mismatch")
        header, sep, content = unpacked.partition(b"\0")
        if not sep or len(content) > cap or header != f"{kind} {len(content)}".encode():
            raise ValidationError("Unexpected Git object type or size")
        return content
    except (OSError, zlib.error, UnicodeError) as exc:
        raise ValidationError("Only available loose Git objects are supported") from exc


def _read_ref(path):
    try:
        no_links(path)
        stamp = path.lstat()
        if not stat.S_ISREG(stamp.st_mode) or stamp.st_nlink != 1:
            raise ValidationError("Git ref must be a private regular file")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not os.path.samestat(stamp, opened) or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise ValidationError("Git ref must be a stable regular file")
            return stream.read(257).decode("ascii").removesuffix("\n").removesuffix("\r")
    except (OSError, UnicodeError) as exc:
        raise ValidationError("Only local loose Git refs are supported") from exc


def _tree(objects, oid):
    raw = _read_object(objects, oid, "tree", 32768)
    entries = {}
    while raw:
        meta, sep, remaining = raw.partition(b"\0")
        mode, space, name = meta.partition(b" ")
        if not sep or not space or not name or len(remaining) < 20 or name in entries:
            raise ValidationError("Malformed Git tree")
        entries[name] = (mode, remaining[:20].hex())
        raw = remaining[20:]
    return entries


def _files(objects, commit, paths):
    head = _read_object(objects, commit, "commit", 8192).split(b"\n", 1)[0]
    if not re.fullmatch(rb"tree [a-f0-9]{40}", head):
        raise ValidationError("Invalid Git commit")
    root = head[5:].decode("ascii")
    result = []
    for name in paths:
        tree = root
        parts = name.encode("ascii").split(b"/")
        for index, segment in enumerate(parts):
            rows = _tree(objects, tree)
            # Reject Windows case aliases even though Git resolves exact bytes.
            aliases = [key for key in rows if key.lower() == segment]
            if aliases and aliases != [segment]:
                raise ValidationError("Ambiguous Git path")
            item = rows.get(segment)
            if item is None:
                break
            mode, tree = item
            if index < len(parts) - 1:
                if mode != b"40000":
                    raise ValidationError("Git path crosses a non-directory")
            else:
                if mode not in {b"100644", b"100755"}:
                    raise ValidationError(
                        "Only regular tracked source files are allowed"
                    )
                try:
                    text = _read_object(objects, tree, "blob", 4096).decode("utf-8")
                except UnicodeError as exc:
                    raise ValidationError("Only UTF-8 source is accepted") from exc
                result.append((name, public_text(text, limit=4096)))
    return tuple(result)


@dataclass(frozen=True)
class Snapshot:
    base: str
    revision: str
    paths: tuple
    before: tuple
    files: tuple
    root: Path | None = field(default=None, compare=False, repr=False)
    create_paths: tuple = ()

    @classmethod
    def register(cls, root, *, base, revision, paths, create_paths=()):
        if any(
            not isinstance(oid, str) or not OID.fullmatch(oid)
            for oid in (base, revision)
        ):
            raise ValidationError("Use full Git commit IDs")
        if not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= 12:
            raise ValidationError("Allowlist must contain 1 to 12 Python files")
        paths = tuple(sorted({source_path(name) for name in paths}))
        if not isinstance(create_paths, (list, tuple)) or len(create_paths) + len(paths) > 12:
            raise ValidationError("Too many explicitly permitted new source paths")
        create_paths = tuple(sorted({source_path(name) for name in create_paths}))
        all_paths = paths + create_paths
        if set(paths) & set(create_paths) or any(a != b and b.startswith(a + "/") for a in all_paths for b in all_paths):
            raise ValidationError("Registered source paths overlap")
        if not 1 <= len(paths) <= 12:
            raise ValidationError("Allowlist must contain 1 to 12 Python files")
        root = Path(root).absolute()
        objects = root / ".git" / "objects"
        no_links(objects)
        before = _files(objects, base, paths)
        files = _files(objects, revision, paths)
        if set(paths) != set(dict(before)) | set(dict(files)):
            raise ValidationError("Allowlist file is absent from both commits")
        if _files(objects, revision, create_paths):
            raise ValidationError("Permitted new paths must be absent from the registered revision")
        return cls(base, revision, paths, before, files, root, create_paths)

    def confirm_current(self):
        if self.root is None:
            raise ValidationError("A registered Git checkout is required")
        git = self.root / ".git"
        head = _read_ref(git / "HEAD")
        if head.startswith("ref: "):
            ref = head[5:].split("/")
            if ref[:2] != ["refs", "heads"] or len(ref) < 3 or any(not SEGMENT.fullmatch(p) or p.endswith(".") for p in ref):
                raise ValidationError("Unsupported Git branch reference")
            head = _read_ref(git.joinpath(*ref))
        if head != self.revision or self != Snapshot.register(
                self.root, base=self.base, revision=self.revision, paths=self.paths, create_paths=self.create_paths):
            raise ValidationError("The registered Git revision is no longer current")

    @property
    def identity(self):
        return digest(
            {
                "base": self.base,
                "revision": self.revision,
                "paths": self.paths,
                "before": self.before,
                "files": self.files,
                **({"create_paths": self.create_paths} if self.create_paths else {}),
            }
        )
