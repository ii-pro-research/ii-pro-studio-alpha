"""Operator-managed, append-only engineering evidence and bounded recall."""

import hashlib
import json
import re

from .errors import Conflict, CoreError, ValidationError
from .security import Action, canonical, digest, no_links, public_text
from .storage import utc_now

_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,99}")
_HASH = re.compile(r"[a-f0-9]{64}")
_WORDS = re.compile(r"[^\W_]+")
CATEGORIES = frozenset((
    "programming", "debugging", "tool_choice", "architecture", "functional_testing",
    "security", "learner_errors", "mentoring", "engineering_strategies",
))
CARD_FIELDS = {"category", "task", "mistake", "approach", "language", "tools"}


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value)


def _words(text):
    return set(_WORDS.findall(text.casefold()))


def _read(path, limit):
    no_links(path.absolute())
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValidationError("Memory evidence exceeds import limit")
    return data


class EngineeringMemory:
    """Trusted local API. Neither model nor browser receives write access."""

    def __init__(self, store):
        self.store = store

    def unreviewed_artifacts(self, artifacts):
        """Read-only learning library, absent from verified recall."""
        rows = self.store.all("""
            SELECT a.id, a.run_id, a.relative_path, a.sha256
            FROM artifacts AS a JOIN runs AS r ON a.run_id = r.id
            WHERE r.status = 'succeeded'
              AND ((r.executor = 'team_text' AND a.relative_path LIKE '%/shadow-material.json')
                OR (r.executor = 'engineering_worker' AND a.relative_path LIKE '%/result.json'))
            ORDER BY a.rowid DESC LIMIT 32
        """)
        result = []
        for row in rows:
            try:
                body = json.loads(artifacts.read(row["id"]))
            except (CoreError, ValueError, RecursionError):
                continue
            if row["relative_path"].endswith("/shadow-material.json"):
                if body.get("status") != "unreviewed" or "sealed_review_scope" not in body:
                    continue
                author = "GPT Architect"
                material = {k: body[k] for k in ("engineering_evidence", "team_evidence", "sealed_review_scope")}
                limitation = "Разбор наставника требует проверки. Статические наблюдения и функциональные результаты учитываются отдельно."
            else:
                author = "Worker"
                raw = body.get("result", {})
                if not raw or not body.get("stop_confirmed"):
                    continue
                try:
                    material = json.loads(raw["stdout"])
                except (KeyError, ValueError, RecursionError):
                    continue
                material = {"test_result": material, "exit_code": raw.get("exit_code")}
                limitation = "Доказательство относится к указанному кандидату и набору тестов. Mock не подтверждает функциональность."
                permit = self.store.all("SELECT s.body_json FROM approvals a JOIN operation_specs s ON a.spec_digest=s.digest WHERE a.run_id=? AND a.status='consumed'", (row["run_id"],))
                configuration = json.loads(permit[0][0]).get("executor_configuration", {}) if permit else {}
                material["backend"] = configuration.get("backend", {}).get("backend", "mock")
            result.append({"id": row["id"], "run": row["run_id"], "sha256": row["sha256"],
                           "status": "unreviewed", "author": author, "limitation": limitation,
                           "material": material})
        return result

    def record(self, entry_id, body, *, tested=False):
        if (
            not _matches(_ID, entry_id)
            or not isinstance(body, dict)
            or set(body) - CARD_FIELDS
            != {
                "author",
                "source",
                "revision",
                "title",
                "tags",
                "observation",
                "limitations",
                "evidence",
            }
        ):
            raise ValidationError("Invalid memory entry")
        if body.keys() & CARD_FIELDS:
            if (not CARD_FIELDS <= body.keys() or not isinstance(body["category"], str)
                    or body["category"] not in CATEGORIES):
                raise ValidationError("Incomplete engineering knowledge card")
            for field, limit in (("task", 400), ("mistake", 400), ("approach", 400), ("language", 32)):
                public_text(body[field], limit=limit, empty=False)
            tools = body["tools"]
            if not isinstance(tools, list) or len(tools) > 8:
                raise ValidationError("Invalid knowledge tool scope")
            for tool in tools:
                public_text(tool, limit=40, empty=False)
        fingerprint = digest(body)
        for field, limit in (("author", 100), ("title", 200), ("observation", 400)):
            public_text(body[field], limit=limit, empty=False)
        for field, size, limit in (("tags", 16, 40), ("limitations", 12, 300)):
            values = body[field]
            if not isinstance(values, list) or not 1 <= len(values) <= size:
                raise ValidationError("Invalid memory tags or limitations")
            for value in values:
                public_text(value, limit=limit, empty=False)
        if not _matches(re.compile(r"[a-f0-9]{40}"), body["revision"]):
            raise ValidationError("Git source revision required")
        source = body["source"]
        if not isinstance(source, dict) or set(source) not in (
            {"external"},
            {"task_id", "run_id"},
        ):
            raise ValidationError("Explicit memory source required")
        if "external" in source:
            public_text(source["external"], limit=200, empty=False)
        elif not all(
            _matches(re.compile(r"[a-f0-9]{32}"), value) for value in source.values()
        ):
            raise ValidationError("Invalid task or Run identifier")
        evidence = body["evidence"]
        if not isinstance(evidence, dict) or set(evidence) != {
            "passed",
            "total",
            "sha256",
        }:
            raise ValidationError("Invalid evidence")
        passed, total, hashes = (evidence[k] for k in ("passed", "total", "sha256"))
        if (
            type(passed) is not int
            or type(total) is not int
            or not 0 <= passed <= total <= 10000
            or not isinstance(hashes, list)
            or not 1 <= len(hashes) <= 16
            or not all(_matches(_HASH, item) for item in hashes)
            or (tested and not total)
        ):
            raise ValidationError("Invalid test evidence")
        encoded = public_text(canonical(body), limit=16384)
        with self.store.transaction(admit=True) as conn:
            if "run_id" in source:
                run = conn.execute(
                    "SELECT task_id FROM runs WHERE id=?", (source["run_id"],)
                ).fetchone()
                if not run or run[0] != source["task_id"]:
                    raise ValidationError("Memory task and Run do not match")
                attached = {
                    r[0]
                    for r in conn.execute(
                        "SELECT sha256 FROM artifacts WHERE run_id=?",
                        (source["run_id"],),
                    )
                }
                if not set(hashes) <= attached:
                    raise ValidationError("Evidence does not belong to the source Run")
            prior = conn.execute(
                "SELECT sha256 FROM engineering_memory WHERE id=?", (entry_id,)
            ).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise Conflict("Memory identifier already holds different evidence")
                return entry_id
            conn.execute(
                "INSERT INTO engineering_memory VALUES(?,?,?,?)",
                (entry_id, encoded, fingerprint, utc_now()),
            )
            conn.execute(
                "INSERT INTO engineering_memory_status(entry_id,status,reviewer,reason,created_at) VALUES(?,?,?,?,?)",
                (
                    entry_id,
                    "tested" if tested else "imported",
                    "local operator",
                    "Initial evidence record",
                    utc_now(),
                ),
            )
        return entry_id

    def history(self, entry_id):
        return [
            dict(row)
            for row in self.store.all(
                "SELECT * FROM engineering_memory_status WHERE entry_id=? ORDER BY seq",
                (entry_id,),
            )
        ]

    def get(self, entry_id):
        row = self.store.one("SELECT * FROM engineering_memory WHERE id=?", (entry_id,))
        return {
            **dict(row),
            "body": json.loads(row["body_json"]),
            "history": self.history(entry_id),
        }

    def assess(self, entry_id, status, *, reviewer, reason, approved_run=None):
        public_text(reviewer, limit=100, empty=False)
        public_text(reason, limit=500, empty=False)
        allowed = {
            "imported": ("tested", "revoked", "stale"),
            "tested": ("verified", "revoked", "stale"),
            "verified": ("revoked", "stale"),
        }
        with self.store.transaction(admit=True) as conn:
            current = conn.execute(
                "SELECT status FROM engineering_memory_status WHERE entry_id=? ORDER BY seq DESC LIMIT 1",
                (entry_id,),
            ).fetchone()
            if not current or status not in allowed.get(current[0], ()):
                raise Conflict("Invalid memory assessment transition")
            card = conn.execute("SELECT body_json,sha256 FROM engineering_memory WHERE id=?", (entry_id,)).fetchone()
            if status == "verified" and "category" in json.loads(card[0]):
                run = conn.execute("""SELECT r.action_json FROM runs r JOIN approvals a ON a.run_id=r.id
                    WHERE r.id=? AND r.executor='knowledge_review' AND r.status='running' AND a.status='consumed'""",
                    (approved_run,)).fetchone()
                expected = Action.build("memory.verify", {"entry": entry_id, "sha256": card[1]})
                if not run or run[0] != expected.json:
                    raise Conflict("The exact consumed knowledge Approval is required")
            conn.execute(
                "INSERT INTO engineering_memory_status(entry_id,status,reviewer,reason,created_at) VALUES(?,?,?,?,?)",
                (entry_id, status, reviewer, reason, utc_now()),
            )
            if approved_run:
                self.store.event(conn, "memory.approval_applied", run_id=approved_run, entry=entry_id)

    def recall(self, text, *, language=None, category=None, tools=(), allowed_ids=None):
        """Return a deterministic, byte-bounded set of verified facts."""
        from .memory_transfer import origin_still_verified

        words = _words(public_text(text, limit=3000, empty=False))
        ranked = []
        for row in self.store.all(
            """SELECT id, body_json, sha256 FROM engineering_memory AS m
               WHERE (SELECT status FROM engineering_memory_status
                      WHERE entry_id=m.id ORDER BY seq DESC LIMIT 1)='verified'
               AND NOT EXISTS (SELECT 1 FROM events e JOIN runs r ON r.id=e.run_id
                   WHERE e.kind='memory.approval_applied' AND r.status!='succeeded'
                     AND json_extract(e.payload_json,'$.entry')=m.id)"""
        ):
            body = json.loads(row["body_json"])
            if allowed_ids is not None and row["id"] not in allowed_ids:
                continue
            with self.store.lock:
                if not origin_still_verified(self.store.connection, row["id"]):
                    continue
            if language and body.get("language", language).casefold() != language.casefold():
                continue
            score = len(words & _words(" ".join(body["tags"])))
            if "task" in body:
                score += len(words & _words(body["task"] + " " + body["mistake"]))
                score += 2 * (category is not None and body["category"] == category)
                score += len(set(tools) & set(body["tools"]))
            if score and len(body["observation"]) <= 160:
                ranked.append(
                    (
                        -score,
                        row["id"],
                        {
                            "id": row["id"],
                            "sha256": row["sha256"],
                            "observation": body["observation"],
                        },
                    )
                )
        memory = []
        for _, _, item in sorted(ranked):
            if len(memory) == 2:
                break
            if len(canonical(memory + [item]).encode()) <= 512:
                memory.append(item)
        return memory

    def prepare_review(self, prompt, profile_digest, *, search_text=None, use_memory=True, **scope):
        """Freeze matching verified observations in the existing Approval action."""
        public_text(prompt, limit=3000, empty=False)
        params = {"prompt": prompt, "profile_digest": profile_digest}
        memory = self.recall(prompt if search_text is None else search_text, **scope) if (
            use_memory and len(prompt.encode()) <= 800
        ) else []
        if memory:
            params["memory"] = memory
        return Action.build("local.review", params)


def validate_review_memory(conn, action):
    """Check provenance at admission and dispatch, without regenerating context."""
    from .memory_transfer import origin_still_verified

    for item in json.loads(action.parameters_json).get("memory", []):
        row = conn.execute(
            """SELECT sha256, body_json, (SELECT status FROM engineering_memory_status
               WHERE entry_id=m.id ORDER BY seq DESC LIMIT 1) FROM engineering_memory AS m WHERE id=?""",
            (item["id"],),
        ).fetchone()
        if (
            not row
            or row[0] != item["sha256"]
            or row[2] != "verified"
            or json.loads(row[1])["observation"] != item["observation"]
            or not origin_still_verified(conn, item["id"])
            or conn.execute("""SELECT 1 FROM events e JOIN runs r ON r.id=e.run_id
                WHERE e.kind='memory.approval_applied' AND r.status!='succeeded'
                  AND json_extract(e.payload_json,'$.entry')=?""", (item["id"],)).fetchone()
        ):
            raise Conflict(
                "Frozen engineering memory is no longer verified or does not match"
            )


def import_mentor_repair(memory, directory):
    """Import the documented legacy candidate bundle, without applying its diff."""
    from pathlib import Path
    from ii_pro_school.adapters import _strict_json

    directory = Path(directory)
    try:
        entry = _strict_json(_read(directory / "memory-import-v1.json", 16384))
        report_raw = _read(directory / "verification.json", 16384)
        patch = _read(directory / "candidate.diff", 32768)
        report = _strict_json(report_raw)
    except (OSError, ValueError, RecursionError) as exc:
        raise ValidationError("Invalid mentor bundle") from exc
    if not isinstance(entry, dict) or not isinstance(report, dict):
        raise ValidationError("Mentor entry and report must be JSON objects")
    evidence = entry.get("evidence", {})
    report_sha, patch_sha = (
        hashlib.sha256(data).hexdigest() for data in (report_raw, patch)
    )
    if (
        not isinstance(evidence, dict)
        or entry.get("format") != "ii-pro-memory-import-v1"
        or entry.get("status") != "tested"
        or entry.get("author") != "GPT mentor"
        or report.get("status") != "candidate_only"
        or report.get("solution_author") != entry["author"]
        or report.get("revision") != entry.get("revision")
        or evidence.get("report_sha256") != report_sha
        or evidence.get("diff_sha256") != patch_sha
        or report.get("local_model_attempts_failed") != 2
        or not patch.startswith(b"--- a/ii_pro_core/core.py")
        or b"not isinstance(executor.name, str)" not in patch
        or any(
            report.get(key) != evidence.get(key)
            for key in ("before", "after", "passed", "total")
        )
        or report.get("expected") != report.get("after")
        or not isinstance(report.get("after"), dict)
        or report["passed"] != report["total"]
        or report["total"] != len(report["after"])
    ):
        raise ValidationError("Mentor evidence or attribution mismatch")
    return memory.record(
        entry["id"],
        {
            "author": entry["author"],
            "source": {"external": "manual-training/mentor-repair-20261125-113931"},
            "revision": entry["revision"],
            "title": entry["title"],
            "tags": entry["tags"],
            "observation": (
                "The mentor candidate checked executor.name type before re.fullmatch. "
                f"Targeted checks passed: {report['passed']}/{report['total']}."
            ),
            "limitations": entry["limitations"],
            "evidence": {
                "passed": report["passed"],
                "total": report["total"],
                "sha256": [report_sha, patch_sha],
            },
        },
        tested=True,
    )
