"""Operator-only knowledge transfer with a live, trusted SQLite origin."""

import argparse
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from .errors import Conflict, CoreError, ValidationError
from .security import Action, canonical, digest, no_links


@contextmanager
def origin(path):
    path = Path(path).absolute()
    for name in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        no_links(name)
    if not path.is_file():
        raise Conflict("Knowledge origin is unavailable")
    stat = path.stat()
    identity = [stat.st_dev, stat.st_ino]
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        current = path.stat()
        if not stat.st_ino or [current.st_dev, current.st_ino] != identity:
            raise Conflict("Knowledge origin identity changed")
        yield db, identity
    finally:
        db.close()


def source_history(db, entry):
    return [dict(row) for row in db.execute(
        """SELECT status,reviewer,reason,created_at FROM engineering_memory_status
           WHERE entry_id=? ORDER BY seq""", (entry,))]


def _verified_bundle(db, entry):
    row = db.execute("SELECT * FROM engineering_memory WHERE id=?", (entry,)).fetchone()
    history = source_history(db, entry)
    if not row or not history or history[-1]["status"] != "verified":
        raise Conflict("Only verified knowledge can be transferred")
    body = json.loads(row["body_json"])
    if digest(body) != row["sha256"] or "category" not in body:
        raise Conflict("A structured, approved source card is required")
    expected = Action.build("memory.verify", {"entry": entry, "sha256": row["sha256"]})
    review = db.execute("""SELECT r.id,r.action_hash,a.spec_digest,s.body_json FROM events e
        JOIN runs r ON r.id=e.run_id JOIN approvals a ON a.run_id=r.id
        JOIN operation_specs s ON s.digest=a.spec_digest
        WHERE e.kind='memory.approval_applied' AND json_extract(e.payload_json,'$.entry')=?
        AND r.executor='knowledge_review' AND r.status='succeeded' AND r.execution_stopped=1
        AND r.action_json=? AND a.status='consumed'""", (entry, expected.json)).fetchall()
    if len(review) != 1:
        raise Conflict("Completed operator approval is required at the origin")
    decided = review[0]
    spec = json.loads(decided["body_json"])
    if (digest(spec) != decided["spec_digest"] or spec["run_id"] != decided["id"]
            or spec["action_hash"] != expected.hash or decided["action_hash"] != expected.hash):
        raise Conflict("Source review identity differs")
    source = body["source"]
    run = db.execute("SELECT * FROM runs WHERE id=?", (source.get("run_id"),)).fetchone()
    if (not run or run["task_id"] != source.get("task_id") or run["status"] != "succeeded"
            or not run["execution_stopped"] or run["executor"] not in {"worker", "engineering_worker"}):
        raise Conflict("Stopped functional source required")
    approved = db.execute("""SELECT s.body_json FROM approvals a JOIN operation_specs s ON s.digest=a.spec_digest
                             WHERE a.run_id=? AND a.status='consumed'""", (run["id"],)).fetchall()
    if len(approved) != 1 or json.loads(approved[0][0])["executor"]["configuration"]["backend"]["backend"] != "docker-fixed":
        raise Conflict("Functional Docker approval is required at the origin")
    seal = db.execute("SELECT * FROM artifact_seals WHERE run_id=?", (run["id"],)).fetchone()
    artifacts = db.execute("""SELECT a.id,a.relative_path,a.sha256,a.size,b.content FROM artifacts a
        JOIN artifact_blobs b ON b.artifact_id=a.id WHERE a.run_id=? ORDER BY a.id""", (run["id"],)).fetchall()
    manifest = [{k: row[k] for k in ("id", "relative_path", "sha256", "size")} for row in artifacts]
    if (not seal or canonical(manifest) != seal["manifest_json"] or digest(manifest) != seal["digest"]
            or not set(body["evidence"]["sha256"]) <= {a["sha256"] for a in artifacts}
            or any(len(a["content"]) != a["size"] or sha256(a["content"]).hexdigest() != a["sha256"] for a in artifacts)):
        raise Conflict("Functional evidence seal differs")
    receipt = next((a for a in artifacts if a["relative_path"].endswith("/result.json")), None)
    if not receipt or receipt["sha256"] not in body["evidence"]["sha256"]:
        raise Conflict("Functional receipt is missing")
    value = json.loads(receipt["content"])
    from ii_pro_worker.school_contract import validate_result

    package = json.loads(run["action_json"])["parameters"]["arguments"]["package"]
    checks = validate_result(json.loads(value["result"]["stdout"]), package)
    if (value["run_id"] != run["id"] or value["stop_confirmed"] is not True or value["process_exit_code"] != 0
            or (checks["passed"], checks["total"]) != (body["evidence"]["passed"], body["evidence"]["total"])):
        raise Conflict("Functional result differs from card")
    return {"version": 1, "id": entry, "card": body, "card_sha256": row["sha256"],
            "created_at": row["created_at"], "history": history,
            "evidence": {"seal_sha256": seal["digest"], "manifest": manifest,
                         "receipt_sha256": receipt["sha256"], "functional_result": checks}}


def export_card(source, entry):
    with origin(source) as (db, _):
        bundle = _verified_bundle(db, entry)
    return {"bundle": bundle, "sha256": digest(bundle)}


def transfer_record(conn, entry):
    row = conn.execute("""SELECT payload_json FROM events WHERE kind='memory.transferred'
        AND json_extract(payload_json,'$.entry')=? LIMIT 1""", (entry,)).fetchone()
    return json.loads(row[0]) if row else None


def current_origin(record, entry):
    with origin(record["origin"]) as (db, identity):
        if identity != record["identity"]:
            raise Conflict("Knowledge origin database was replaced")
        history = source_history(db, entry)
        prefix = history[:record["history_size"]]
        if digest(prefix) != record["history_sha256"]:
            raise Conflict("Source trust history differs")
        card = db.execute("SELECT body_json,sha256 FROM engineering_memory WHERE id=?", (entry,)).fetchone()
        if not card or digest(json.loads(card[0])) != card[1] or card[1] != record["card_sha256"]:
            raise Conflict("Source knowledge card differs")
        if history[-1]["status"] == "verified" and digest(_verified_bundle(db, entry)) != record["bundle_sha256"]:
            raise Conflict("Source functional proof changed")
        return history


def origin_still_verified(conn, entry):
    record = transfer_record(conn, entry)
    if not record:
        return True
    try:
        return current_origin(record, entry)[-1]["status"] == "verified"
    except (CoreError, OSError, sqlite3.Error, ValueError, KeyError, TypeError, IndexError):
        return False


def source_execution_blocked(conn, entry, *, pilot=False):
    """Moving comparative knowledge does not move or relax its origin's stop fence."""
    record = transfer_record(conn, entry)
    if not record:
        return False
    try:
        with origin(record["origin"]) as (db, identity):
            if identity != record["identity"] or db.execute("SELECT 1 FROM worker_jobs WHERE stop_confirmed=0").fetchone():
                return True
            rows = db.execute("""SELECT id,executor,action_hash,status,effect_status FROM runs
                WHERE status IN ('running','unknown') OR effect_status IN ('unknown','partial')
                   OR (status='failed' AND effect_status='applied')""")
            for row in rows:
                if not pilot or (row["status"], row["effect_status"], row["executor"]) != ("unknown", "unknown", "team_text"):
                    return True
                closed = canonical({"abandoned_run_id": row["id"], "action_hash": row["action_hash"],
                                    "accept_unknown_cost": True, "no_retry": True})
                if not db.execute("SELECT 1 FROM events WHERE kind='team.unknown_abandoned' AND payload_json=?", (closed,)).fetchone():
                    return True
            return False
    except (CoreError, OSError, sqlite3.Error):
        return True


def import_card(core, source, envelope):
    if (not isinstance(envelope, dict) or set(envelope) != {"bundle", "sha256"}
            or not isinstance(envelope["bundle"], dict) or not isinstance(envelope["bundle"].get("id"), str)
            or digest(envelope["bundle"]) != envelope["sha256"]):
        raise ValidationError("Invalid knowledge bundle checksum")
    source = Path(source).absolute()
    if source.samefile(core.store.path):
        raise Conflict("Source and destination must be separate databases")
    entry = envelope["bundle"]["id"]
    with origin(source) as (db, identity):
        bundle = _verified_bundle(db, entry)
        if bundle != envelope["bundle"]:
            raise Conflict("Bundle differs from the independently selected trusted origin")
        history = bundle["history"]
        with core.store.transaction(admit=True) as conn:
            record = {"entry": entry, "origin": str(source), "identity": identity,
                      "bundle_sha256": envelope["sha256"], "card_sha256": bundle["card_sha256"],
                      "history_size": len(history), "history_sha256": digest(history)}
            if conn.execute("SELECT 1 FROM engineering_memory WHERE id=?", (entry,)).fetchone():
                if transfer_record(conn, entry) != record:
                    raise Conflict("Knowledge ID is already bound to another record")
                return entry
            conn.execute("INSERT INTO engineering_memory VALUES(?,?,?,?)",
                         (entry, canonical(bundle["card"]), bundle["card_sha256"], bundle["created_at"]))
            for item in history:
                conn.execute("""INSERT INTO engineering_memory_status(entry_id,status,reviewer,reason,created_at)
                             VALUES(?,?,?,?,?)""", (entry, item["status"], item["reviewer"], item["reason"], item["created_at"]))
            core.store.event(conn, "memory.transferred", **record)
    return entry


def sync_revocation(core, entry):
    with core.store.transaction(admit=True) as conn:
        record = transfer_record(conn, entry)
        if not record:
            raise Conflict("This knowledge card was not transferred")
        latest = current_origin(record, entry)[-1]
        current = source_history(conn, entry)[-1]["status"]
        if current == "verified" and latest["status"] in {"revoked", "stale"}:
            conn.execute("""INSERT INTO engineering_memory_status(entry_id,status,reviewer,reason,created_at)
                         VALUES(?,?,?,?,?)""", (entry, latest["status"], latest["reviewer"], latest["reason"], latest["created_at"]))
            core.store.event(conn, "memory.source_trust_changed", entry=entry, status=latest["status"])
            return latest["status"]
        return current


def main():
    from .core import Core

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, values in (("export", ("source", "entry", "bundle")),
                         ("import", ("destination", "source", "bundle")),
                         ("sync-revocation", ("destination", "entry"))):
        command = commands.add_parser(name)
        for value in values:
            command.add_argument(value)
    args = parser.parse_args()
    try:
        if args.command == "export":
            payload = canonical(export_card(args.source, args.entry)).encode()
            with Path(args.bundle).open("xb") as out:
                out.write(payload)
        else:
            destination = Path(args.destination)
            if not (destination / "core.sqlite3").is_file():
                raise Conflict("Initialize the destination through Studio first")
            with Core(destination) as core:
                if args.command == "sync-revocation":
                    print(sync_revocation(core, args.entry))
                else:
                    with Path(args.bundle).open("rb") as incoming:
                        payload = incoming.read(131073)
                    if len(payload) > 131072:
                        raise ValidationError("Knowledge bundle exceeds quota")
                    print(import_card(core, args.source, json.loads(payload)))
    except (CoreError, KeyError, OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f"Knowledge transfer refused ({type(exc).__name__})\n")


if __name__ == "__main__":
    main()
