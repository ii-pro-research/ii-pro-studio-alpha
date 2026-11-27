from __future__ import annotations

import re
import hashlib

from .errors import ArtifactMismatch, Conflict, UnsafePath, ValidationError
from .executors import ExecutionResult
from .models import Artifact, artifact_from
from .security import ArtifactFiles, canonical, digest, public_text
from .storage import Store, new_id, utc_now


class ArtifactManager:
    def __init__(self, store: Store):
        self.store = store
        self.files = ArtifactFiles(store.root / "workspace")

    def relative(self, run, name: str) -> str:
        if not all(re.fullmatch(r"[a-f0-9]{32}", run[key]) for key in ("task_id", "id")):
            raise UnsafePath("Invalid run workspace identity")
        self.files.path(name)  # reject absolute/traversal paths before prefixing
        return f"{run['task_id']}/{run['id']}/{name}"

    def write_outputs(self, run, result: ExecutionResult) -> list[str]:
        if not isinstance(result, ExecutionResult) or result.storage != "files" or not 1 <= len(result.artifacts) <= 16:
            raise ValidationError("Executor must return between 1 and 16 artifacts")
        names = [self.relative(run, item.relative_path) for item in result.artifacts]
        if len({name.casefold() for name in names}) != len(names):
            raise Conflict("Executor returned duplicate artifact paths")
        for name, item in zip(names, result.artifacts, strict=True):
            self.files.write(name, item.content)
        return names

    def _register(self, conn, run, relative: str) -> Artifact:
        # Only the exact task/run namespace, even for caller-created files.
        prefix = f"{run['task_id']}/{run['id']}/"
        if not relative.startswith(prefix):
            raise UnsafePath("Artifact does not belong to this run")
        sha, size = self.files.fingerprint(relative)
        row = conn.execute("SELECT * FROM artifacts WHERE relative_path=?", (relative,)).fetchone()
        if row:
            if row["run_id"] != run["id"] or row["sha256"] != sha or row["size"] != size:
                raise ArtifactMismatch("Registered artifact changed")
            return artifact_from(row)
        if conn.execute("SELECT COUNT(*) FROM artifacts WHERE run_id=?", (run["id"],)).fetchone()[0] >= 16:
            raise Conflict("A run may register at most 16 file artifacts")
        artifact_id = new_id()
        conn.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)",
                     (artifact_id, run["task_id"], run["id"], relative, sha, size, utc_now()))
        self.store.event(conn, "artifact.registered", task_id=run["task_id"], run_id=run["id"],
                         artifact_id=artifact_id, sha256=sha, size=size)
        return self.get(artifact_id, conn=conn)

    def register(self, run_id: str, name: str) -> Artifact:
        with self.store.transaction() as conn:
            if self.store.schema_version >= 7 and conn.execute("SELECT 1 FROM artifact_seals WHERE run_id=?", (run_id,)).fetchone():
                raise Conflict("This run has a sealed artifact manifest")
            run = self.store.one("SELECT * FROM runs WHERE id=?", (run_id,), conn=conn)
            task = self.store.one("SELECT status FROM tasks WHERE id=?", (run["task_id"],), conn=conn)
            if task[0] in {"verified", "saved"}:
                raise Conflict("Reviewed artifact evidence cannot be extended")
            if run["status"] != "succeeded":
                raise Conflict("Additional artifacts may only be registered on a successful run")
            return self._register(conn, run, self.relative(run, name))

    def get(self, artifact_id: str, *, conn=None) -> Artifact:
        return artifact_from(self.store.one("SELECT * FROM artifacts WHERE id=?", (artifact_id,), conn=conn))

    def list(self, run_id: str) -> list[Artifact]:
        self.store.one("SELECT id FROM runs WHERE id=?", (run_id,))
        return [artifact_from(row) for row in self.store.all(
            "SELECT * FROM artifacts WHERE run_id=? ORDER BY created_at,id", (run_id,))]

    def validate(self, artifact_id: str) -> bool:
        with self.store.lock:
            artifact = self.get(artifact_id)
            if self.store.schema_version >= 7 and self.store.connection.execute(
                    "SELECT 1 FROM artifact_seals WHERE run_id=?", (artifact.run_id,)).fetchone():
                self.validate_run(artifact.run_id)
                return True
            return self._validate(artifact_id)

    def _validate(self, artifact_id: str) -> bool:
        artifact = self.get(artifact_id)
        if self.store.schema_version >= 7:
            blob = self.store.connection.execute("SELECT content FROM artifact_blobs WHERE artifact_id=?", (artifact_id,)).fetchone()
            if blob is not None:
                if (hashlib.sha256(blob[0]).hexdigest(), len(blob[0])) != (artifact.sha256, artifact.size):
                    raise ArtifactMismatch("Blob digest mismatch")
                return True
            if self.store.connection.execute("SELECT 1 FROM artifact_seals WHERE run_id=?", (artifact.run_id,)).fetchone():
                raise ArtifactMismatch("Sealed blob is missing")
        sha, size = self.files.fingerprint(artifact.relative_path)
        if (sha, size) != (artifact.sha256, artifact.size):
            raise ArtifactMismatch("Artifact checksum or size does not match")
        return True

    def validate_run(self, run_id: str, *, require_any: bool = False, conn=None):
        with self.store.lock:
            self.store._require_connection()
            if self.store.schema_version >= 7:
                seal = (conn or self.store.connection).execute("SELECT * FROM artifact_seals WHERE run_id=?", (run_id,)).fetchone()
                if seal:
                    rows = (conn or self.store.connection).execute("SELECT * FROM artifacts WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
                    manifest = [{k: row[k] for k in ("id", "relative_path", "sha256", "size")} for row in rows]
                    if canonical(manifest) != seal["manifest_json"] or digest(manifest) != seal["digest"]:
                        raise ArtifactMismatch("Sealed manifest changed")
                    if not 1 <= len(rows) <= 8 or sum(r["size"] for r in rows) > 65536:
                        raise ArtifactMismatch("Sealed artifact quota violation")
                    for row in rows:
                        if not (conn or self.store.connection).execute("SELECT 1 FROM artifact_blobs WHERE artifact_id=?", (row["id"],)).fetchone():
                            raise ArtifactMismatch("Sealed blob is missing")
                        self._validate(row["id"])
                    return
            rows = (conn or self.store.connection).execute(
                "SELECT * FROM artifacts WHERE run_id=?", (run_id,)).fetchall()
            if require_any and not rows:
                raise ArtifactMismatch("A successful result must have evidence files")
            for artifact in rows:
                sha, size = self.files.fingerprint(artifact["relative_path"])
                if (sha, size) != (artifact["sha256"], artifact["size"]):
                    raise ArtifactMismatch("Artifact checksum or size does not match")

    def import_blobs(self, conn, run, result):
        if not 1 <= len(result.artifacts) <= 8 or sum(len(a.content) for a in result.artifacts) > 65536:
            raise ValidationError("Blob artifact quota exceeded")
        names = [a.relative_path for a in result.artifacts]
        if len(set(names)) != len(names) or any(not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", n) for n in names):
            raise ValidationError("Only unique lowercase ASCII logical names are accepted")
        if conn.execute("SELECT 1 FROM artifacts WHERE run_id=?", (run["id"],)).fetchone():
            raise Conflict("Blob publication must start with an empty run")
        for item in result.artifacts:
            if not isinstance(item.content, bytes) or len(item.content) > 32768:
                raise ValidationError("Invalid blob")
            public_text(item.content.decode("utf-8"), limit=32768)
            key, now = new_id(), utc_now()
            relative = f"db/{run['task_id']}/{run['id']}/{item.relative_path}"
            sha = hashlib.sha256(item.content).hexdigest()
            conn.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)", (key, run["task_id"], run["id"], relative, sha, len(item.content), now))
            conn.execute("INSERT INTO artifact_blobs VALUES(?,?)", (key, item.content))
            self.store.event(conn, "artifact.registered", task_id=run["task_id"], run_id=run["id"], artifact_id=key, sha256=sha, size=len(item.content), storage="sqlite")
        rows = conn.execute("SELECT * FROM artifacts WHERE run_id=? ORDER BY id", (run["id"],)).fetchall()
        manifest = [{k: row[k] for k in ("id", "relative_path", "sha256", "size")} for row in rows]
        conn.execute("INSERT INTO artifact_seals VALUES(?,?,?,?)", (run["id"], canonical(manifest), digest(manifest), utc_now()))
        self.store.event(conn, "artifacts.sealed", task_id=run["task_id"], run_id=run["id"], digest=digest(manifest))

    def read(self, artifact_id):
        with self.store.lock:
            artifact = self.get(artifact_id)
            self.validate_run(artifact.run_id)
            self.validate(artifact_id)
            if self.store.schema_version >= 7:
                blob = self.store.connection.execute("SELECT content FROM artifact_blobs WHERE artifact_id=?", (artifact_id,)).fetchone()
                if blob is not None:
                    return blob[0]
            return self.files.path(artifact.relative_path).read_bytes()
