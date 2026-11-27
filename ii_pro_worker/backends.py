"""Fixed native fixture backend and compatibility exports for Docker."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
import tempfile

from ii_pro_core.security import no_links

from .errors import DockerApprovalRequired as DockerApprovalRequired
from .identity import source_hashes
from .docker_launcher import DockerBackend as DockerBackend


@dataclass(frozen=True)
class NativeFixtureBackend:
    """Same-user process, NOT a sandbox. No caller-controlled executable/argv."""
    name = "trusted-native-fixture"

    scratch_root: Path
    allow_trusted_fixtures: bool = False

    def __post_init__(self):
        if self.allow_trusted_fixtures is not True:
            raise ValueError("Explicit trusted-fixture opt-in required; no sandbox provided")
        object.__setattr__(self, "scratch_root", Path(self.scratch_root).absolute())

    def configuration(self):
        return {"backend": self.name, "isolated": False, "scratch_root": str(self.scratch_root),
                "python": sys.executable, "sources": source_hashes(), "revision": "native-fixed-v1"}

    def prepare(self):
        no_links(self.scratch_root)
        if not self.scratch_root.is_dir():
            raise ValueError("Trusted scratch root must already exist")

    def start(self):
        # No application env, credentials, PATH, PYTHONPATH or Core handles.
        env = {k: os.environ[k] for k in ("SystemRoot", "WINDIR") if k in os.environ}
        scratch = tempfile.TemporaryDirectory(prefix="worker-", dir=self.scratch_root)
        if not Path(scratch.name).resolve().is_relative_to(self.scratch_root.resolve()):
            raise ValueError("Fixture scratch escaped its intended root")
        try:
            process = subprocess.Popen([sys.executable, "-I", "-S", "-B", str(Path(__file__).with_name("runtime.py"))],
                                       cwd=scratch.name, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, close_fds=True, shell=False,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except BaseException:
            scratch.cleanup()
            raise
        return NativeHandle(process, scratch, self.scratch_root.resolve())


class NativeHandle:
    def __init__(self, process, scratch, intended_root):
        self.process, self.scratch, self.intended_root = process, scratch, intended_root
        self.stdin, self.stdout, self.stderr = process.stdin, process.stdout, process.stderr

    @property
    def identity(self):
        return {"pid": self.process.pid, "kind": "trusted-fixed-process"}

    def poll(self):
        return self.process.poll()  # trusted fixtures cannot spawn descendants

    def cleanup(self):
        if self.poll() is None:
            raise RuntimeError("Cannot clean a live fixture")
        for stream in (self.stdin, self.stdout, self.stderr):
            stream.close()
        no_links(Path(self.scratch.name))
        if not Path(self.scratch.name).resolve().is_relative_to(self.intended_root):
            raise ValueError("Fixture scratch path changed")
        self.scratch.cleanup()
