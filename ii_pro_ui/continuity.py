"""Export a bounded, inert view of Git and the existing Studio database."""

import argparse
import hashlib
import io
import json
import re
import sqlite3
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from ii_pro_core.errors import ValidationError
from ii_pro_core.security import no_links, public_text

from .reader import public, stamp

ROOT = Path(__file__).resolve().parents[1]
FILENAME = "II_PRO_CONTINUE.zip"
STAGE = "M.1"
LIMIT = 128 * 1024
DOCS = (
    "README.md", "docs/ROADMAP.md", "docs/LOCAL_ENGINE_STUDIO.md",
    "docs/STAGE_L5_AUTONOMOUS_REPORT.md", "docs/STAGE_L64C_PREAPPLY_REPAIR.md",
    "docs/STAGE_M1_CONTINUITY.md",
)
TRANSCRIPT = "notes/chat.txt"
INSTRUCTIONS = """# Продолжение работы с II PRO Studio

Прочитай manifest.json, state.json и docs/ROADMAP.md. Сверь HEAD, ветку и
фактическое состояние Core, если доступна рабочая копия. Продолжай существующий
проект с одним Core, одной системой разрешений и действующими ограничениями.
Учитывай результаты испытаний и незавершённые операции. Никогда не повторяй
unknown и не запускай модели или Worker без нового штатного согласования.
Материалы unreviewed и ручные заметки требуют проверки. SHA-256 подтверждает
целостность файлов. Снимок сам по себе не подтверждает истинность утверждений.
Если следующий этап не согласован, обозначь это явно.
"""
PRIVATE = re.compile(
    r"(?im)(?:^\s*(?:cookie|set-cookie|authorization)\s*:|"
    r"(?:refresh[_-]?token|id[_-]?token|client[_-]?secret|api[_-]?key|password)[\"']?\s*[:=]\s*[\S]+|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def git(root, *args):
    return subprocess.run(
        ["git", "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root), *args], capture_output=True,
        check=True, timeout=5,
    ).stdout


def checked_text(data):
    if len(data) > LIMIT:
        raise ValidationError("Continuity text exceeds its quota")
    value = public_text(data.decode("utf-8-sig"), limit=LIMIT, empty=False)
    if PRIVATE.search(value):
        raise ValidationError("Continuity text may contain confidential data")
    # Keep project-relative references usable without disclosing local roots.
    return public(value, limit=LIMIT).encode()


def core_state(root):
    path = root / "core.sqlite3"
    for target in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        no_links(target)
    if not path.is_file():
        return {"available": False}
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        open_runs = [dict(row) for row in conn.execute(
            """SELECT id,status,effect_status,execution_stopped FROM runs
            WHERE status IN ('created','running','unknown') ORDER BY rowid DESC LIMIT 50"""
        )]
        abandoned = {row[0] for row in conn.execute(
            """SELECT json_extract(payload_json, '$.abandoned_run_id') FROM events
            WHERE kind='team.unknown_abandoned' AND json_extract(payload_json,'$.no_retry')=1"""
        )}
        for run in open_runs:
            if not re.fullmatch(r"[0-9a-f]{32}", run["id"]):
                raise ValidationError("Unexpected Run identity")
            if run["status"] == "unknown":
                run.update(no_retry=True, cloud_abandoned=run["id"] in abandoned)
        trials = []
        for row in conn.execute(
            """SELECT json_extract(payload_json,'$.job') AS job,
            json_extract(payload_json,'$.reason') AS outcome, created_at
            FROM events WHERE kind='team.project_auto_end' ORDER BY id DESC LIMIT 5"""
        ):
            item = dict(row)
            if not isinstance(item["job"], str) or not re.fullmatch(r"[0-9a-f]{32}", item["job"]):
                raise ValidationError("Unexpected project session identity")
            if item["outcome"] not in {"completed", "failed", "unknown", "expired", "constraint", "budget"}:
                raise ValidationError("Unexpected project outcome")
            item["created_at"] = datetime.fromisoformat(item["created_at"]).isoformat()
            scope = conn.execute(
                """SELECT json_extract(payload_json,'$.scope.task'),json_extract(payload_json,'$.scope.acceptance')
                FROM events WHERE kind='team.project_draft'
                AND json_extract(payload_json,'$.job')=? ORDER BY id DESC LIMIT 1""", (item["job"],)
            ).fetchone()
            if scope and all(isinstance(field, str) and len(field) <= 800 for field in scope):
                try:
                    item["public_assignment"], item["acceptance"] = [
                        checked_text(field.encode()).decode() for field in scope
                    ]
                except ValidationError:
                    item["assignment_redacted"] = True
            review = conn.execute(
                """SELECT a.sha256,b.content,shadow.sha256 AS shadow_sha,sb.content AS shadow
                FROM runs r JOIN artifact_seals s ON r.id=s.run_id
                JOIN artifacts a ON a.run_id=r.id AND a.relative_path LIKE '%/engineering.json'
                JOIN artifact_blobs b ON a.id=b.artifact_id
                JOIN artifacts shadow ON shadow.run_id=r.id AND shadow.relative_path LIKE '%/shadow-material.json'
                JOIN artifact_blobs sb ON shadow.id=sb.artifact_id
                WHERE r.idempotency_key=? AND r.status='succeeded'""",
                (f"project:{item['job']}:review",),
            ).fetchone()
            if review and sha(review["content"]) == review["sha256"] and sha(review["shadow"]) == review["shadow_sha"]:
                marks = json.loads(review["content"]).get("functional_tests")
                if isinstance(marks, dict) and all(type(marks.get(k)) is int for k in ("passed", "total")):
                    item["functional"] = {k: marks[k] for k in ("passed", "total")}
                    item["evidence_sha256"] = review["sha256"]
                if json.loads(review["shadow"]).get("status") == "unreviewed":
                    item["learning"] = "unreviewed"
            trials.append(item)
        pending = [
            {"job": row["job"], "state": "draft" if not row["expires"] else (
                "expired" if datetime.fromisoformat(row["expires"]) <= datetime.now(UTC) else "consented"
            )}
            for row in conn.execute(
                """SELECT json_extract(d.payload_json,'$.job') AS job,
                (SELECT json_extract(s.payload_json,'$.expires_at') FROM events s
                 WHERE s.kind='team.project_started' AND json_extract(s.payload_json,'$.job')=
                 json_extract(d.payload_json,'$.job') ORDER BY s.id DESC LIMIT 1) AS expires
                FROM events d WHERE d.kind='team.project_draft'
                AND json_type(d.payload_json,'$.scope.autonomous')='object'
                AND NOT EXISTS (SELECT 1 FROM events e
                  WHERE e.kind IN ('team.project_auto_end','team.project_stopped')
                  AND json_extract(e.payload_json,'$.job')=json_extract(d.payload_json,'$.job'))
                ORDER BY d.id DESC LIMIT 10"""
            )
            if isinstance(row["job"], str) and re.fullmatch(r"[0-9a-f]{32}", row["job"])
        ]
        return {"available": True, "recent_runs": open_runs, "recent_trials": trials,
                "unfinished_autonomous_sessions": pending,
                "worker_stop_unconfirmed": conn.execute(
                    "SELECT count(*) FROM worker_jobs WHERE stop_confirmed=0").fetchone()[0]}
    finally:
        conn.close()


def create(state_root, *, root=ROOT, include_transcript=False):
    head = git(root, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").decode("utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", head) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,180}", branch):
        raise ValidationError("Unexpected Git identity")
    created_at = datetime.now(UTC).isoformat(timespec="seconds")
    state = {"stage": STAGE, "previous_completed": "L.6.4c",
             "git": {"head": head, "branch": branch,
                     "dirty": bool(git(root, "status", "--porcelain", "--untracked-files=normal"))},
             "core": core_state(state_root), "next_agreed_stage": None,
             "limits": ["Approvals stay in Core.", "Historical unknown cannot be replayed.",
                        "Size correction was verified on Mock. L.6.4c completed without using it."]}
    files = {"START_HERE.md": (INSTRUCTIONS.encode(), "operator_instructions"),
             "state.json": (encode(state), "git_and_core_projection")}
    for name in DOCS:
        files[name] = (checked_text(git(root, "show", f"{head}:{name}")), "committed_git_document")
    if include_transcript:
        transcript = root / ".local/continuity/chat.txt"
        no_links(transcript)
        before = stamp(transcript)
        with transcript.open("rb") as stream:
            note = checked_text(stream.read(LIMIT + 1))
        if stamp(transcript) != before:
            raise ValidationError("Manual note changed during export")
        if re.search(rb"(?i)\b(?:approval|csrf|cookie|authorization|bearer|session[_ -]?token)\b", note):
            raise ValidationError("Manual note contains authorization-like material")
        files[TRANSCRIPT] = (note, "manual_unverified")
    manifest = {"version": 1, "stage": STAGE, "head": head, "created_at": created_at,
                "files": {name: {"sha256": sha(data), "bytes": len(data), "version": 1, "origin": origin}
                          for name, (data, origin) in files.items()}}
    out = io.BytesIO()
    with ZipFile(out, "w", ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", encode(manifest))
        for name, (data, _) in files.items():
            archive.writestr(name, data)
    package = out.getvalue()
    verify(package)
    return package, manifest


def verify(data):
    required = {"manifest.json", "START_HERE.md", "state.json", *DOCS}
    try:
        if len(data) > LIMIT * (len(required) + 1):
            raise ValueError()
        with ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if (set(names) not in (required, required | {TRANSCRIPT}) or len(names) != len(set(names))
                    or any(info.file_size > LIMIT or stat.S_ISLNK(info.external_attr >> 16)
                           for info in archive.infolist())):
                raise ValueError()
            manifest = json.loads(archive.read("manifest.json"))
            files = manifest["files"]
            if manifest["version"] != 1 or set(files) != set(names) - {"manifest.json"}:
                raise ValueError()
            for name, meta in files.items():
                content = archive.read(name)
                if meta["version"] != 1 or sha(content) != meta["sha256"] or len(content) != meta["bytes"]:
                    raise ValueError()
            state = json.loads(archive.read("state.json"))
            if state["git"]["head"] != manifest["head"] or state["stage"] != manifest["stage"]:
                raise ValueError()
            return manifest
    except (BadZipFile, KeyError, TypeError, ValueError, OSError, RuntimeError, EOFError, AttributeError) as error:
        raise ValidationError("Invalid Continuity package") from error


def main():
    parser = argparse.ArgumentParser(description="Create or verify the II PRO continuation package")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--verify", type=Path)
    modes.add_argument("--state-dir", type=Path)
    parser.add_argument("--directory", type=Path, default=Path.cwd())
    parser.add_argument("--include-transcript", action="store_true")
    args = parser.parse_args()
    if args.verify:
        with args.verify.open("rb") as stream:
            verify(stream.read(LIMIT * (len(DOCS) + 4) + 1))
        print("OK")
    else:
        destination = args.directory / FILENAME
        data, manifest = create(args.state_dir, include_transcript=args.include_transcript)
        with destination.open("xb") as stream:
            stream.write(data)
        print(f"{destination.resolve()} | {manifest['head']} | OK")


if __name__ == "__main__":
    main()
