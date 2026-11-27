from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, is_dataclass

from .core import Core
from .errors import CoreError
from .models import TaskStatus
from .security import Action


def output(value):
    def convert(item):
        if is_dataclass(item):
            return asdict(item)
        raise TypeError("Not JSON serializable")
    print(json.dumps(value, default=convert, ensure_ascii=True, indent=2))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="II PRO Core v0.1 (local mock only)")
    root.add_argument("--state-dir", required=True, help="Dedicated local Core state directory")
    commands = root.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--title", required=True)
    create.add_argument("--description", default="")
    create.add_argument("--key", required=True)
    update = commands.add_parser("update")
    update.add_argument("task_id")
    update.add_argument("--version", type=int, required=True)
    update.add_argument("--title")
    update.add_argument("--description")
    for name in ("task", "plan", "verify", "save", "runs", "events"):
        commands.add_parser(name).add_argument("task_id")
    prepare = commands.add_parser("prepare")
    prepare.add_argument("task_id")
    prepare.add_argument("--key", required=True)
    prepare.add_argument("--text", default="Mock task result")
    prepare.add_argument("--protected", action="store_true")
    execute = commands.add_parser("execute")
    execute.add_argument("run_id")
    execute.add_argument("--approval")
    for name in ("run", "artifacts", "cancel"):
        commands.add_parser(name).add_argument("run_id")
    request = commands.add_parser("request-approval")
    request.add_argument("run_id")
    request.add_argument("--ttl", type=int, default=300)
    for name in ("approval", "approve", "reject", "approval-spec", "revoke"):
        commands.add_parser(name).add_argument("approval_id")
    reconcile = commands.add_parser("resolve-interrupted")
    reconcile.add_argument("run_id")
    reconcile.add_argument("--confirm-stopped", action="store_true")
    commands.add_parser("backup")
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        with Core(args.state_dir) as core:
            command = args.command
            if command == "create":
                value = core.tasks.create(args.title, description=args.description, idempotency_key=args.key)
            elif command == "update":
                value = core.tasks.update(args.task_id, expected_version=args.version,
                                          title=args.title, description=args.description)
            elif command == "task":
                value = core.tasks.get(args.task_id)
            elif command in {"plan", "verify", "save"}:
                target = {"plan": TaskStatus.PLANNED, "verify": TaskStatus.VERIFIED, "save": TaskStatus.SAVED}[command]
                value = core.tasks.transition(args.task_id, target)
            elif command == "runs":
                value = core.runs.list(args.task_id)
            elif command == "events":
                value = core.events(args.task_id)
            elif command == "prepare":
                action = Action.build("mock.protected_echo" if args.protected else "mock.echo", {"text": args.text})
                value = core.runs.create(args.task_id, action=action, idempotency_key=args.key)
            elif command == "execute":
                value = core.runs.execute(args.run_id, approval_id=args.approval)
            elif command == "run":
                value = core.runs.get(args.run_id)
            elif command == "artifacts":
                value = core.artifacts.list(args.run_id)
            elif command == "cancel":
                value = core.runs.cancel(args.run_id)
            elif command == "request-approval":
                value = core.approvals.request(args.run_id, ttl_seconds=args.ttl)
            elif command == "approval":
                value = core.approvals.get(args.approval_id)
            elif command == "approval-spec":
                value = core.approvals.describe(args.approval_id)
            elif command == "revoke":
                value = core.approvals.revoke(args.approval_id)
            elif command in {"approve", "reject"}:
                value = core.approvals.decide(args.approval_id, allow=command == "approve")
            elif command == "resolve-interrupted":
                value = core.runs.resolve_interrupted(args.run_id, confirm_stopped=args.confirm_stopped)
            else:
                value = {"backup": str(core.backup())}
            output(value)
        return 0
    except (CoreError, sqlite3.Error, OSError) as exc:
        # Avoid printing raw SQL/OS/exception messages, which may contain private data.
        output({"error": type(exc).__name__})
        return 2
