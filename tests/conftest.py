from contextlib import contextmanager
import os
import sys
from threading import Lock
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ii_pro_core import Core, MockExecutor, TaskStatus  # noqa: E402


@pytest.fixture
def mock():
    return MockExecutor()


@pytest.fixture
def core(tmp_path, mock):
    with Core(tmp_path / "state", executors=[mock]) as instance:
        yield instance


def planned(core, key="task"):
    task = core.tasks.create("Test task", idempotency_key=key)
    return core.tasks.transition(task.id, TaskStatus.PLANNED)


@pytest.fixture
def siwc_protocol_vault(monkeypatch):
    # Protocol tests always use the mock issuer and scratch data. Windows also
    # exercises the native vault. Linux substitutes only the OS-specific calls.
    if os.name == "nt":
        return

    from cryptography.fernet import Fernet
    from ii_pro_core.errors import Conflict
    from ii_pro_team import siwc

    cipher = Fernet(Fernet.generate_key())
    lock = Lock()

    def protect(data, *, decrypt=False):
        return cipher.decrypt(data) if decrypt else cipher.encrypt(data)

    @contextmanager
    def login_lock(_vault):
        if not lock.acquire(blocking=False):
            raise Conflict("Another Studio sign-in is active")
        try:
            yield
        finally:
            lock.release()

    monkeypatch.setattr(siwc, "dpapi", protect)
    monkeypatch.setattr(siwc.Vault, "login_lock", login_lock)
