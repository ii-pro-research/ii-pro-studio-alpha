import json
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Thread

import pytest
from test_local_engine_ui import decision as local_decision
from test_local_engine_ui import mock_model, registry, request  # noqa: F401
from test_skills import reply as skill_reply

from ii_pro_core.executors import ExecutionResult, Executor, ProducedArtifact
from ii_pro_core.security import canonical
from ii_pro_local.engineering_executor import sha
from ii_pro_local.snapshot import Snapshot
from ii_pro_team import project_contract, project_work
from ii_pro_ui.server import LocalServer
from ii_pro_ui.training import Training
from ii_pro_worker.app_contract import FILTER_PROFILE, PROFILES, app_package


class ProjectTextFixture:
    mock = True
    engineering_supported = True
    auth_error = False
    model = "gpt-6.2-sol"

    def __init__(self):
        self.calls = []
        self.fail_phase = None
        self.reject_plan = False

    def ready(self):
        return True

    def ready_for_deadline(self, seconds):
        return seconds == 120

    def configuration(self):
        return {"model": self.model, "effort": "sdk_default", "backend": "fixture", "authorization_ref": "fixture"}

    def run(self, value, emit):
        self.calls.append(value)
        phase = value["phase"]
        if phase == self.fail_phase:
            raise RuntimeError("Fixture loses its outcome")
        if phase == "plan":
            response = {"plan": ["x" * 181 if self.reject_plan else "Patch the registered file"],
                        "checks": ["Check calculation"]}
        elif phase == "implement":
            assert value["inputs"]["context"]["previous"]["architect"]["plan"]
            ctx = value["inputs"]["context"]
            if value["project_id"] in {"task-manager", "task-manager-filter"}:
                path = "app.py"
                source = "def create_app(db_path):\n    return None\n" if "failed_test" not in ctx["previous"] else (
                    "def create_app(db_path):\n    return db_path\n")
            else:
                path = "pricing.py"
                source = ctx["source"]["pricing.py"].replace("price +", "price -")
            response = {"changes": [{"path": path, "after": source}],
                        "explanation": "Correct a sign"}
        else:
            result = value["inputs"]["context"]["previous"]["functional_tests"]
            assert result == "not_run" if value["project_id"] != "task-manager" else result["passed"] in (3, 4)
            response = {"summary": "Functional confirmation pending", "coaching": ["Use a decisive example"]}
        return {"text": json.dumps(response), "backend": "fixture"}


@contextmanager
def server_for(root, projects, cloud, worker=None):
    server = LocalServer(0, None)
    server.training = Training(root, server.token, include_ollama=True, include_team=True,
                               local_projects=projects, text_adapter_factory=lambda: cloud, engineering_worker=worker)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.03})
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()


def call(server, action, body=None):
    status, result = request(server, "/api/team/" + action, body)
    assert status == 200, result
    return result


def settle(server):
    for thread in tuple(server.training.threads.values()):
        thread.join(4)
    assert not server.training.threads
    return call(server, "state")["project_work"]["sessions"][0]


def approve(server, phase):
    approval = phase["approval"]
    return call(server, "decision", {"run_id": phase["id"], "approval_id": approval["id"],
                                     "spec_digest": approval["spec_digest"], "allow": True})


def draft(server):
    return call(server, "project-draft", {"job": "b" * 32, "project": "small-app", "task": "Correct a sign.",
                                          "acceptance": "Preserve signature.", "files": ["pricing.py"]})


def test_project_restores_handoffs_and_requires_real_approval(tmp_path, registry, monkeypatch):  # noqa: F811
    projects, path = registry
    baseline = (path / "pricing.py").read_bytes()
    projects = {"small-app": projects["pricing"]}
    model = mock_model(monkeypatch, [{"tool": "read_file", "args": {"path": "pricing.py", "line": 1},
                                    "plan": "Inspect the frozen source first."}])
    cloud, state = ProjectTextFixture(), tmp_path / "studio"
    with server_for(state, projects, cloud) as server:
        data = draft(server)
        assert not data["phases"] and not cloud.calls
        assert request(server, "/api/team/project-start", {"job": data["job"], "sha256": data["sha256"]}, trusted=False)[0] == 403
        data = call(server, "project-start", {"job": data["job"], "sha256": data["sha256"]})
        assert not cloud.calls
        approve(server, data["phases"]["plan"])
        data = settle(server)
        assert list(data["phases"]) == ["plan", "implement"]
        assert len(cloud.calls) == 1 and not model
    with server_for(state, projects, cloud) as server:
        history = call(server, "state")["project_work"]["sessions"][0]
        assert history["phases"]["implement"]["approval"]["status"] == "stale"
        data = call(server, "project-progress", {"job": history["job"]})
        approve(server, data["phases"]["implement"])
        data = settle(server)
        local = local_decision(data["local"])
        assert request(server, "/api/local-engine/decision", local)[0] == 200
        data = settle(server)
        assert len(model) == 1 and data["local"]["results"]["1"]["tool"] == "read_file"
        assert request(server, "/api/local-engine/decision", local)[0] == 409
        assert "apply" in data["phases"]
        approve(server, data["phases"]["apply"])
        data = settle(server)
        assert data["phases"]["apply"]["result"]["files"]["pricing.py"].startswith("def discounted")
        assert b"price +" in baseline and (path / "pricing.py").read_bytes() == baseline
        approve(server, data["phases"]["review"])
        data = settle(server)
        assert data["learning_status"] == "unreviewed" and data["functional_tests"] == "not_run"
        assert len(cloud.calls) == 3
        assert call(server, "project-stop", {"job": data["job"]})["stopped"]
        assert request(server, "/api/team/project-progress", {"job": data["job"]})[0] == 409


def test_project_unknown_blocks_and_requires_registered_files(tmp_path, registry):  # noqa: F811
    projects, _ = registry
    cloud = ProjectTextFixture()
    cloud.fail_phase = "plan"
    with server_for(tmp_path / "studio", {"small-app": projects["pricing"]}, cloud) as server:
        bad = {"job": "a" * 32, "project": "small-app", "task": "Read", "acceptance": "Check", "files": ["private.py"]}
        assert request(server, "/api/team/project-draft", bad)[0] == 409
        data = draft(server)
        data = call(server, "project-start", {"job": data["job"], "sha256": data["sha256"]})
        approve(server, data["phases"]["plan"])
        data = settle(server)
        assert data["phases"]["plan"]["status"] == "unknown"
        assert request(server, "/api/team/project-progress", {"job": data["job"]})[0] == 409
        assert request(server, "/api/team/project-recover", {"job": data["job"], "sha256": data["sha256"],
                       "previous_run": data["phases"]["plan"]["id"]})[0] == 409
        assert len(cloud.calls) == 1


class AppWorkerFixture(Executor):
    name = "engineering_worker"
    revision = "application-mock-only"

    def __init__(self, *, pass_first=False):
        self.calls = []
        self.pass_first = pass_first

    def approval_configuration(self):
        return {"mock": True}

    def execute(self, request):
        pkg = json.loads(request.action.parameters_json)["arguments"]["package"]
        assert pkg == app_package(pkg["code"], pkg["profile"])
        self.calls.append(pkg)
        total = len(pkg["cases"])
        passed = total if self.pass_first or pkg["profile"] == FILTER_PROFILE or len(self.calls) > 1 else 3
        results = {"method": pkg["profile"], "package_digest": pkg["digest"], "passed": passed, "total": total,
                   "cases": [{"id": case, "status": "passed" if i < passed else "failed",
                              "diagnostic": "ok" if i < passed else "response_mismatch"}
                             for i, case in enumerate(pkg["cases"])]}
        receipt = {"run_id": request.run_id, "stop_confirmed": True, "process_exit_code": 0,
                   "result": {"status": "ok", "stdout": canonical(results)}}
        return ExecutionResult((ProducedArtifact("result.json", canonical(receipt).encode()),), storage="sqlite")


def test_application_failure_allows_one_repair_with_new_worker_approval(tmp_path, registry, monkeypatch):  # noqa: F811
    projects, path = registry
    title, snapshot = projects["pricing"]
    registered = {"task-manager": (title, replace(snapshot, create_paths=("app.py",)))}
    calls = mock_model(monkeypatch, [{"tool": "git_diff", "args": {"path": "pricing.py"}, "plan": "Check baseline"}])
    cloud, worker = ProjectTextFixture(), AppWorkerFixture()
    with server_for(tmp_path / "studio", registered, cloud, worker) as server:
        data = call(server, "project-draft", {"job": "c" * 32, "project": "task-manager", "task": "Implement a WSGI app.",
                    "acceptance": "Use the fixed SQLite WSGI contract.", "files": ["pricing.py", "app.py"]})
        data = call(server, "project-start", {"job": data["job"], "sha256": data["sha256"]})
        for phase in ("plan", "implement"):
            approve(server, data["phases"][phase])
            data = settle(server)
        assert request(server, "/api/local-engine/decision", local_decision(data["local"]))[0] == 200
        data = settle(server)
        assert len(calls) == 1
        for phase in ("apply", "test", "repair", "apply_fix", "test_fix", "review"):
            approve(server, data["phases"][phase])
            data = settle(server)
        assert data["phases"]["test"]["result"]["passed"] == 3
        assert data["phases"]["test_fix"]["result"]["passed"] == 4
        assert data["phases"]["test"]["approval"]["id"] != data["phases"]["test_fix"]["approval"]["id"]
        assert len(cloud.calls) == 4 and len(worker.calls) == 2 and not (path / "app.py").exists()
        assert data["learning_status"] == "unreviewed"


def test_rejected_plan_recovery_preserves_budget_and_requires_renewal(tmp_path, registry, monkeypatch):  # noqa: F811
    projects, _ = registry
    title, snapshot = projects["pricing"]
    registered = {"task-manager": (title, replace(snapshot, create_paths=("app.py",)))}
    mock_model(monkeypatch, [{"tool": "git_diff", "args": {"path": "pricing.py"}, "plan": "Check baseline"}])
    cloud, worker = ProjectTextFixture(), AppWorkerFixture()
    state = tmp_path / "studio"
    with server_for(state, registered, cloud, worker) as server:
        data = call(server, "project-draft", {"job": "d" * 32, "project": "task-manager", "task": "Implement a WSGI app.",
                    "acceptance": "Use the fixed SQLite WSGI contract.", "files": ["pricing.py", "app.py"]})
        data = call(server, "project-start", {"job": data["job"], "sha256": data["sha256"]})
        recovery_request = {"job": data["job"], "sha256": data["sha256"], "previous_run": data["phases"]["plan"]["id"]}
        assert request(server, "/api/team/project-recover", recovery_request)[0] == 409
        cloud.reject_plan = True
        approve(server, data["phases"]["plan"])
        data = settle(server)
        original = data["phases"]["plan"]
        assert original["status"] == "succeeded" and original["result"]["state"] == "rejected"
        assert original["approval"]["status"] == "consumed"
        assert data["plan_recovery_available"] and set(data["phases"]) == {"plan"}

    class Future(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(hours=1)

    monkeypatch.setattr(project_work, "utc_now", lambda: Future.now(UTC).isoformat(timespec="microseconds"))
    monkeypatch.setattr(project_work, "datetime", Future)
    with server_for(state, registered, cloud, worker) as server:
        assert request(server, "/api/team/project-recover", recovery_request, trusted=False)[0] == 403
        data = call(server, "project-recover", recovery_request)
        recovery = data["phases"]["plan_recovery"]
        assert data["expired"] and data["renewal_available"]
        assert recovery["id"] != original["id"] and recovery["approval"]["id"] != original["approval"]["id"]
        assert call(server, "project-recover", recovery_request)["phases"]["plan_recovery"] == recovery
        permit = recovery["approval"]
        assert request(server, "/api/team/decision", {"run_id": recovery["id"], "approval_id": permit["id"],
                       "spec_digest": permit["spec_digest"], "allow": True})[0] == 409
        assert len(cloud.calls) == 1

    with server_for(state, registered, cloud, worker) as server:
        data = call(server, "project-start", {k: recovery_request[k] for k in ("job", "sha256")})
        assert not data["expired"] and not data["renewal_available"]
        assert data["phases"]["plan"] == original
        assert data["phases"]["plan_recovery"]["id"] == recovery["id"] and len(cloud.calls) == 1
        cloud.reject_plan = False
        approve(server, data["phases"]["plan_recovery"])
        data = settle(server)
        prior = cloud.calls[-1]["inputs"]["context"]["previous"]
        assert prior == {"rejected_plan": original["id"]}
        assert json.loads(project_contract.wire(cloud.calls[-1])[0])["context"]["previous"] == {}
        approve(server, data["phases"]["implement"])
        data = settle(server)
        assert cloud.calls[-1]["inputs"]["context"]["previous"]["architect"]["run"] == recovery["id"]
        assert request(server, "/api/local-engine/decision", local_decision(data["local"]))[0] == 200
        data = settle(server)
        for phase in ("apply", "test", "review"):
            approve(server, data["phases"][phase])
            data = settle(server)
        assert "repair" not in data["phases"] and data["functional_tests"]["passed"] == 3
        assert len(cloud.calls) == data["scope"]["cloud_calls"] == 4 and len(worker.calls) == 1


def test_project_response_limits_match_published_instructions():
    for role in ("project_architect", "project_reviewer"):
        assert "at most 180 characters" in project_contract.INSTRUCTIONS[role]
    for length in (180, 181, 233, 258):
        state = project_contract.assess({"phase": "plan"}, json.dumps({"plan": ["a" * length], "checks": ["check"]}))
        assert state["state"] == ("proposed" if length == 180 else "rejected")
    assert project_contract.assess({"phase": "plan"}, '{"plan":["valid"],"plan":["duplicate"],"checks":["valid"]}')["state"] == "rejected"
    review = {"phase": "review", "inputs": {"context": {"previous": {"functional_tests": "not_run"}}}}
    assert project_contract.assess(review, json.dumps({"summary": "x" * 700, "coaching": ["x" * 180]}))["state"] == "reviewed"
    assert project_contract.assess(review, json.dumps({"summary": "x" * 701, "coaching": ["check"]}))["state"] == "rejected"
    engineer = {"phase": "implement", "project_id": "small-app",
                "inputs": {"context": {"allowed": ["app.py"], "source": {}, "snapshot": "a" * 64}}}
    valid = {"changes": [{"path": "app.py", "after": "pass\n"}], "explanation": "a" * 400}
    assert project_contract.assess(engineer, json.dumps(valid))["state"] == "proposed"
    assert project_contract.assess(engineer, json.dumps({**valid, "explanation": "a" * 401}))["state"] == "rejected"
    assert project_contract.assess(engineer, json.dumps({**valid, "changes": [{"path": "app.py", "after": "# " + "é" * 2200}]}))["state"] == "rejected"
    assert project_contract.assess(engineer, json.dumps({**valid, "changes": valid["changes"] * 2}))["state"] == "rejected"


def test_filter_project_hands_failed_local_prediction_to_engineer(tmp_path, registry, monkeypatch):  # noqa: F811
    _, path = registry
    source = ("def create_app(db_path):\n    def app(environ, start_response):\n"
              "        method = environ['REQUEST_METHOD']\n        path = environ['PATH_INFO']\n"
              "        if method == 'GET' and path in ('/', '/tasks.json'):\n"
              "            return []\n    return app\n")
    (path / "app.py").write_bytes(source.encode())
    subprocess.run(["git", "-C", str(path), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "Accepted fixture baseline"], check=True)
    head = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    snap = Snapshot.register(path, base=head, revision=head, paths=["app.py"])
    calls = mock_model(monkeypatch, [skill_reply("1")])
    cloud = ProjectTextFixture()
    with server_for(tmp_path / "filter-studio", {"task-manager-filter": ("Filter", snap)}, cloud) as server:
        data = call(server, "project-draft", {"job": "f" * 32, "project": "task-manager-filter", "task": "Filter tasks.",
                    "acceptance": "Preserve defaults; filter GET.", "files": ["app.py"]})
        assert data["scope"]["functional_profile"] == "task-manager-filter-wsgi-v1"
        data = call(server, "project-start", {"job": data["job"], "sha256": data["sha256"]})
        approve(server, data["phases"]["plan"])
        data = settle(server)
        assert "implement" not in data["phases"] and not calls
        assert request(server, "/api/local-engine/decision", local_decision(data["local"]))[0] == 200
        data = settle(server)
        message = json.loads(calls[0]["messages"][1]["content"])
        assert message["source"] in source and "method == 'GET'" in message["source"]
        assert "candidate" not in message and "verified_reference_data" not in message
        assert len(cloud.calls) == 1 and data["phases"]["implement"]["approval"]["status"] == "pending"
        approve(server, data["phases"]["implement"])
        settle(server)
        local = cloud.calls[-1]["inputs"]["context"]["previous"]["local"]
        assert local["prediction"] == "1" and local["matches_probe"] is False
        assert (path / "app.py").read_bytes() == source.encode()


def fragment(path, source, before, after):
    return {"path": path, "before_sha256": sha(source), "before": before, "after": after}


def test_fragment_contract_compares_identical_candidates_without_raising_budgets():
    source = "# " + "x" * 3500 + "\nfirst = 1\nlast = 2\n"
    baseline = {"phase": "implement", "project_id": "small-app", "text": "Update assignments",
                "inputs": {"context": {"allowed": ["app.py"], "source": {"app.py": source}, "snapshot": "a" * 64,
                                       "previous": {"architect": {}}, "acceptance": "Keep scope"}}}
    trial = {**baseline, "inputs": {"context": {**baseline["inputs"]["context"],
                                                "previous": {"edit_protocol": project_contract.FRAGMENTS}}}}
    prompt, role = project_contract.wire(trial)
    assert role == "project_engineer_fragments"
    assert json.loads(prompt)["source_budgets"]["app.py"]["sha256"] == sha(source)
    assert "4096 characters" in project_contract.INSTRUCTIONS[role]
    assert "4400 UTF-8 bytes" in project_contract.INSTRUCTIONS[role]
    for replacement, status in (("first = 3", "proposed"), ("#" + "x" * 1000, "rejected"),
                                ("#" + "é" * 550, "rejected")):
        edits = [fragment("app.py", source, "last = 2", "last = 4"),
                 fragment("app.py", source, "first = 1", replacement)]
        after = source.replace("first = 1", replacement).replace("last = 2", "last = 4")
        full = canonical({"changes": [{"path": "app.py", "after": after}], "explanation": "Update assignments"})
        compact = canonical({"edits": edits, "explanation": "Update assignments"})
        assert len(compact.encode()) < len(full.encode())
        assert project_contract.assess(baseline, full) == project_contract.assess(trial, compact)
        assert project_contract.assess(trial, compact)["state"] == status
        assert project_contract.assess(trial, full)["state"] == "rejected"


@pytest.mark.parametrize("failure", ["hash", "missing", "ambiguous", "overlap", "path", "syntax", "create"])
def test_fragment_contract_rejects_invalid_anchors_and_scope(failure):
    source = "x = 1\ny = 1\n"
    edit = fragment("app.py", source, "x = 1", "x = 2")
    edits = [edit]
    if failure == "hash":
        edit["before_sha256"] = "b" * 64
    elif failure == "missing":
        edit["before"] = "z = 9"
    elif failure == "ambiguous":
        edit["before"] = "= 1"
    elif failure == "overlap":
        edits.append(fragment("app.py", source, "1\ny", "2\ny"))
    elif failure == "path":
        edit["path"] = "other.py"
    elif failure == "create":
        edit["path"] = "new.py"
    else:
        edit["after"] = "x = (("
    request_data = {"phase": "implement", "project_id": "small-app",
                    "inputs": {"context": {"allowed": ["app.py", "new.py"], "source": {"app.py": source},
                                           "snapshot": "a" * 64,
                                           "previous": {"edit_protocol": project_contract.FRAGMENTS}}}}
    assert project_contract.assess(request_data, canonical({"edits": edits, "explanation": "attempt"}))["state"] == "rejected"


class FilterContinuationCloud(ProjectTextFixture):
    def run(self, value, emit):
        if value["phase"] == "implement":
            self.calls.append(value)
            ctx = value["inputs"]["context"]
            if ctx["previous"].get("edit_protocol") == project_contract.FRAGMENTS:
                if self.fail_phase == "implement":
                    raise RuntimeError("Lost fixture outcome")
                answer = {"edits": [fragment("app.py", ctx["source"]["app.py"], "return []", "return [b'fixture']")],
                          "explanation": "Synthetic candidate"}
            else:
                answer = {"changes": [{"path": "app.py", "after": "#" + "x" * 4493}], "explanation": "Oversized"}
            return {"text": canonical(answer), "backend": "fixture"}
        if value["phase"] == "review":
            self.calls.append(value)
            assert value["inputs"]["context"]["previous"]["functional_tests"]["passed"] == 7
            return {"text": canonical({"summary": "Mock acceptance", "coaching": ["Inspect the fixture"]}), "backend": "fixture"}
        return super().run(value, emit)


def test_filter_continuation_reuses_sealed_history_and_requires_new_approvals(tmp_path, registry, monkeypatch):  # noqa: F811
    _, path = registry
    source = ("def create_app(db_path):\n    def app(environ, start_response):\n"
              "        method = environ['REQUEST_METHOD']\n        path = environ['PATH_INFO']\n"
              "        if method == 'GET' and path in ('/', '/tasks.json'):\n"
              "            return []\n    return app\n")
    (path / "app.py").write_bytes(source.encode())
    subprocess.run(["git", "-C", str(path), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "Accepted fixture baseline"], check=True)
    head = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    projects = {"task-manager-filter": ("Filter", Snapshot.register(path, base=head, revision=head, paths=["app.py"]))}
    calls = mock_model(monkeypatch, [skill_reply("2")])
    cloud, worker, root = FilterContinuationCloud(), AppWorkerFixture(), tmp_path / "continuation"
    with server_for(root, projects, cloud, worker) as server:
        old = call(server, "project-draft", {"job": "e" * 32, "project": "task-manager-filter", "task": "Filter.",
                   "acceptance": "Preserve cases.", "files": ["app.py"]})
        old = call(server, "project-start", {"job": old["job"], "sha256": old["sha256"]})
        approve(server, old["phases"]["plan"])
        old = settle(server)
        assert request(server, "/api/local-engine/decision", local_decision(old["local"]))[0] == 200
        old = settle(server)
        approve(server, old["phases"]["implement"])
        old = settle(server)
        assert old["filter_continuation_available"] and "apply" not in old["phases"]
        body = {"job": "f" * 32, "resume_from": old["job"]}
        new = call(server, "project-draft", body)
        assert call(server, "project-draft", body) == new
        assert request(server, "/api/team/project-draft", {**body, "job": "c" * 32})[0] == 409
        assert request(server, "/api/team/project-progress", {"job": new["job"]})[0] == 409
        assert not new["phases"] and not new["consent"] and new["scope"]["local_steps"] == 0
        assert new["scope"]["cloud_calls"] == 2 and len(cloud.calls) == 2 and len(calls) == 1

    with server_for(root, projects, cloud, worker) as server:
        assert settle(server)["sha256"] == new["sha256"]
        new = call(server, "project-start", {"job": new["job"], "sha256": new["sha256"]})
        assert set(new["phases"]) == {"implement"}
        assert new["phases"]["implement"]["approval"]["id"] != old["phases"]["implement"]["approval"]["id"]
        for phase in ("implement", "apply", "test", "review"):
            pending = new["phases"][phase]
            assert pending["status"] == "created" and pending["approval"]["status"] == "pending"
            approve(server, pending)
            new = settle(server)
            if phase == "implement":
                assert "+            return [b'fixture']" in new["phases"][phase]["diff"]
            if phase == "apply":
                backup = project_work.artifact_json(server.training.team.core, pending["id"], "backup.json")
                assert backup["files"]["app.py"] == source and backup["sha256"]["app.py"] == sha(source)
        assert new["functional_tests"]["passed"] == 7 and new["learning_status"] == "unreviewed"
        assert len(worker.calls) == 1 and worker.calls[0]["cases"] == list(PROFILES[FILTER_PROFILE])
        assert len(cloud.calls) == 4 and len(calls) == 1
        historical = next(s for s in call(server, "state")["project_work"]["sessions"] if s["job"] == old["job"])
        assert historical["phases"] == old["phases"]
        assert (path / "app.py").read_bytes() == source.encode()


def test_abandoned_fragment_unknown_preserves_history_and_opens_one_new_draft(tmp_path, registry, monkeypatch):  # noqa: F811
    from ii_pro_core.errors import Conflict, RecoveryRequired

    _, path = registry
    source = ("def create_app(db_path):\n    def app(environ, start_response):\n"
              "        method = environ['REQUEST_METHOD']\n        path = environ['PATH_INFO']\n"
              "        if method == 'GET' and path in ('/', '/tasks.json'):\n"
              "            return []\n    return app\n")
    (path / "app.py").write_bytes(source.encode())
    subprocess.run(["git", "-C", str(path), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "Fixture"], check=True)
    head = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    projects = {"task-manager-filter": ("Filter", Snapshot.register(path, base=head, revision=head, paths=["app.py"]))}
    mock_model(monkeypatch, [skill_reply("2")])
    cloud = FilterContinuationCloud()
    root = tmp_path / "studio"
    with server_for(root, projects, cloud) as server:
        first = call(server, "project-draft", {"job": "a" * 32, "project": "task-manager-filter", "task": "Filter.",
                     "acceptance": "Preserve the fixed cases.", "files": ["app.py"]})
        first = call(server, "project-start", {"job": first["job"], "sha256": first["sha256"]})
        approve(server, first["phases"]["plan"])
        first = settle(server)
        assert request(server, "/api/local-engine/decision", local_decision(first["local"]))[0] == 200
        first = settle(server)
        approve(server, first["phases"]["implement"])
        settle(server)
        lost = call(server, "project-draft", {"job": "b" * 32, "resume_from": first["job"]})
        lost = call(server, "project-start", {"job": lost["job"], "sha256": lost["sha256"]})
        cloud.fail_phase = "implement"
        approve(server, lost["phases"]["implement"])
        lost = settle(server)
        phase = lost["phases"]["implement"]
        assert phase["status"] == "unknown" and phase["approval"]["status"] == "consumed"
        body = {"job": "c" * 32, "resume_from": lost["job"]}
        assert request(server, "/api/team/project-draft", body)[0] == 409
        team = server.training.team
        existing = team.core.runs.get(phase["id"])
        with pytest.raises(RecoveryRequired):
            team.abandon_unknown(existing.id, action_hash="0" * 64, accept_unknown_cost=True, no_retry=True)
        team.abandon_unknown(existing.id, action_hash=existing.action_hash, accept_unknown_cost=True, no_retry=True)
        assert team.core.runs.get(existing.id) == existing
    with server_for(root, projects, cloud) as server:
        restored = settle(server)
        assert restored["filter_continuation_available"]
        new = call(server, "project-draft", body)
        assert request(server, "/api/team/project-draft", {**body, "job": "d" * 32})[0] == 409
        assert not new["consent"] and not new["phases"]
        assert new["scope"]["restart_of"] == lost["job"] and new["scope"]["resume_from"] == first["job"]
        assert len(cloud.calls) == 3
        team = server.training.team
        with pytest.raises(Conflict):
            team.projects.prepare_extended_engineer(job=new["job"], original_sha256=new["sha256"], seconds=180)
        prepared = team.projects.prepare_extended_engineer(job=new["job"], original_sha256=new["sha256"], seconds=120)
        assert prepared["sha256"] != new["sha256"] and not prepared["consent"]
        pending = prepared["phases"]["implement"]
        run = team.core.runs.get(pending["id"])
        value = json.loads(run.description)
        assert value["inputs"]["deadline_seconds"] == 120
        assert value["inputs"]["scope"] == prepared["sha256"]
        assert request(server, "/api/team/project-start", {"job": new["job"], "sha256": new["sha256"]})[0] == 409
        auth = pending["approval"]
        assert request(server, "/api/team/decision", {"run_id": run.id, "approval_id": auth["id"],
                       "spec_digest": auth["spec_digest"], "allow": True})[0] == 409
        started = call(server, "project-start", {"job": prepared["job"], "sha256": prepared["sha256"]})
        assert started["consent"] and started["phases"]["implement"]["approval"]["status"] == "pending"
        assert team.projects.permits(run.id, value)
        assert not team.projects.permits(run.id, {**value, "inputs": {k: v for k, v in value["inputs"].items() if k != "deadline_seconds"}})
        assert len(cloud.calls) == 3
