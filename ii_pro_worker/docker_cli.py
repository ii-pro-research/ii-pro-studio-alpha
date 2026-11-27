"""Bounded Docker CLI transport. No shell, forced kills or Docker socket APIs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading

from .errors import DockerUnavailable


class DockerCLI:
    def __init__(self):
        self._pending = set()
        self._lock = threading.Lock()

    def identity(self):
        executable = shutil.which("docker")
        if executable is None:
            return {"executable": None, "sha256": None}
        with open(executable, "rb") as stream:
            sha = hashlib.file_digest(stream, "sha256").hexdigest()
        return {"executable": str(Path(executable).resolve()), "sha256": sha}

    def spawn(self, args):
        executable = self.identity()["executable"]
        if executable is None:
            raise DockerUnavailable("Docker CLI is not installed")
        # Host supervisor may read Docker context credentials. None of this
        # environment or filesystem authority is delivered to the container.
        env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "USERPROFILE", "HOME", "PATH") if key in os.environ}
        try:
            return subprocess.Popen([executable, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env, close_fds=True, shell=False,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            raise DockerUnavailable("Docker CLI cannot start") from exc

    def call(self, args, *, timeout=4):
        with self._lock:
            if len(self._pending) >= 4:
                raise DockerUnavailable("Unfinished Docker control requests")
            process = self.spawn(args)
            self._pending.add(process)
        process.stdin.close()
        done, overflow = threading.Event(), threading.Event()
        buffers = [bytearray(), bytearray()]

        def drain(stream, target, limit):
            try:
                while data := stream.read(4096):
                    remaining = max(0, limit - len(target))
                    target.extend(data[:remaining])
                    if len(data) > remaining:
                        overflow.set()
            finally:
                stream.close()

        def collect():
            readers = [threading.Thread(target=drain, args=(process.stdout, buffers[0], 65536), daemon=True),
                       threading.Thread(target=drain, args=(process.stderr, buffers[1], 4096), daemon=True)]
            try:
                for thread in readers:
                    thread.start()
                process.wait()
                for thread in readers:
                    thread.join()
            finally:
                with self._lock:
                    self._pending.discard(process)
                done.set()

        threading.Thread(target=collect, daemon=True, name="ii-pro-docker-control").start()
        if not done.wait(timeout):
            # No kill: the command may have reached the daemon. Callers retain
            # unknown intent; background collection stays bounded.
            raise DockerUnavailable("Docker control request timed out")
        if process.returncode != 0 or overflow.is_set():
            raise DockerUnavailable("Docker control request failed")
        return bytes(buffers[0])

    def json(self, args):
        try:
            return json.loads(self.call(args))
        except (ValueError, UnicodeError) as exc:
            raise DockerUnavailable("Invalid Docker metadata") from exc
