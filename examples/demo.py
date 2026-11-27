"""Reproducible normal/restart/hard-exit demo; all work stays in a dedicated directory."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ii_pro_core import (Core, MockExecutor, TaskStatus, EffectStatus,
                        Reconciler, ReconciliationReceipt)  # noqa: E402


class HardExitExecutor(MockExecutor):
    def execute(self, request):
        os._exit(23)  # actual child-process crash, not an exception caught by Core


def worker(state_dir: Path, run_id: str):
    with Core(state_dir, executors=[HardExitExecutor()]) as core:
        core.runs.execute(run_id)


def demo(state_dir: Path) -> dict:
    if (state_dir / "core.sqlite3").exists():
        raise ValueError("Choose a fresh demo state directory")
    with Core(state_dir) as core:
        task = core.tasks.create("Core v0.1 demo", idempotency_key="demo-task")
        core.tasks.transition(task.id, TaskStatus.PLANNED)
        run = core.runs.create(task.id, idempotency_key="demo-run-1")
        done = core.runs.execute(run.id)
        core.tasks.transition(task.id, TaskStatus.VERIFIED)
        saved = core.tasks.transition(task.id, TaskStatus.SAVED)
        artifact = core.artifacts.list(run.id)[0]
        normal = {"task": asdict(saved), "run": asdict(done), "artifact": asdict(artifact)}
    # A separate interpreter reopens SQLite and verifies the same IDs/state/files.
    probe = subprocess.run([sys.executable, "-m", "ii_pro_core", "--state-dir", str(state_dir),
                            "task", task.id], cwd=ROOT, capture_output=True, text=True, check=True, timeout=15)
    restored = json.loads(probe.stdout)
    with Core(state_dir) as core:
        interrupted_task = core.tasks.create("Interruption demo", idempotency_key="crash-task")
        core.tasks.transition(interrupted_task.id, TaskStatus.PLANNED)
        pending = core.runs.create(interrupted_task.id, idempotency_key="crash-run-1")
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker",
                            "--state-dir", str(state_dir), "--run-id", pending.id], cwd=ROOT,
                           capture_output=True, text=True, timeout=15)
    if child.returncode != 23:
        raise RuntimeError("Crash demonstration did not reach the executor")

    class DemoVerifier(Reconciler):
        name = "demo-hard-exit"

        def reconcile(self, run):
            # Only this exact fixture is verified: its child exited with 23,
            # and HardExitExecutor.execute contains only os._exit, no effect.
            if run.id != pending.id or child.returncode != 23:
                raise ValueError("Unexpected demonstration run")
            return ReconciliationReceipt(run.task_id, run.id, run.action_hash,
                                         EffectStatus.NOT_APPLIED, "demo:exit23-no-effect-executor")

    with Core(state_dir, reconcilers=[DemoVerifier()]) as core:
        recovered = core.runs.get(pending.id)
        recovered_ids = list(core.recovered_run_ids)
        core.runs.resolve_interrupted(pending.id, confirm_stopped=True)
        core.runs.reconcile(pending.id, verifier="demo-hard-exit")
        core.tasks.transition(interrupted_task.id, TaskStatus.PLANNED)
        retry = core.runs.create(interrupted_task.id, idempotency_key="crash-run-2")
        retry = core.runs.execute(retry.id)
        attempts = [asdict(row) for row in core.runs.list(interrupted_task.id)]
        artifact_valid = core.artifacts.validate(artifact.id)
        event_count = len(core.events())
    return {"normal": normal, "reopened_in_another_process": restored,
            "artifact_valid_after_restart": artifact_valid,
            "interruption": {"child_exit_code": child.returncode, "recovered_run_ids": recovered_ids,
                             "status_after_recovery": recovered.status, "retry_status": retry.status,
                             "attempts": attempts}, "event_count": event_count}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    if args.worker:
        if args.state_dir is None or args.run_id is None:
            parser.error("worker requires state directory and run ID")
        worker(args.state_dir, args.run_id)
    elif args.state_dir:
        print(json.dumps(demo(args.state_dir.absolute()), ensure_ascii=True, indent=2))
    else:
        with tempfile.TemporaryDirectory(prefix="ii-pro-demo-") as directory:
            print(json.dumps(demo(Path(directory)), ensure_ascii=True, indent=2))
