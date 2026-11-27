from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .errors import CoreBusy, NotFound, OwnershipLost, SchemaError, ValidationError
from .models import CoreState, Event
from .security import canonical, no_links, safe_root


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def new_id() -> str:
    return uuid.uuid4().hex


MIGRATIONS = {
    1: (
        "CREATE TABLE migrations(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)",
        """CREATE TABLE tasks(
            id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
            executor TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN
            ('draft','planned','awaiting_approval','running','verifying','needs_revision','verified','saved','failed','cancelled')),
            version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL)""",
        """CREATE TABLE runs(
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
            attempt INTEGER NOT NULL CHECK(attempt>0), executor TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('created','running','succeeded','failed','unknown','cancelled')),
            idempotency_key TEXT NOT NULL UNIQUE, action_json TEXT NOT NULL, action_hash TEXT NOT NULL,
            title TEXT NOT NULL, description TEXT NOT NULL, error_code TEXT, error_type TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
            UNIQUE(task_id,attempt), UNIQUE(id,task_id))""",
        """CREATE TABLE artifacts(
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
            relative_path TEXT NOT NULL UNIQUE COLLATE NOCASE, sha256 TEXT NOT NULL, size INTEGER NOT NULL CHECK(size>=0),
            created_at TEXT NOT NULL, FOREIGN KEY(run_id,task_id) REFERENCES runs(id,task_id))""",
        """CREATE TABLE approvals(
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT NOT NULL, action_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','consumed','expired')),
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL, decided_at TEXT, consumed_at TEXT,
            FOREIGN KEY(run_id,task_id) REFERENCES runs(id,task_id))""",
        """CREATE TABLE events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT REFERENCES tasks(id),
            run_id TEXT REFERENCES runs(id), kind TEXT NOT NULL,
            payload_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
    ),
    2: (
        "CREATE UNIQUE INDEX one_active_run ON runs(task_id) WHERE status IN ('created','running','unknown')",
        "CREATE INDEX events_by_task ON events(task_id,id)",
        "CREATE INDEX approvals_by_run ON approvals(run_id,status)",
        "CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'Journal is append-only'); END",
        "CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'Journal is append-only'); END",
    ),
}
MIGRATIONS[3] = (
    "ALTER TABLE runs ADD COLUMN effect_status TEXT NOT NULL DEFAULT 'unknown' CHECK(effect_status IN ('not_started','unknown','proven_not_applied','applied','partial'))",
    "ALTER TABLE runs ADD COLUMN execution_stopped INTEGER NOT NULL DEFAULT 0 CHECK(execution_stopped IN (0,1))",
    "ALTER TABLE runs ADD COLUMN reconciliation_json TEXT",
    "UPDATE runs SET effect_status='not_started',execution_stopped=1 WHERE status IN ('created','cancelled')",
    "INSERT INTO events(task_id,run_id,kind,payload_json,created_at) SELECT task_id,id,'run.legacy_effect_unconfirmed','{}',strftime('%Y-%m-%dT%H:%M:%f+00:00','now') FROM runs WHERE status IN ('failed','succeeded','unknown')",
)
MIGRATIONS[4] = (
    """CREATE TABLE owner_sessions(
        generation INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL UNIQUE,
        opened_at TEXT NOT NULL, shutdown_requested_at TEXT, closed_at TEXT)""",
    "ALTER TABLE runs ADD COLUMN owner_generation INTEGER REFERENCES owner_sessions(generation)",
)
MIGRATIONS[5] = (
    "CREATE TABLE school_models(id TEXT PRIMARY KEY,profile_json TEXT NOT NULL,snapshot_json TEXT NOT NULL,created_at TEXT NOT NULL)",
    "CREATE TABLE school_students(id TEXT PRIMARY KEY,name TEXT NOT NULL,model_id TEXT NOT NULL REFERENCES school_models(id),created_at TEXT NOT NULL)",
    """CREATE TABLE school_lessons(id TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>0),
        kind TEXT NOT NULL CHECK(kind IN ('practice','exam')),body_json TEXT NOT NULL,digest TEXT NOT NULL,
        created_at TEXT NOT NULL,PRIMARY KEY(id,revision))""",
    "CREATE TABLE school_sessions(id TEXT PRIMARY KEY,student_id TEXT NOT NULL REFERENCES school_students(id),kind TEXT NOT NULL CHECK(kind IN ('practice','exam')),created_at TEXT NOT NULL)",
    """CREATE TABLE school_enrollments(session_id TEXT NOT NULL REFERENCES school_sessions(id),
        lesson_id TEXT NOT NULL,revision INTEGER NOT NULL,task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
        PRIMARY KEY(session_id,lesson_id),FOREIGN KEY(lesson_id,revision) REFERENCES school_lessons(id,revision))""",
    """CREATE TABLE school_attempts(run_id TEXT PRIMARY KEY REFERENCES runs(id),session_id TEXT NOT NULL,
        lesson_id TEXT NOT NULL,sequence INTEGER NOT NULL CHECK(sequence>0),created_at TEXT NOT NULL,
        UNIQUE(session_id,lesson_id,sequence),FOREIGN KEY(session_id,lesson_id) REFERENCES school_enrollments(session_id,lesson_id))""",
    """CREATE TABLE school_responses(run_id TEXT PRIMARY KEY REFERENCES school_attempts(run_id),
        answer BLOB NOT NULL,sha256 TEXT NOT NULL,receipt_json TEXT NOT NULL,created_at TEXT NOT NULL)""",
    """CREATE TABLE school_assessments(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES school_responses(run_id),
        response_sha256 TEXT NOT NULL,body_json TEXT NOT NULL,created_at TEXT NOT NULL)""",
    """CREATE TABLE school_feedback(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES school_attempts(run_id),
        role TEXT NOT NULL CHECK(role IN ('gpt_architect','gpt_engineer')),origin TEXT NOT NULL CHECK(origin IN ('manual','connected')),
        author TEXT NOT NULL,body TEXT NOT NULL,evidence_ref TEXT,created_at TEXT NOT NULL)""",
    """CREATE TABLE school_knowledge(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES school_responses(run_id),
        status TEXT NOT NULL CHECK(status='unverified'),body TEXT NOT NULL,created_at TEXT NOT NULL)""",
) + tuple(
    f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'School history is immutable'); END"
    for table in ("school_models", "school_students", "school_lessons", "school_sessions", "school_enrollments",
                  "school_attempts", "school_responses", "school_assessments", "school_feedback", "school_knowledge")
    for verb in ("UPDATE", "DELETE")
)
MIGRATIONS[6] = (
    """CREATE TABLE operation_specs(digest TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),
        body_json TEXT NOT NULL,created_at TEXT NOT NULL)""",
    """CREATE TABLE approvals_v6(
        id TEXT PRIMARY KEY,task_id TEXT NOT NULL,run_id TEXT NOT NULL,action_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','consumed','expired','revoked','invalidated')),
        created_at TEXT NOT NULL,expires_at TEXT NOT NULL,decided_at TEXT,consumed_at TEXT,
        spec_digest TEXT REFERENCES operation_specs(digest),revoked_at TEXT,
        FOREIGN KEY(run_id,task_id) REFERENCES runs(id,task_id))""",
    """INSERT INTO events(task_id,run_id,kind,payload_json,created_at)
        SELECT task_id,run_id,'approval.invalidated','{"reason":"legacy_unscoped"}',
        strftime('%Y-%m-%dT%H:%M:%f+00:00','now') FROM approvals WHERE status IN ('pending','approved')""",
    """INSERT INTO approvals_v6(id,task_id,run_id,action_hash,status,created_at,expires_at,decided_at,consumed_at)
        SELECT id,task_id,run_id,action_hash,CASE WHEN status IN ('pending','approved') THEN 'invalidated' ELSE status END,
        created_at,expires_at,decided_at,consumed_at FROM approvals""",
    "DROP TABLE approvals",
    "ALTER TABLE approvals_v6 RENAME TO approvals",
    "CREATE INDEX approvals_by_run ON approvals(run_id,status)",
    "CREATE UNIQUE INDEX one_live_approval ON approvals(run_id) WHERE status IN ('pending','approved')",
    "CREATE TRIGGER operation_specs_no_update BEFORE UPDATE ON operation_specs BEGIN SELECT RAISE(ABORT,'Operation specs are immutable'); END",
    "CREATE TRIGGER operation_specs_no_delete BEFORE DELETE ON operation_specs BEGIN SELECT RAISE(ABORT,'Operation specs are immutable'); END",
    "CREATE TRIGGER approvals_no_delete BEFORE DELETE ON approvals BEGIN SELECT RAISE(ABORT,'Approval history is immutable'); END",
    """CREATE TRIGGER approvals_scope_immutable BEFORE UPDATE OF id,task_id,run_id,action_hash,spec_digest,created_at,expires_at ON approvals
        BEGIN SELECT RAISE(ABORT,'Approval scope is immutable'); END""",
)
MIGRATIONS[7] = (
    "CREATE TABLE artifact_blobs(artifact_id TEXT PRIMARY KEY REFERENCES artifacts(id),content BLOB NOT NULL CHECK(typeof(content)='blob' AND length(content)<=32768))",
    "CREATE TABLE artifact_seals(run_id TEXT PRIMARY KEY REFERENCES runs(id),manifest_json TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL)",
    """CREATE TABLE worker_jobs(job_id TEXT PRIMARY KEY,run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
        epoch INTEGER NOT NULL CHECK(epoch>0),profile_digest TEXT NOT NULL,backend TEXT NOT NULL,
        phase TEXT NOT NULL CHECK(phase IN ('launching','running','unknown','exited')),
        handle_json TEXT,stop_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(stop_confirmed IN (0,1)),
        receipt_json TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
        CHECK(stop_confirmed=0 OR phase='exited'))""",
    "CREATE TRIGGER worker_jobs_no_delete BEFORE DELETE ON worker_jobs BEGIN SELECT RAISE(ABORT,'Worker history is immutable'); END",
    "CREATE TRIGGER worker_jobs_identity BEFORE UPDATE OF job_id,run_id,epoch,profile_digest,backend,created_at ON worker_jobs BEGIN SELECT RAISE(ABORT,'Worker identity is immutable'); END",
    "CREATE TRIGGER worker_stop_monotonic BEFORE UPDATE ON worker_jobs WHEN OLD.stop_confirmed=1 BEGIN SELECT RAISE(ABORT,'Worker stop receipt is final'); END",
    "CREATE TRIGGER sealed_artifact_no_insert BEFORE INSERT ON artifacts WHEN EXISTS(SELECT 1 FROM artifact_seals WHERE run_id=NEW.run_id) BEGIN SELECT RAISE(ABORT,'Manifest is sealed'); END",
    "CREATE TRIGGER sealed_artifact_no_update BEFORE UPDATE ON artifacts WHEN EXISTS(SELECT 1 FROM artifact_seals WHERE run_id=OLD.run_id) BEGIN SELECT RAISE(ABORT,'Manifest is sealed'); END",
    "CREATE TRIGGER sealed_artifact_no_delete BEFORE DELETE ON artifacts WHEN EXISTS(SELECT 1 FROM artifact_seals WHERE run_id=OLD.run_id) BEGIN SELECT RAISE(ABORT,'Manifest is sealed'); END",
) + tuple(
    f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'Worker artifacts are sealed'); END"
    for table in ("artifact_blobs", "artifact_seals") for verb in ("UPDATE", "DELETE")
)
MIGRATIONS[8] = (
    "CREATE TABLE school_v2_policy(id INTEGER PRIMARY KEY CHECK(id=1),blob_bytes INTEGER NOT NULL,payload_bytes INTEGER NOT NULL)",
    "INSERT INTO school_v2_policy VALUES(1,8388608,8388608)",
    "CREATE TABLE school_v2_cycles(id TEXT PRIMARY KEY,student_id TEXT NOT NULL REFERENCES school_students(id),model_json TEXT NOT NULL,seed INTEGER NOT NULL,created_at TEXT NOT NULL)",
    "CREATE TABLE school_v2_suites(id TEXT PRIMARY KEY,phase TEXT NOT NULL CHECK(phase IN ('baseline','practice','control')),body_json TEXT NOT NULL,tests_digest TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL)",
    """CREATE TABLE school_v2_submissions(id TEXT PRIMARY KEY,cycle_id TEXT NOT NULL REFERENCES school_v2_cycles(id),
        suite_id TEXT NOT NULL REFERENCES school_v2_suites(id),sequence INTEGER NOT NULL,
        origin TEXT NOT NULL CHECK(origin IN ('developer_fixture','external_unverified','model_response')),
        source_run_id TEXT REFERENCES school_responses(run_id),answer BLOB NOT NULL,answer_sha256 TEXT NOT NULL,
        code TEXT NOT NULL,code_sha256 TEXT NOT NULL,static_json TEXT NOT NULL,created_at TEXT NOT NULL,
        UNIQUE(cycle_id,suite_id,sequence))""",
    "CREATE TABLE school_v2_packages(id TEXT PRIMARY KEY,submission_id TEXT NOT NULL UNIQUE REFERENCES school_v2_submissions(id),body_json TEXT NOT NULL,digest TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL)",
    "CREATE TABLE school_v2_proposals(cycle_id TEXT NOT NULL REFERENCES school_v2_cycles(id),suite_id TEXT NOT NULL REFERENCES school_v2_suites(id),sequence INTEGER NOT NULL,run_id TEXT NOT NULL UNIQUE REFERENCES school_attempts(run_id),PRIMARY KEY(cycle_id,suite_id,sequence))",
    "CREATE TABLE school_v2_evaluations(package_id TEXT PRIMARY KEY REFERENCES school_v2_packages(id),run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),UNIQUE(package_id,run_id))",
    """CREATE TABLE school_v2_results(id TEXT PRIMARY KEY,package_id TEXT NOT NULL UNIQUE,run_id TEXT NOT NULL,
        artifact_id TEXT NOT NULL REFERENCES artifacts(id),body_json TEXT NOT NULL,created_at TEXT NOT NULL,
        FOREIGN KEY(package_id,run_id) REFERENCES school_v2_evaluations(package_id,run_id))""",
    "CREATE TABLE school_v2_feedback(id TEXT PRIMARY KEY,submission_id TEXT NOT NULL REFERENCES school_v2_submissions(id),body_json TEXT NOT NULL,created_at TEXT NOT NULL)",
    """CREATE TRIGGER artifact_blob_aggregate_quota BEFORE INSERT ON artifact_blobs
        WHEN (SELECT COALESCE(SUM(length(content)),0) FROM artifact_blobs)+length(NEW.content)>(SELECT blob_bytes FROM school_v2_policy WHERE id=1)
        BEGIN SELECT RAISE(ABORT,'Aggregate blob quota exceeded'); END""",
    """CREATE TRIGGER school_v2_answer_quota BEFORE INSERT ON school_v2_submissions
        WHEN (SELECT COALESCE(SUM(length(answer)+length(CAST(code AS BLOB))),0) FROM school_v2_submissions)+length(NEW.answer)+length(CAST(NEW.code AS BLOB))>(SELECT payload_bytes FROM school_v2_policy WHERE id=1)
        BEGIN SELECT RAISE(ABORT,'School answer quota exceeded'); END""",
    """CREATE TRIGGER school_v2_package_quota BEFORE INSERT ON school_v2_packages
        WHEN (SELECT COALESCE(SUM(length(body_json)),0) FROM school_v2_packages)+length(NEW.body_json)>(SELECT payload_bytes FROM school_v2_policy WHERE id=1)
        BEGIN SELECT RAISE(ABORT,'School package quota exceeded'); END""",
    """CREATE TRIGGER school_v2_result_quota BEFORE INSERT ON school_v2_results
        WHEN (SELECT COALESCE(SUM(length(body_json)),0) FROM school_v2_results)+length(NEW.body_json)>(SELECT payload_bytes FROM school_v2_policy WHERE id=1)
        BEGIN SELECT RAISE(ABORT,'School result quota exceeded'); END""",
    """CREATE TRIGGER school_v2_feedback_quota BEFORE INSERT ON school_v2_feedback
        WHEN (SELECT COALESCE(SUM(length(body_json)),0) FROM school_v2_feedback)+length(NEW.body_json)>(SELECT payload_bytes FROM school_v2_policy WHERE id=1)
        BEGIN SELECT RAISE(ABORT,'School feedback quota exceeded'); END""",
) + tuple(
    f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'School v2 history is immutable'); END"
    for table in ("school_v2_policy", "school_v2_cycles", "school_v2_suites", "school_v2_submissions",
                  "school_v2_packages", "school_v2_proposals", "school_v2_evaluations", "school_v2_results", "school_v2_feedback") for verb in ("UPDATE", "DELETE")
)
MIGRATIONS[9] = (
    """CREATE TABLE engineering_memory(
        id TEXT PRIMARY KEY, body_json TEXT NOT NULL CHECK(length(body_json)<=16384),
        sha256 TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE engineering_memory_status(
        seq INTEGER PRIMARY KEY, entry_id TEXT NOT NULL REFERENCES engineering_memory(id),
        status TEXT NOT NULL CHECK(status IN ('imported','tested','verified','revoked','stale')),
        reviewer TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL)""",
    "CREATE INDEX engineering_memory_status_entry ON engineering_memory_status(entry_id, seq)",
    """CREATE TRIGGER engineering_memory_no_replace BEFORE INSERT ON engineering_memory
        WHEN EXISTS(SELECT 1 FROM engineering_memory WHERE id=NEW.id)
        BEGIN SELECT RAISE(ABORT,'Engineering memory entries cannot be replaced'); END""",
    """CREATE TRIGGER engineering_memory_status_no_replace BEFORE INSERT ON engineering_memory_status
        WHEN EXISTS(SELECT 1 FROM engineering_memory_status WHERE seq=NEW.seq)
        BEGIN SELECT RAISE(ABORT,'Engineering memory status cannot be replaced'); END""",
    """CREATE TRIGGER engineering_memory_quota BEFORE INSERT ON engineering_memory
        WHEN (SELECT count(*) FROM engineering_memory)>=512
        BEGIN SELECT RAISE(ABORT,'Engineering memory quota exceeded'); END""",
    """CREATE TRIGGER engineering_memory_status_quota BEFORE INSERT ON engineering_memory_status
        WHEN (SELECT count(*) FROM engineering_memory_status)>=2048
        BEGIN SELECT RAISE(ABORT,'Engineering memory status quota exceeded'); END""",
) + tuple(
    f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'Engineering memory history is immutable'); END"
    for table in ("engineering_memory", "engineering_memory_status") for verb in ("UPDATE", "DELETE")
)
SCHEMA_VERSION = 9


def migration_checksum(statements: tuple[str, ...]) -> str:
    return hashlib.sha256(canonical(statements).encode()).hexdigest()


class ProcessLock:
    """One live owner per state directory, released by the OS even on hard exit."""

    def __init__(self, path: Path):
        no_links(path)
        self.file = path.open("a+b")
        if self.file.seek(0, 2) == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise CoreBusy("This state directory already has a live owner") from None

    def close(self):
        if not self.file.closed:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()


@dataclass
class ActiveExecution:
    thread_id: int
    cancel: threading.Event = field(default_factory=threading.Event)
    cancellable: bool = False


class Store:
    def __init__(self, state_dir: str | Path, *, shutdown_timeout: float = 1.0):
        self.shutdown_timeout = self._timeout(shutdown_timeout)
        self._lifecycle = threading.Condition()
        self._state = CoreState.RUNNING
        self._active: dict[str, ActiveExecution] = {}
        self._external: dict[str, tuple[object, threading.Event]] = {}
        self._cancel_on_shutdown = False
        self._shutdown_requested_at = None
        self.generation = None
        self.owner_id = new_id()
        self.root = safe_root(state_dir)
        self.lock = threading.RLock()
        self.owner = ProcessLock(self.root / "owner.lock")
        self.connection = None
        try:
            self.path = self.root / "core.sqlite3"
            for suffix in ("", "-wal", "-shm"):
                no_links(Path(str(self.path) + suffix))
            self.connection = sqlite3.connect(self.path, isolation_level=None, timeout=5,
                                              check_same_thread=False)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys=ON")
            self._migrate()
            self.schema_version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            with self.transaction() as conn:
                cursor = conn.execute("INSERT INTO owner_sessions(owner_id,opened_at) VALUES(?,?)",
                                      (self.owner_id, utc_now()))
                generation = cursor.lastrowid
            self.generation = generation
        except BaseException:
            self._dispose()
            raise

    @contextmanager
    def transaction(self, *, admit: bool = False):
        with self.lock:
            self._require_connection()
            if admit:
                with self._lifecycle:
                    if self._state != CoreState.RUNNING:
                        raise CoreBusy("Core is stopping; new work is not admitted")
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.assert_owner(self.connection)
                yield self.connection
                self.assert_owner(self.connection)
                self.connection.execute("COMMIT")
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise

    def _require_connection(self):
        if self.connection is None:
            raise CoreBusy("Core is closed")

    def assert_owner(self, conn):
        # None is used only during migration/initial owner acquisition, while
        # the exclusive OS lock is already held. There is no live takeover API.
        if self.generation is not None:
            row = conn.execute("SELECT generation,owner_id FROM owner_sessions ORDER BY generation DESC LIMIT 1").fetchone()
            if row is None or (row[0], row[1]) != (self.generation, self.owner_id):
                raise OwnershipLost("Core ownership generation changed")

    @staticmethod
    def _timeout(value):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not 0 <= value <= threading.TIMEOUT_MAX or not math.isfinite(value)):
            raise ValidationError("Shutdown timeout must be a finite nonnegative number")
        return float(value)

    @property
    def state(self) -> CoreState:
        with self._lifecycle:
            return self._state

    @property
    def active_run_ids(self) -> tuple[str, ...]:
        with self._lifecycle:
            return tuple(dict.fromkeys((*self._active, *self._external)))

    def retain_external_execution(self, run_id: str, cancel: threading.Event):
        """Trusted Supervisor lease; release only after observed job stop."""
        token = object()
        with self._lifecycle:
            if run_id not in self._active or run_id in self._external:
                raise CoreBusy("External lease requires the current invocation")
            self._external[run_id] = (token, cancel)
        def release():
            with self._lifecycle:
                if self._external.get(run_id, (None,))[0] is token:
                    del self._external[run_id]
                    self._lifecycle.notify_all()
        return release

    @contextmanager
    def execution(self, run_id: str):
        # Registration precedes prepare, DB claim and dispatch. close cannot
        # observe an empty set while an accepted invocation is about to start.
        with self._lifecycle:
            if self._state != CoreState.RUNNING:
                raise CoreBusy("Core is stopping; execution is not admitted")
            if run_id in self._active:
                raise CoreBusy("Run already has an active invocation")
            handle = ActiveExecution(threading.get_ident())
            self._active[run_id] = handle
        try:
            yield handle
        finally:
            with self._lifecycle:
                del self._active[run_id]
                self._lifecycle.notify_all()

    def enable_cancellation(self, handle: ActiveExecution, supported: bool):
        with self._lifecycle:
            handle.cancellable = supported
            if supported and self._cancel_on_shutdown:
                handle.cancel.set()

    def _migrate(self):
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if not 0 <= version <= SCHEMA_VERSION:
            raise SchemaError("Database schema version is not supported by this Core")
        if version == 0:
            tables = self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if tables:
                raise SchemaError("Unversioned database is not adopted automatically")
        else:
            try:
                applied = self.connection.execute("SELECT version,checksum FROM migrations ORDER BY version").fetchall()
                if [(r[0], r[1]) for r in applied] != [
                    (n, migration_checksum(MIGRATIONS[n])) for n in range(1, version + 1)
                ]:
                    raise SchemaError("Migration history does not match")
            except sqlite3.DatabaseError as exc:
                raise SchemaError("Invalid migration history") from exc
            self._check_schema(version)
        if 0 < version < SCHEMA_VERSION:
            self.backup(f"pre-v{SCHEMA_VERSION}")
        # No executescript: it implicitly commits, defeating rollback of a migration.
        with self.transaction() as conn:
            for number in range(version + 1, SCHEMA_VERSION + 1):
                for statement in MIGRATIONS[number]:
                    conn.execute(statement)
                conn.execute("INSERT INTO migrations VALUES(?,?,?)",
                             (number, migration_checksum(MIGRATIONS[number]), utc_now()))
                conn.execute(f"PRAGMA user_version={number}")  # number is a trusted migration constant
        self._check_schema(SCHEMA_VERSION)

    def _check_schema(self, version):
        # Extra diagnostic objects are allowed. All published objects must match.
        with sqlite3.connect(":memory:") as expected:
            for number in range(1, version + 1):
                for statement in MIGRATIONS[number]:
                    expected.execute(statement)
            catalog = expected.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        for kind, name, sql in catalog:
            actual = self.connection.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
            if actual is None or (actual[0], actual[1]) != (kind, sql):
                raise SchemaError("Critical database schema differs from the published migrations")
        if self.connection.execute("PRAGMA quick_check").fetchone()[0] != "ok" or self.connection.execute("PRAGMA foreign_key_check").fetchone():
            raise SchemaError("Database integrity check failed")

    def verify_schema(self):
        """Recheck before readiness-path dispatch, not only at application open."""
        with self.lock:
            self._require_connection()
            if self.connection.execute("PRAGMA user_version").fetchone()[0] != self.schema_version:
                raise SchemaError("Database version changed during this owner session")
            try:
                history = self.connection.execute("SELECT version,checksum FROM migrations ORDER BY version").fetchall()
                if [(r[0], r[1]) for r in history] != [(n, migration_checksum(MIGRATIONS[n])) for n in range(1, self.schema_version + 1)]:
                    raise SchemaError("Migration ledger changed during this owner session")
            except sqlite3.DatabaseError as exc:
                raise SchemaError("Migration ledger is unavailable") from exc
            self._check_schema(self.schema_version)
            if self.schema_version >= 8 and tuple(self.connection.execute("SELECT blob_bytes,payload_bytes FROM school_v2_policy WHERE id=1").fetchone() or ()) != (8388608, 8388608):
                raise SchemaError("Published readiness quota policy changed")

    def backup(self, label: str = "checkpoint") -> Path:
        if label not in {"checkpoint", f"pre-v{SCHEMA_VERSION}"}:
            raise ValueError("Unsupported backup label")
        with self.lock:
            self._require_connection()
            folder = safe_root(self.root / "backups")
            destination = folder / f"{label}-{new_id()}.sqlite3"
            with sqlite3.connect(destination) as target:
                self.connection.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise SchemaError("Backup failed integrity check")
            return destination

    def one(self, sql: str, args=(), *, conn=None):
        with self.lock:
            self._require_connection()
            row = (conn or self.connection).execute(sql, args).fetchone()
            if row is None:
                raise NotFound("Record was not found")
            return row

    def all(self, sql: str, args=()):
        with self.lock:
            self._require_connection()
            return self.connection.execute(sql, args).fetchall()

    def event(self, conn, kind: str, *, task_id=None, run_id=None, **payload):
        conn.execute("INSERT INTO events(task_id,run_id,kind,payload_json,created_at) VALUES(?,?,?,?,?)",
                     (task_id, run_id, kind, canonical(payload), utc_now()))

    def events(self, task_id: str | None = None) -> list[Event]:
        rows = self.all("SELECT * FROM events ORDER BY id") if task_id is None else self.all(
            "SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,))
        return [Event(r["id"], r["task_id"], r["run_id"], r["kind"],
                      json.loads(r["payload_json"]), r["created_at"]) for r in rows]

    def close(self, *, timeout: float | None = None, cancel: bool = False):
        """Stop admission and drain; on timeout retain DB and exclusive OS lock.

        No Python thread/process is killed. A caller must retry close after
        CoreBusy. Returning from an invocation is not proof about descendants.
        """
        duration = self.shutdown_timeout if timeout is None else self._timeout(timeout)
        if not isinstance(cancel, bool):
            raise ValidationError("cancel must be boolean")
        deadline = time.monotonic() + duration
        with self._lifecycle:
            if self._state == CoreState.CLOSED:
                return
            self._state = CoreState.STOPPING
            if self._shutdown_requested_at is None:
                self._shutdown_requested_at = utc_now()
            self._cancel_on_shutdown |= cancel
            if self._cancel_on_shutdown:
                for handle in self._active.values():
                    if handle.cancellable:
                        handle.cancel.set()
                for _, signal in self._external.values():
                    signal.set()
            if any(h.thread_id == threading.get_ident() for h in self._active.values()):
                raise CoreBusy("An executor cannot wait for its own invocation to finish")
            while self._active or self._external:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CoreBusy("Executors are still active; ownership is retained")
                self._lifecycle.wait(remaining)
        # Never hold the lifecycle condition while waiting for the DB lock.
        # This also bounds waiting for pre-existing non-execution transactions.
        if not self.lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise CoreBusy("Storage is still busy; ownership is retained")
        try:
            with self._lifecycle:
                if self._state == CoreState.CLOSED:
                    return
            if self.connection is None:
                # Retry resource release if an earlier unlock raised after
                # SQLite was already closed. Admission remains stopped.
                self._dispose()
                return
            busy_timeout = self.connection.execute("PRAGMA busy_timeout").fetchone()[0]
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            self.connection.execute(f"PRAGMA busy_timeout={min(busy_timeout, remaining_ms)}")
            try:
                with self.transaction() as conn:
                    conn.execute("UPDATE owner_sessions SET shutdown_requested_at=?,closed_at=? WHERE generation=? AND owner_id=?",
                                 (self._shutdown_requested_at, utc_now(), self.generation, self.owner_id))
            except OwnershipLost:
                # Never stamp the new owner's session as closed. No active
                # local invocation remains, so local resources can be released.
                pass
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    raise CoreBusy("SQLite is still busy; ownership is retained") from None
                raise
            finally:
                self.connection.execute(f"PRAGMA busy_timeout={busy_timeout}")
            self._dispose()
        finally:
            self.lock.release()

    def _dispose(self):
        # Construction failure or a fully drained close only.
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        self.owner.close()
        with self._lifecycle:
            self._state = CoreState.CLOSED
            self._lifecycle.notify_all()
