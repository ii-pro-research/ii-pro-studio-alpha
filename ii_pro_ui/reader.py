"""Fixed registered sources → private SQLite copies → allowlisted public DTOs."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile

from ii_pro_core.security import canonical, digest, no_links
from ii_pro_core.storage import MIGRATIONS, SCHEMA_VERSION, migration_checksum
from ii_pro_school.functional import inspect_answer
from ii_pro_worker.school_contract import validate_package, validate_result


@dataclass(frozen=True)
class Source:
    key: str
    title: str
    root: Path


def sources(root):
    return (
        Source(
            "school-first",
            "Первое занятие · School v0.1",
            root / ".local/school-v01-first-live",
        ),
        Source(
            "qwen-baseline",
            "Первый экзамен Qwen · School v0.2",
            root / ".local/qwen-first-functional-exam-20261121",
        ),
    ) + tuple(
        Source(key, title, root / folder)
        for key, title, folder in (
            ("school-ui-v03", "School v0.3 · MockExecutor", ".local/school-v03-ui"),
            ("school-ui-v04", "School v0.4 · Mock / Ollama", ".local/school-v04-ui"),
        )
        if (root / folder / "core.sqlite3").is_file()
    )


def public(value, limit=4000):
    """Project data is untrusted. Never send raw paths/credentials/errors."""
    text = str(value or "")[:limit]
    text = re.sub(
        r"(?i)(?:sk-[\w-]{12,}|Bearer\s+\S+|(?:api[_-]?key|password|secret|access_token)\s*[=:]\s*\S+)",
        "[скрыто]",
        text,
    )
    text = re.sub(r'(?:[A-Za-z]:[\\/]|\\\\)[^\s"\'<>]+', "[локальный путь]", text)
    text = re.sub(
        r'(?<![\w:])/(?:home|Users|root|etc|var|mnt|run|opt|private)/[^\s"\'<>]+',
        "[локальный путь]",
        text,
    )
    return text


def sha(data):
    return hashlib.sha256(data).hexdigest()


def stamp(path):
    no_links(path)
    try:
        s = path.stat()
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    except FileNotFoundError:
        return None


@contextmanager
def snapshot(source, cache):
    """NEVER pass an original pathname to SQLite (mode=ro may write SHM).

    Copy DB+WAL between stable file-identity/metadata checks. Concurrent changes
    fail closed. SQLite can build its WAL index only inside this private copy.
    This is a bounded observation, not an online-backup transaction or protection
    against an adversary controlling the host filesystem.
    """
    no_links(source.root)
    paths = [source.root / ("core.sqlite3" + suffix) for suffix in ("", "-wal")]
    if stamp(source.root / "core.sqlite3-journal"):
        raise ValueError("Active rollback journal")
    before = [stamp(p) for p in paths]
    if before[0] is None:
        raise FileNotFoundError("Missing registered database")
    if sum(s[2] for s in before if s) > 64 * 1048576:
        raise ValueError("Observation byte quota exceeded")
    cache.mkdir(parents=True, exist_ok=True)
    no_links(cache)
    with tempfile.TemporaryDirectory(prefix="view-", dir=cache) as folder:
        private = Path(folder)
        assert private.resolve().is_relative_to(cache.resolve())
        no_links(private)
        for path, identity in zip(paths, before, strict=True):
            if identity:
                with path.open("rb") as stream:
                    data = stream.read(64 * 1048576 + 1)
                if len(data) != identity[2]:
                    raise ValueError("Source changed while copying")
                (private / path.name).write_bytes(data)
        if before != [stamp(p) for p in paths] or stamp(
            source.root / "core.sqlite3-journal"
        ):
            raise ValueError("Busy source; do not publish stale data")
        db = sqlite3.connect(
            (private / "core.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=0.5
        )
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            budget = [20000]
            db.set_progress_handler(
                lambda: 1 if budget[0] <= 0 else decrement(budget), 1000
            )
            validate_schema(db)
            yield db
        finally:
            db.close()
        # TemporaryDirectory cleanup is confined to this checked private root.
        no_links(private)


def decrement(budget):
    budget[0] -= 1
    return 0


def validate_schema(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if not 5 <= version <= SCHEMA_VERSION:
        raise ValueError("Unsupported schema")
    ledger = [
        (r[0], r[1])
        for r in db.execute("SELECT version,checksum FROM migrations ORDER BY version")
    ]
    if ledger != [
        (n, migration_checksum(MIGRATIONS[n])) for n in range(1, version + 1)
    ]:
        raise ValueError("Migration identity mismatch")
    # Reuse published schema definitions in RAM, never migrate an original.
    with sqlite3.connect(":memory:") as expected:
        for n in range(1, version + 1):
            for sql in MIGRATIONS[n]:
                expected.execute(sql)
        for kind, name, sql in expected.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ):
            actual = db.execute(
                "SELECT type,sql FROM sqlite_master WHERE name=?", (name,)
            ).fetchone()
            if actual is None or tuple(actual) != (kind, sql):
                raise ValueError("Schema changed")
    if (
        db.execute("PRAGMA quick_check").fetchone()[0] != "ok"
        or db.execute("PRAGMA foreign_key_check").fetchone()
    ):
        raise ValueError("Database integrity failed")


def rows(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args)]


def sealed_stdout(db, run_id, artifact_id, *, basename="stdout.txt"):
    artifacts = rows(
        db, "SELECT * FROM artifacts WHERE run_id=? ORDER BY id", (run_id,)
    )
    seal = db.execute(
        "SELECT * FROM artifact_seals WHERE run_id=?", (run_id,)
    ).fetchone()
    manifest = [
        {k: a[k] for k in ("id", "relative_path", "sha256", "size")} for a in artifacts
    ]
    if (
        not seal
        or digest(manifest) != seal["digest"]
        or canonical(manifest) != seal["manifest_json"]
    ):
        raise ValueError("Unsealed evidence")
    selected = None
    for a in artifacts:
        blob = db.execute(
            "SELECT content FROM artifact_blobs WHERE artifact_id=?", (a["id"],)
        ).fetchone()
        if not blob or len(blob[0]) != a["size"] or sha(blob[0]) != a["sha256"]:
            raise ValueError("Corrupt evidence")
        if a["id"] == artifact_id:
            if not a["relative_path"].endswith("/" + basename):
                raise ValueError("Wrong result artifact")
            selected = json.loads(blob[0])
    if selected is None:
        raise ValueError("Missing result artifact")
    return selected, {
        "verified": True,
        "manifest_sha256": seal["digest"],
        "artifacts": [
            {
                "id": a["id"],
                "name": a["relative_path"].rsplit("/", 1)[-1],
                "sha256": a["sha256"],
                "bytes": a["size"],
            }
            for a in artifacts
        ],
    }


def read_source(source, cache):
    with snapshot(source, cache) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        tasks = rows(
            db,
            "SELECT id,title,executor,status,created_at,updated_at FROM tasks ORDER BY created_at DESC LIMIT 200",
        )
        runs = rows(
            db,
            "SELECT id,task_id,attempt,executor,status,effect_status,error_code,created_at,finished_at FROM runs ORDER BY created_at DESC LIMIT 200",
        )
        provenance = {}
        for record in rows(
            db,
            """SELECT a.run_id,m.profile_json FROM school_attempts a
                JOIN school_sessions s ON s.id=a.session_id JOIN school_students st ON st.id=s.student_id
                JOIN school_models m ON m.id=st.model_id ORDER BY a.created_at DESC LIMIT 200""",
        ):
            profile = json.loads(record["profile_json"])
            real = profile["model"] == "qwen7b-fast:latest"
            mock = profile["model"].startswith("mock")
            provenance[record["run_id"]] = {
                "backend": "Ollama Qwen7B"
                if real
                else "MockExecutor"
                if mock
                else "School · другой профиль",
                "real_inference": real,
                "model": profile["model"],
            }
        for run in runs:
            run.update(provenance.get(run["id"], {}))
            if run["executor"] == "codex_diagnostics":
                run.update(
                    backend="Codex App Server", real_inference=False, diagnostic=True
                )
            if run["executor"] in {"team_architect", "team_engineer", "team_text"}:
                saved = db.execute(
                    """SELECT o.body_json FROM approvals a JOIN operation_specs o ON o.digest=a.spec_digest
                    WHERE a.run_id=? ORDER BY a.rowid DESC LIMIT 1""",
                    (run["id"],),
                ).fetchone()
                mode = (
                    json.loads(saved[0])["executor"]["configuration"]
                    .get("adapter", {})
                    .get("backend")
                    if saved
                    else None
                )
                role = (
                    "GPT Architect"
                    if run["executor"] == "team_architect"
                    else "GPT Engineer"
                )
                run.update(
                    backend=role
                    + (
                        " · Mock Adapter"
                        if mode == "mock"
                        else " · режим не подтверждён"
                    ),
                    real_inference=False,
                )
                if run["executor"] == "team_text":
                    run.update(
                        backend="GPT Text · Mock"
                        if mode == "siwc_text_mock"
                        else "GPT Text · ChatGPT plan",
                        text_generation=True,
                    )
                    if run["status"] == "succeeded":
                        artifact = db.execute(
                            "SELECT id FROM artifacts WHERE run_id=? AND relative_path LIKE '%/result.json'",
                            (run["id"],),
                        ).fetchone()
                        if not artifact:
                            raise ValueError("Missing Text result evidence")
                        result, _ = sealed_stdout(
                            db, run["id"], artifact[0], basename="result.json"
                        )
                        run["real_inference"] = (
                            mode == "siwc_responses"
                            and result.get("real_inference") is True
                            and result.get("backend") == mode
                        )
        for r in tasks + runs:
            r["source"] = source.key
            if "title" in r:
                r["title"] = public(r["title"], 200)
            if "error_code" in r:
                r["error_code"] = public(r["error_code"], 100)
        owner = db.execute(
            "SELECT closed_at,opened_at FROM owner_sessions ORDER BY generation DESC LIMIT 1"
        ).fetchone()
        students = rows(db, "SELECT id,name,model_id FROM school_students LIMIT 100")
        for s in students:
            s.update(source=source.key, name=public(s["name"], 200))
        models = []
        for m in rows(
            db, "SELECT id,profile_json,snapshot_json FROM school_models LIMIT 100"
        ):
            profile = json.loads(m["profile_json"])
            models.append(
                {
                    "id": m["id"],
                    "name": public(profile["model"], 120),
                    "digest": profile["expected_digest"],
                    "options": {
                        k: v
                        for k, v in json.loads(profile["options_json"]).items()
                        if k
                        in {
                            "num_ctx",
                            "num_predict",
                            "num_thread",
                            "seed",
                            "temperature",
                        }
                        and type(v) in (int, float)
                    },
                    "source": source.key,
                }
            )
        lessons = []
        for l in rows(db, "SELECT id,kind,body_json FROM school_lessons LIMIT 100"):
            body = json.loads(l["body_json"])
            lessons.append(
                {
                    "id": l["id"],
                    "kind": l["kind"],
                    "title": public(body["title"], 180),
                    "requirements": public(body["requirements"]),
                    "source": source.key,
                }
            )
        attempts = []
        for a in rows(
            db,
            """SELECT a.run_id,a.lesson_id,a.sequence,r.status,r.created_at,p.answer,p.sha256,p.receipt_json
                FROM school_attempts a JOIN runs r ON r.id=a.run_id LEFT JOIN school_responses p ON p.run_id=a.run_id
                ORDER BY r.created_at DESC LIMIT 200""",
        ):
            receipt = json.loads(a["receipt_json"]) if a["receipt_json"] else {}
            raw = a["answer"] or b""
            if a["answer"] is not None and sha(raw) != a["sha256"]:
                raise ValueError("Response checksum failed")
            statistics = receipt.get("statistics", {})
            count, duration = (
                statistics.get("eval_count"),
                statistics.get("eval_duration"),
            )
            tps = (
                round(count * 1e9 / duration, 2)
                if type(count) is int and type(duration) is int and duration > 0
                else None
            )
            legacy = db.execute(
                "SELECT body_json FROM school_assessments WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
                (a["run_id"],),
            ).fetchone()
            old_grade = json.loads(legacy[0]) if legacy else {}
            attempts.append(
                {
                    "run_id": a["run_id"],
                    **provenance.get(a["run_id"], {}),
                    "lesson_id": public(a["lesson_id"], 150),
                    "sequence": a["sequence"],
                    "status": a["status"],
                    "source": source.key,
                    "created_at": a["created_at"],
                    "raw_answer": public(raw.decode("utf-8"), 16000),
                    "answer_sha256": a["sha256"],
                    "elapsed_seconds": receipt.get("elapsed_seconds"),
                    "tokens": count,
                    "tokens_per_second": tps,
                    "static_legacy": {
                        k: old_grade[k]
                        for k in ("score_awarded", "score_available", "method")
                        if k in old_grade
                    }
                    if legacy
                    else None,
                }
            )
        feedback = [
            dict(
                run_id=f["run_id"],
                role=f["role"],
                origin=f["origin"],
                author=public(f["author"], 200),
                text=public(f["body"]),
            )
            for f in rows(
                db,
                "SELECT run_id,role,origin,author,body FROM school_feedback ORDER BY created_at LIMIT 100",
            )
        ]
        exams, jobs = [], []
        if version >= 7:
            for j in rows(
                db,
                "SELECT run_id,phase,stop_confirmed,receipt_json FROM worker_jobs ORDER BY created_at DESC LIMIT 100",
            ):
                raw_receipt = j.pop("receipt_json")
                receipt = json.loads(raw_receipt) if raw_receipt else {}
                j.update(
                    source=source.key,
                    exit_code=receipt.get("process_exit_code"),
                    reason=public(receipt.get("reason"), 150),
                )
                jobs.append(j)
        if version >= 8:
            for x in rows(
                db,
                """SELECT s.id,s.suite_id,s.code,s.code_sha256,s.answer,s.answer_sha256,s.static_json,s.origin,s.created_at,s.source_run_id,
                    p.id package_id,p.body_json package_json,e.run_id,r.body_json result_json,r.artifact_id
                    FROM school_v2_submissions s LEFT JOIN school_v2_packages p ON p.submission_id=s.id
                    LEFT JOIN school_v2_evaluations e ON e.package_id=p.id LEFT JOIN school_v2_results r ON r.package_id=p.id
                    ORDER BY s.created_at DESC LIMIT 100""",
            ):
                # Saved ordinary lessons are practice checks, not a new exam.
                # Their detailed evidence is served by the functional UI facade.
                if (
                    x["origin"] == "model_response"
                    and x["source_run_id"]
                    and not db.execute(
                        "SELECT 1 FROM school_v2_proposals WHERE run_id=?",
                        (x["source_run_id"],),
                    ).fetchone()
                ):
                    continue
                if (
                    sha(x["answer"]) != x["answer_sha256"]
                    or sha(x["code"].encode()) != x["code_sha256"]
                ):
                    raise ValueError("Submission identity changed")
                _, checked_static = inspect_answer(x["answer"].decode())
                static = json.loads(x["static_json"])
                if static != checked_static:
                    raise ValueError("Static evidence changed")
                functional, evidence = None, None
                run_status = None
                if x["run_id"]:
                    run = db.execute(
                        "SELECT status FROM runs WHERE id=?", (x["run_id"],)
                    ).fetchone()
                    run_status = run[0]
                if x["result_json"]:
                    package = validate_package(json.loads(x["package_json"]))
                    if (
                        package["source_sha256"] != x["code_sha256"]
                        or package["code"] != x["code"]
                    ):
                        raise ValueError("Package identity changed")
                    raw_result, evidence = sealed_stdout(
                        db, x["run_id"], x["artifact_id"]
                    )
                    validated = validate_result(raw_result, package)
                    assessment = json.loads(x["result_json"])
                    job = db.execute(
                        "SELECT stop_confirmed FROM worker_jobs WHERE run_id=?",
                        (x["run_id"],),
                    ).fetchone()
                    if run_status != "succeeded" or not job or not job[0]:
                        raise ValueError("Execution not confirmed")
                    if (
                        assessment["passed"],
                        assessment["total"],
                        assessment["functional_awarded"],
                        assessment["package_digest"],
                        assessment["source_sha256"],
                    ) != (
                        validated["passed"],
                        validated["total"],
                        70 * validated["passed"] // validated["total"],
                        package["digest"],
                        x["code_sha256"],
                    ):
                        raise ValueError("Grade does not match evidence")
                    functional = {
                        k: assessment[k]
                        for k in (
                            "passed",
                            "total",
                            "functional_awarded",
                            "cases",
                            "resources",
                        )
                    }
                    evidence.update(
                        package_sha256=package["digest"],
                        suite_sha256=package["suite_digest"],
                    )
                code = public(x["code"], 4096)
                exams.append(
                    {
                        "id": x["id"],
                        "source": source.key,
                        "suite_id": x["suite_id"],
                        "created_at": x["created_at"],
                        "run_id": x["run_id"],
                        "status": run_status or "not_started",
                        "origin": x["origin"],
                        "code": code,
                        "code_redacted": code != x["code"],
                        "code_sha256": x["code_sha256"],
                        "static_awarded": static["awarded"],
                        "static_available": static["available"],
                        "functional": functional,
                        "evidence": evidence,
                        "total_awarded": static["awarded"]
                        + functional["functional_awarded"]
                        if functional
                        else None,
                    }
                )
            for s in rows(
                db, "SELECT id,phase,body_json FROM school_v2_suites LIMIT 100"
            ):
                body = json.loads(s["body_json"])
                lessons.append(
                    {
                        "id": s["id"],
                        "kind": s["phase"],
                        "title": "sum_positive · "
                        + {
                            "baseline": "Исходный экзамен",
                            "practice": "Практика",
                            "control": "Контроль",
                        }[s["phase"]],
                        "requirements": public(body["public"]["requirements"]),
                        "source": source.key,
                    }
                )
        return {
            "key": source.key,
            "title": source.title,
            "status": "available",
            "schema": version,
            "owner_state": "closed" if owner and owner["closed_at"] else "unconfirmed",
            "tasks": tasks,
            "runs": runs,
            "students": students,
            "models": models,
            "lessons": lessons,
            "attempts": attempts,
            "feedback": feedback,
            "exams": exams,
            "jobs": jobs,
        }
