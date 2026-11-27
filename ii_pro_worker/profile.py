"""Reviewable Docker create argv. No daemon calls, builds, pulls or starts."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re

from .wire import fingerprint


@dataclass(frozen=True)
class DockerProfile:
    image: str
    context: str = "desktop-linux"
    cpu: str = "0.5"
    memory: str = "128m"
    pids: int = 16

    def __post_init__(self):
        if (type(self.image) is not str or not re.fullmatch(r"sha256:[a-f0-9]{64}", self.image)
                or (self.context, self.cpu, self.memory, self.pids) != ("desktop-linux", "0.5", "128m", 16)):
            raise ValueError("Only the fixed profile and a pinned local image ID are accepted")

    @property
    def digest(self):
        return fingerprint(self.describe())

    def describe(self):
        return {**asdict(self), "revision": "docker-fixed-v021", "user": "65532:65532",
                "network": "none", "read_only": True, "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"], "mounts": [],
                "tmpfs": ["/tmp:rw,noexec,nosuid,nodev,size=1m,mode=0700,uid=65532,gid=65532"],
                "memory_swap": self.memory, "pull": "never", "init": True,
                "entrypoint": ["/usr/local/bin/python", "-I", "-S", "-B", "/opt/worker/runtime.py"],
                "max_runtime_ms": 5000, "max_frame_bytes": 16384,
                "max_output_bytes": 4096, "core_secrets": False, "docker_socket": False}

    def create_argv(self, job_id, run_id, epoch):
        if any(not re.fullmatch(r"[a-f0-9]{32}", value) for value in (job_id, run_id)):
            raise ValueError("Invalid launch identity")
        if type(epoch) is not int or not 1 <= epoch < 2**53:
            raise ValueError("Invalid epoch")
        config = self.describe()
        args = ["--context", self.context, "create", "--pull", "never", "--interactive", "--init",
                "--name", self.container_name(job_id), "--ipc", "private", "--cgroupns", "private", "--runtime", "runc",
                "--user", "65532:65532", "--read-only", "--network", "none",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--cpus", self.cpu, "--memory", self.memory, "--memory-swap", self.memory,
                "--pids-limit", str(self.pids), "--workdir", "/work", "--log-driver", "none"]
        for mount in config["tmpfs"]:
            args.extend(["--tmpfs", mount])
        for key, value in {"job": job_id, "run": run_id, "epoch": str(epoch), "profile": self.digest}.items():
            args.extend(["--label", f"ii-pro.{key}={value}"])
        args.extend(["--entrypoint", config["entrypoint"][0], self.image, *config["entrypoint"][1:]])
        return args

    @staticmethod
    def container_name(job_id):
        if type(job_id) is not str or not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("Invalid job ID")
        return "ii-pro-worker-" + job_id

    def labels(self, job_id, run_id, epoch):
        self.create_argv(job_id, run_id, epoch)  # identity validation, no effects
        return {"ii-pro.job": job_id, "ii-pro.run": run_id,
                "ii-pro.epoch": str(epoch), "ii-pro.profile": self.digest}
