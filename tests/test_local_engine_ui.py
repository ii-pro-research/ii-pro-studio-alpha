"""Real Studio loopback HTTP, with only Ollama replaced by a controlled fixture."""

from contextlib import contextmanager
from http.client import HTTPConnection
import json
from threading import Event, Thread
import time

import pytest

from ii_pro_core import Core
from ii_pro_core.errors import CoreBusy
from ii_pro_local.snapshot import Snapshot
from ii_pro_school.adapters import LoopbackTransport, StudentFailure
from ii_pro_ui.server import LocalServer
from ii_pro_ui.local_engine import load_projects
from ii_pro_ui.training import REAL_PROFILE, Training
from test_engineering_memory import entry
from test_local_agent import BUGGY, fixture_project

JOB = "a" * 32
BASE = "/api/local-engine/"


@contextmanager
def studio(path, projects, *, live=True):
    server = LocalServer(0, None)
    server.training = Training(
        path,
        server.token,
        include_ollama=True,
        include_team=True,
        local_projects=projects,
        local_live=live,
    )
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.03})
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(3)
        server.server_close()


def request(server, path, body=None, *, trusted=True):
    method = "GET" if body is None else "POST"
    headers = {"Authorization": "Bearer " + server.token}
    if body is not None:
        headers.update(
            {
                "Origin": server.origin,
                "Sec-Fetch-Site": "same-origin",
                "X-CSRF-Token": server.csrf if trusted else "invalid",
                "Content-Type": "application/json",
            }
        )
    client = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        client.request(
            method,
            path,
            body=json.dumps(body) if body is not None else None,
            headers=headers,
        )
        response = client.getresponse()
        return response.status, json.loads(response.read())
    finally:
        client.close()


def payload(job=JOB):
    return {
        "project": "pricing",
        "job": job,
        "task": "Review regex validation and discount",
        "requirements": "Preserve the public signature.",
    }


def accepted(server, path, body=None):
    code, result = request(server, BASE + path, body)
    assert code == 200, result
    return result


def decision(detail):
    approval = detail["approval"]
    return {
        "project": detail["project"],
        "job": detail["job"],
        "allow": True,
        "run": approval["run"],
        "approval": approval["id"],
        "digest": approval["digest"],
    }


def settled(server, job):
    until = time.monotonic() + 5
    while time.monotonic() < until:
        detail = accepted(server, "jobs/pricing/" + job)
        if not server.training.threads and detail["status"] not in {
            "running",
            "stopping_after_current_step",
            "awaiting_approval",
        }:
            return detail
        time.sleep(0.02)
    pytest.fail("Local Engine did not settle")


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "project"
    base, revision = fixture_project(path)
    snap = Snapshot.register(path, base=base, revision=revision, paths=["pricing.py"])
    return {"pricing": ("Проверка скидки", snap)}, path


def mock_model(monkeypatch, replies):
    calls = []
    iterator = iter(replies)

    def request(self, route, payload=None, **kwargs):
        if route == "version":
            return {"version": "fixture"}
        if route == "tags":
            return {
                "models": [
                    {"name": REAL_PROFILE.model, "digest": REAL_PROFILE.expected_digest}
                ]
            }
        if route == "show":
            return {}
        assert route == "chat" and payload["format"]["oneOf"]
        calls.append(payload)
        reply = next(iterator)
        if callable(reply):
            reply = reply()
        if isinstance(reply, Exception):
            raise reply
        return {
            "model": REAL_PROFILE.model,
            "done": True,
            "done_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps(reply)},
        }

    monkeypatch.setattr(LoopbackTransport, "request", request)
    return calls


def test_end_to_end_shared_owner_authority_restart_and_memory(
    tmp_path, registry, monkeypatch
):
    projects, source = registry
    read = {"tool": "read_file", "args": {"path": "pricing.py", "line": 1}}
    calls = mock_model(
        monkeypatch,
        [
            {**read, "plan": "Read the formula."},
            {
                "tool": "propose_patch",
                "args": {
                    "path": "pricing.py",
                    "before": "return price +",
                    "after": "return price -",
                },
            },
            {
                "tool": "finish",
                "args": {
                    "report": "The candidate subtracts the discount.",
                    "evidence": [1, 2],
                },
            },
        ],
    )
    state = tmp_path / "studio"
    with studio(state, projects) as server:
        dashboard = accepted(server, "state")
        assert dashboard["projects"] == [{"id": "pricing", "title": "Проверка скидки"}]
        core = server.training.shared_core
        assert (
            server.training.local_engine.core
            is server.training.team.core
            is server.training.school.core
            is core
        )
        core.memory.record("mentor", entry(), tested=True)
        assert (
            accepted(server, "state")["memory"][0]["history"][-1]["status"] == "tested"
        )
        with pytest.raises(CoreBusy):
            Core(state)
        assert request(server, BASE + "prepare", payload(), trusted=False)[0] == 403
        assert (
            request(server, BASE + "prepare", {**payload(), "root": "C:/private"})[0]
            == 409
        )
        assert (
            request(server, BASE + "prepare", {**payload(), "project": "unknown"})[0]
            == 409
        )
        detail = accepted(server, "prepare", payload())
        assert detail["status"] == "awaiting_approval" and not calls
        initial = detail["approval"]["id"]
        assert (
            request(server, BASE + "decision", {**decision(detail), "digest": "wrong"})[
                0
            ]
            == 409
        )
    with studio(state, projects) as server:
        assert accepted(server, "state")["jobs"][0]["job"] == JOB
        detail = accepted(server, "jobs/pricing/" + JOB)
        assert detail["approval"]["status"] == "stale" and not detail["can_approve"]
        detail = accepted(server, "prepare", payload())
        assert detail["approval"]["id"] != initial
        for step in range(1, 4):
            chosen = decision(detail)
            accepted(server, "decision", chosen)
            assert request(server, BASE + "decision", chosen)[0] == 409
            detail = settled(server, JOB)
            assert detail["results"][str(step)]["status"] == "observed"
            if step < 3:
                detail = accepted(server, "prepare", payload())
        assert detail["status"] == "finished"
        assert detail["runs"][0]["memory"] == []
        assert detail["results"]["2"]["result"]["functional_tests"] == "not_run"
        assert "-    return price +" in detail["results"]["2"]["result"]["diff"]
        assert (
            request(server, "/api/team/state")[0]
            == request(server, "/api/school/training")[0]
            == 200
        )
    with studio(state, projects, live=False) as server:
        restored = accepted(server, "jobs/pricing/" + JOB)
        assert restored["status"] == "finished" and restored["runs"] == detail["runs"]
        new = accepted(server, "prepare", payload("b" * 32))
        assert (
            not new["can_approve"]
            and request(server, BASE + "decision", decision(new))[0] == 409
        )
    assert len(calls) == 3 and (source / "pricing.py").read_bytes() == BUGGY.encode()


def test_stop_during_run_unknown_and_budget(tmp_path, registry, monkeypatch):
    projects, _ = registry
    entered, release = Event(), Event()
    list_files = {"tool": "list_files", "args": {}}

    def blocked():
        entered.set()
        assert release.wait(5)
        return {**list_files, "plan": "Inspect."}

    calls = mock_model(
        monkeypatch,
        [
            blocked,
            StudentFailure("timeout"),
            {**list_files, "plan": "Inspect."},
            *[list_files] * 5,
        ],
    )
    with studio(tmp_path / "studio", projects) as server:
        detail = accepted(server, "prepare", payload())
        accepted(server, "decision", decision(detail))
        try:
            assert entered.wait(4)
            stopped = accepted(server, "stop", {"project": "pricing", "job": JOB})
            assert stopped["status"] == "stopping_after_current_step"
        finally:
            release.set()
        assert (
            settled(server, JOB)["results"]["1"]["result"]["reason"] == "operator_stop"
        )
        unknown = "b" * 32
        detail = accepted(server, "prepare", payload(unknown))
        permit = decision(detail)
        accepted(server, "decision", permit)
        failed = settled(server, unknown)
        assert (
            failed["status"] == "unknown"
            and failed["runs"][0]["error_code"] == "executor_failed"
        )
        assert (
            request(server, BASE + "decision", permit)[0]
            == request(server, BASE + "prepare", payload(unknown))[0]
            == 409
        )
        budget = "c" * 32
        for step in range(6):
            detail = accepted(server, "prepare", payload(budget))
            accepted(server, "decision", decision(detail))
            detail = settled(server, budget)
        assert detail["status"] == "budget_exhausted"
        assert len(calls) == 8
        assert request(server, BASE + "prepare", payload(budget))[0] == 409


def test_handoff_uses_approved_project_scope_and_does_not_dispatch(
    tmp_path, registry, monkeypatch
):
    projects, source = registry
    title, snapshot = projects["pricing"]
    config = tmp_path / "projects.json"
    config.write_text(
        json.dumps(
            [
                {
                    "id": "pricing",
                    "title": title,
                    "root": str(source),
                    "base": snapshot.base,
                    "revision": snapshot.revision,
                    "files": ["pricing.py"],
                }
            ]
        ),
        encoding="utf-8",
    )
    assert load_projects(config) == projects
    calls = mock_model(
        monkeypatch,
        [
            {
                "tool": "read_file",
                "args": {"path": "pricing.py", "line": 1},
                "plan": "Inspect",
            },
            {
                "tool": "propose_patch",
                "args": {
                    "path": "pricing.py",
                    "before": "missing",
                    "after": "candidate",
                },
            },
            {
                "tool": "propose_patch",
                "args": {
                    "path": "pricing.py",
                    "before": "return price +",
                    "after": "return (",
                },
            },
        ],
    )
    with studio(tmp_path / "studio", projects) as server:
        for _ in range(3):
            detail = accepted(server, "prepare", payload())
            assert detail["approval"]["status"] == "pending"
            accepted(server, "decision", decision(detail))
            detail = settled(server, JOB)
        handoff = detail["handoff"]
        assert detail["status"] == "waiting_for_engineer"
        assert handoff["recipient"] == "gpt_engineer" and handoff["version"] == 1
        assert handoff["snapshot"] == snapshot.identity and handoff["files"] == [
            "pricing.py"
        ]
        assert handoff["criteria"] == [payload()["requirements"]]
        assert handoff["functional_tests"] == "not_run"
        assert request(server, BASE + "prepare", payload())[0] == 409
        assert len(calls) == 3
