import hashlib
import json
from pathlib import Path


def source_hashes():
    root = Path(__file__).parent
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob("*.py"))}


def deployment_review(profile):
    """Image is derived AFTER review from this pinned base and source bundle."""
    root = Path(__file__).resolve().parents[1]
    paths = ("deployment/worker/Dockerfile", "deployment/worker/Dockerfile.dockerignore",
             "deployment/worker/runtime.lock.json")
    resources = profile.describe()
    resources.pop("image")
    sources = source_hashes()
    return {"revision": "worker-docker-review-v021", "resources": resources,
            "sources": {name: sources[name] for name in ("runtime.py", "wire.py", "probes.py", "school_contract.py",
                                                       "python_runner.py", "app_contract.py", "app_runner.py")},
            "recipe": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in paths}}


def archived_school_review(profile):
    """Retain the existing School image without authorizing a new one."""
    if profile.image != "sha256:81538a4143cb5aac5d330ce045e2b8b9354c8dfc61a9fb7866bb91011518dc50":
        raise ValueError("Unexpected archived School image")
    path = Path(__file__).resolve().parents[1] / "deployment/worker/school-v02.review.json"
    review = json.loads(path.read_bytes())["review"]
    resources = profile.describe()
    resources.pop("image")
    if review["resources"] != resources:
        raise ValueError("Archived School profile differs")
    return review
