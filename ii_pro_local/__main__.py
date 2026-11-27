"""Operator-only entry point for the local engineering workflow."""

import argparse
import json
import sys

from ii_pro_core import Core
from ii_pro_core.errors import CoreError, Conflict
from ii_pro_school.adapters import ModelProfile
from .agent import LocalAgent, LocalAgentExecutor
from .snapshot import Snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="II PRO Local Agent (read-only Git snapshot)"
    )
    parser.add_argument("--state", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--file", action="append", required=True)
    parser.add_argument("--model-digest", required=True)
    parser.add_argument("--job", required=True)
    commands = parser.add_subparsers(dest="operation", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--task", required=True)
    prepare.add_argument("--requirements", required=True)
    commands.add_parser("status")
    commands.add_parser("stop")
    dispatch = commands.add_parser("dispatch")
    dispatch.add_argument("--run", required=True)
    dispatch.add_argument("--approval", required=True)
    dispatch.add_argument("--spec-digest", required=True)
    args = parser.parse_args(argv)
    try:
        snapshot = Snapshot.register(
            args.project, base=args.base, revision=args.revision, paths=args.file
        )
        executor = LocalAgentExecutor(
            ModelProfile.build("qwen7b-fast:latest", args.model_digest), snapshot
        )
        with Core(args.state, executors=[executor]) as core:
            agent = LocalAgent(core, executor)
            if args.operation == "prepare":
                run, approval = agent.prepare(
                    args.job, task=args.task, requirements=args.requirements
                )
                result = {
                    "job": args.job,
                    "run": run.id,
                    "approval": approval.id,
                    "approval_status": str(approval.status),
                    "expires_at": approval.expires_at,
                    "spec_digest": approval.spec_digest,
                    "operation": core.approvals.describe(approval.id),
                }
            else:
                if args.operation == "stop":
                    agent.stop(args.job)
                if args.operation == "dispatch":
                    with core.store.lock:
                        runs = agent.runs(args.job)
                        agent._guard(args.job, runs)
                        approval = core.approvals.get(args.approval)
                        if (
                            not runs
                            or runs[-1].id != args.run
                            or runs[-1].status != "created"
                            or approval.run_id != args.run
                            or approval.spec_digest != args.spec_digest
                        ):
                            raise Conflict(
                                "Approval does not match the current prepared operation"
                            )
                        core.approvals.decide(approval.id, allow=True)
                    core.runs.execute(args.run, approval_id=args.approval)
                result = agent.status(args.job)
    except CoreError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
