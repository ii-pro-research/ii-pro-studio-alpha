"""Operator-only build/smoke entry point; no execution without review consent.

python -m ii_pro_worker.acceptance --review
python -m ii_pro_worker.acceptance --approved-review SHA --output .local/worker-v021-tests/live
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from ii_pro_core import Action, Core, TaskStatus
from ii_pro_core.security import no_links

from .adapter import WorkerAdapter
from .docker_cli import DockerCLI
from .docker_launcher import DockerBackend
from .errors import DockerApprovalRequired, DockerConfigMismatch
from .identity import deployment_review
from .profile import DockerProfile
from .supervisor import Limits
from .wire import fingerprint

ROOT = Path(__file__).resolve().parents[1]


def proposed_review():
    descriptor = deployment_review(DockerProfile("sha256:" + "0" * 64))
    return {"review_digest": fingerprint(descriptor), "review": descriptor,
            "base": json.loads((ROOT / "deployment/worker/runtime.lock.json").read_bytes()),
            "status": "AWAITING_EXPLICIT_OPERATOR_APPROVAL"}


def local_output(path):
    path = Path(path).absolute()
    no_links(path)
    allowed = (ROOT / ".local/worker-v021-tests", ROOT / ".local/school-v02-tests")
    if not any(path.resolve().is_relative_to(folder.resolve()) for folder in allowed):
        raise ValueError("Acceptance output must stay in the approved local Worker/School test directories")
    path.mkdir(parents=True, exist_ok=True)
    no_links(path)
    return path


def verify_base_identity(metadata, locked):
    """Engine 29's containerd store may expose manifest ID, not config ID.

    Both cases remain bound to the exact approved repository/platform digest.
    This is identity-format compatibility, never acceptance of another image.
    """
    if metadata.get("os") != "linux" or metadata.get("arch") != "amd64":
        raise DockerConfigMismatch("Base image platform mismatch")
    platform, config = locked["platform_digest"], locked["config_digest"]
    refs = {name + "@" + platform for name in ("python", "library/python", "docker.io/library/python")}
    if not refs.intersection(metadata.get("repo_digests") or []):
        raise DockerConfigMismatch("Approved base repository digest is missing")
    if metadata.get("id") == config:
        return "config-digest"
    if metadata.get("id") == platform and (metadata.get("descriptor") or {}).get("digest") == platform:
        return "platform-manifest-digest"
    raise DockerConfigMismatch("Base image identity does not match the pinned manifest/config")


def build_image(cli, approved_review, output, *, pull=True):
    review = proposed_review()
    if approved_review != review["review_digest"]:
        raise DockerApprovalRequired("Operator must approve the current image/profile review")
    output = local_output(output)
    context = output / "build-context"
    no_links(context)
    context.mkdir()  # never overwrite an old evidence/build context
    (context / "ii_pro_worker").mkdir()
    for name, sha in review["review"]["sources"].items():
        source = ROOT / "ii_pro_worker" / name
        no_links(source)
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise DockerConfigMismatch("Build source changed since review")
        (context / "ii_pro_worker" / name).write_bytes(data)
    recipe = (ROOT / "deployment/worker/Dockerfile").read_bytes()
    if hashlib.sha256(recipe).hexdigest() != review["review"]["recipe"]["deployment/worker/Dockerfile"]:
        raise DockerConfigMismatch("Dockerfile changed since review")
    (context / "Dockerfile").write_bytes(recipe)
    # Physical context contains ONLY reviewed runner files, Dockerfile and allowlist.
    (context / ".dockerignore").write_text("**\n!Dockerfile\n!ii_pro_worker/\n!ii_pro_worker/*.py\n", encoding="utf-8")
    base = review["base"]["repository"] + "@" + review["base"]["platform_digest"]
    prefix = ["--context", "desktop-linux"]
    pulled = cli.call([*prefix, "pull", "--quiet", "--platform", "linux/amd64", base], timeout=60) if pull else b"Using the existing pinned local base; no pull requested.\n"
    (output / "pull.log").write_bytes(pulled)
    metadata = cli.json([*prefix, "image", "inspect", "--format",
                         '{"id":{{json .Id}},"os":{{json .Os}},"arch":{{json .Architecture}},'
                         '"repo_digests":{{json .RepoDigests}},"descriptor":{{json .Descriptor}}}', base])
    metadata["identity_kind"] = verify_base_identity(metadata, review["base"])
    (output / "base-inspect.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    iid = output / "worker-image.iid"
    built = cli.call([*prefix, "build", "--builder", "desktop-linux", "--quiet", "--pull=false", "--network", "none", "--platform", "linux/amd64",
              "--build-arg", "BASE_IMAGE=" + base, "--label", "ii-pro.review=" + approved_review,
              "--iidfile", str(iid), str(context)], timeout=60)
    (output / "build.log").write_bytes(built)
    profile = DockerProfile(iid.read_text(encoding="utf-8").strip())
    if proposed_review()["review_digest"] != approved_review:
        raise DockerConfigMismatch("Deployment review changed during build")
    result = {"review_digest": approved_review, "base": base, "image_id": profile.image,
              "profile": profile.describe(), "context_files": sorted(str(p.relative_to(context)) for p in context.rglob("*") if p.is_file())}
    (output / "build.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return profile


def smoke(profile, approved_review, output, *, cli=None, cleanup=True):
    output = local_output(output)
    backend = DockerBackend(profile, approved_review=approved_review, cli=cli)
    adapter = WorkerAdapter(backend, limits=Limits(runtime_ms=5000, heartbeat_ms=2000, cancel_grace_ms=500))
    with Core(output / "smoke-state", executors=[adapter], shutdown_timeout=10) as core:
        adapter.bind(core)
        task = core.tasks.create("Approved fixed Docker smoke", executor="worker", idempotency_key="docker-smoke")
        core.tasks.transition(task.id, TaskStatus.PLANNED)
        run = core.runs.create(task.id, action=Action.build("worker.fixed", {"fixture": "smoke", "arguments": {}}),
                               idempotency_key="docker-smoke-run")
        # This function is only called after explicit operator authorization of
        # THIS fixed acceptance test. It is not a generic automatic approver.
        approval = core.approvals.request(run.id)
        (output / "smoke-operation.json").write_text(json.dumps(core.approvals.describe(approval.id), indent=2), encoding="utf-8")
        core.approvals.decide(approval.id, allow=True)
        completed = core.runs.execute(run.id, approval_id=approval.id)
        jobs = adapter.jobs()
        (output / "smoke-jobs.json").write_text(json.dumps(jobs, indent=2), encoding="utf-8")
        (output / "smoke-run.json").write_text(json.dumps(asdict(completed), indent=2), encoding="utf-8")
        (output / "smoke-events.json").write_text(json.dumps([asdict(event) for event in core.events()], indent=2), encoding="utf-8")
        if completed.status != "succeeded":
            raise RuntimeError("Smoke did not succeed; inspect saved job, do not advance to more tests")
        artifacts = {a.relative_path.rsplit("/", 1)[-1]: core.artifacts.read(a.id) for a in core.artifacts.list(run.id)}
        for name, content in artifacts.items():
            (output / ("smoke-" + name)).write_bytes(content)
        result = json.loads(artifacts["stdout.txt"])
        if result["effective_uid"] != 65532 or result["effective_gid"] != 65532:
            raise DockerConfigMismatch("Smoke identity mismatch")
        (output / "smoke-inspect.json").write_text(json.dumps(
            backend.stopped_observation(json.loads(jobs[0]["handle_json"])), indent=2), encoding="utf-8")
        if cleanup:
            adapter.cleanup_docker_job(jobs[0]["job_id"])
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", action="store_true")
    parser.add_argument("--approved-review")
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--no-pull", action="store_true")
    parser.add_argument("--output", default=str(ROOT / ".local/worker-v021-tests/live"))
    args = parser.parse_args()
    if args.review:
        print(json.dumps(proposed_review(), indent=2))
        return
    if args.approved_review != proposed_review()["review_digest"]:
        raise DockerApprovalRequired("Show --review and obtain explicit operator consent first")
    cli = DockerCLI()
    profile = build_image(cli, args.approved_review, args.output, pull=not args.no_pull)
    result = profile.describe() if args.build_only else smoke(profile, args.approved_review, args.output, cli=cli)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
