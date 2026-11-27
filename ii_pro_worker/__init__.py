"""Trusted fixed-fixture prototype. Docker execution remains approval-gated."""

from .adapter import WorkerAdapter
from .backends import DockerBackend, NativeFixtureBackend
from .profile import DockerProfile

__all__ = ["WorkerAdapter", "DockerBackend", "NativeFixtureBackend", "DockerProfile"]
