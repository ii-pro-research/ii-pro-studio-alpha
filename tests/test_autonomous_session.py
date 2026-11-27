import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from test_local_engine_ui import mock_model, registry, request  # noqa: F401
from test_project_work import AppWorkerFixture, ProjectTextFixture, call, server_for
from test_skills import reply

from ii_pro_core.errors import Conflict, RecoveryRequired
from ii_pro_core.security import canonical
from ii_pro_local import table_probe
from ii_pro_local.engineering_executor import artifact_json, sha
from ii_pro_local.snapshot import Snapshot
from ii_pro_school.adapters import StudentFailure
from ii_pro_team import autonomous, project_work
from ii_pro_worker.app_contract import PROFILES, SORT_PROFILE

BASE = "def create_app(db_path):\n    return 'original'\n"
JOB = "a" * 32


class HeldCloud(ProjectTextFixture):
    def __init__(self):
        super().__init__()
        self.entered, self.release = Event(), Event()

    def run(self, value, emit):
        if value["phase"] == "plan":
            self.entered.set()
            assert self.release.wait(8)
        return super().run(value, emit)


class ForbiddenCloud(ProjectTextFixture):
    def run(self, value, emit):
        result = super().run(value, emit)
        if value["phase"] == "implement":
            body = json.loads(result["text"])
            body["changes"][0]["path"] = "elsewhere.py"
            result["text"] = canonical(body)
        return result


@pytest.fixture
def app_registry(registry):  # noqa: F811
    import subprocess

    _, path = registry
    (path / "app.py").write_bytes(BASE.encode())
    subprocess.run(["git", "-C", str(path), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "Approved baseline"], check=True)
    head = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    snap = Snapshot.register(path, base=head, revision=head, paths=["app.py"])
    return {"task-manager": ("Application fixture", snap)}, path


def draft(server, *, corrections=0, seconds=1800):
    return call(server, "project-draft", {"job": JOB, "project": "task-manager", "task": "Improve the test fixture.",
               "acceptance": "Preserve the fixed application contract.", "files": ["app.py"],
               "autonomous": {"duration_seconds": seconds, "corrections": corrections}})


def start(server, frozen):
    return call(server, "project-start", {"job": JOB, "sha256": frozen["sha256"]})


def history(server):
    return call(server, "state")["project_work"]["sessions"][0]


def settle(server, *, terminal=True):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        state = history(server)
        if not server.training.threads and (not terminal or state["autonomy"]["status"] != "active"):
            return state
        time.sleep(.03)
    pytest.fail("Session did not settle")


def names(value):
    return [x["payload_json"]["slot"] for x in value["autonomy"]["journal"] if x["kind"] == "approval.derived"]


def test_single_consent_dispatches_and_repairs_then_reviews_candidate(tmp_path, app_registry, monkeypatch):
    projects, path = app_registry
    model = mock_model(monkeypatch, [reply("observed")])
    cloud, worker = ProjectTextFixture(), AppWorkerFixture()
    with server_for(tmp_path/"studio", projects, cloud, worker) as server:
        data = draft(server, corrections=1)
        assert not cloud.calls and not data["phases"] and data["autonomy"]["status"] == "draft"
        assert request(server, "/api/team/project-start", {"job": JOB, "sha256": data["sha256"]}, trusted=False)[0] == 403
        start(server, data)
        final = settle(server)
        assert final["autonomy"]["status"] == "completed"
        assert final["functional_tests"]["passed"] == 4
        assert names(final) == ["plan", "local", "implement", "apply", "test", "rollback", "repair", "apply_fix", "test_fix", "review"]
        assert len(cloud.calls) == 4 and len(model) == 1 and len(worker.calls) == 2
        reviewed = cloud.calls[-1]["inputs"]["context"]
        assert reviewed["source"]["app.py"] == worker.calls[-1]["code"]
        assert "return db_path" in reviewed["previous"]["diff_excerpt"]
        assert reviewed["previous"]["functional_tests"] == final["functional_tests"]
        rolled = artifact_json(server.training.shared_core, final["phases"]["rollback"]["id"], "working-copy.json")
        assert rolled["files"]["app.py"] == BASE and (path / "app.py").read_bytes() == BASE.encode()
        logs = server.training.shared_core.store.all("SELECT kind FROM events WHERE kind IN ('approval.decided','team.project_started')")
        assert [r[0] for r in logs] == ["team.project_started"]
        assert request(server, "/api/team/project-start", {"job": JOB, "sha256": data["sha256"]})[0] == 409


def test_no_repair_budget_rolls_back_and_stops(tmp_path, app_registry, monkeypatch):
    projects, path = app_registry
    mock_model(monkeypatch, [reply("observed")])
    cloud, worker = ProjectTextFixture(), AppWorkerFixture()
    with server_for(tmp_path/"studio", projects, cloud, worker) as server:
        start(server, draft(server))
        final = settle(server)
        assert final["autonomy"]["status"] == "budget"
        assert final["phases"]["rollback"]["status"] == "succeeded"
        assert final["functional_tests"]["passed"]==3
        assert len(cloud.calls) == 2 and len(worker.calls) == 1
        assert (path/"app.py").read_bytes() == BASE.encode()


def test_first_pass_does_not_spend_the_correction_slot(tmp_path, app_registry, monkeypatch):
    projects, _ = app_registry
    local = mock_model(monkeypatch, [reply("observed")])
    cloud, worker = ProjectTextFixture(), AppWorkerFixture(pass_first=True)
    with server_for(tmp_path/"studio", projects, cloud, worker) as server:
        start(server, draft(server, corrections=1))
        final = settle(server)
        assert final["autonomy"]["status"] == "completed"
        assert names(final) == ["plan", "local", "implement", "apply", "test", "review"]
        assert final["autonomy"]["remaining"] == {"cloud": 1, "local": 0, "copies": 1, "worker": 1, "rollbacks": 2}
        assert len(cloud.calls) == 3 and len(local) == len(worker.calls) == 1


@pytest.mark.parametrize(("instrument", "repair"), [(None, False), (None, True), ("ok", False), ("ok", True),
                                                      ("wrong_dataset", False), ("wrong_tool", False), ("unknown", False),
                                                      ("extended", True), ("cloud_unknown", False), ("short_window", False),
                                                      ("size_chars", False), ("size_bytes", False),
                                                      ("size_twice", False), ("size_worker_fail", True),
                                                      ("size_unknown", False), ("size_path", False)])
def test_sort_session_binds_fragments_and_review_code(tmp_path, app_registry, monkeypatch, instrument, repair):
    _, path = app_registry
    source = ("def create_app(db_path):\n    def app(environ, respond):\n"
              "        method = environ['REQUEST_METHOD']\n        path = '/'\n"
              "        if method == 'GET' and path in ('/', '/tasks.json'):\n"
              "            return []\n    return app\n")
    (path / "app.py").write_bytes(source.encode())
    subprocess.run(["git", "-C", str(path), "commit", "-qam", "Sort grader baseline"], check=True)
    head = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    projects = {project_work.SORT_PROJECT: ("Sort fixture", Snapshot.register(path, base=head, revision=head, paths=["app.py"]))}
    monkeypatch.setattr(project_work, "SORT_BASELINE", sha(source))
    proposal = {"tool": "table_check", "plan": "Check open titles before proposing SQL.",
                "args": {"dataset": table_probe.DATASET if instrument != "wrong_dataset" else table_probe.GROUNDED_DATASET,
                         "filters": [], "sort": [{"field": "zone", "direction": "asc"}]}}
    if instrument == "wrong_tool":
        proposal = {"tool": "git_status", "args": {}, "plan": "Unexpected tool."}
    answer = StudentFailure("timeout") if instrument == "unknown" else proposal if instrument else reply("observed")
    model = mock_model(monkeypatch, [answer])
    size_trial = bool(instrument and instrument.startswith("size_"))
    extended = instrument in {"extended", "cloud_unknown", "short_window"} or size_trial
    compact = "return () if environ else []"
    oversized = compact + "\n        # " + ("x" * 3200 + "界" * 400 if instrument == "size_bytes" else "x" * 3910)
    rejected = source.replace("return []", oversized)

    if instrument == "short_window":
        from datetime import datetime, timedelta

        authorize = autonomous.AutonomousSession.authorize

        def near_expiry(session, conn, run, proof, *, reserve):
            if proof["slot"] != "implement":
                return authorize(session, conn, run, proof, reserve=reserve)
            grant = session.project._grant(session.project._events(proof["job"]))

            class NearExpiry(datetime):
                @classmethod
                def now(cls, tz=None):
                    return datetime.fromisoformat(grant["expires_at"]) - timedelta(seconds=150)

            with monkeypatch.context() as temporary:
                temporary.setattr(autonomous, "datetime", NearExpiry)
                return authorize(session, conn, run, proof, reserve=reserve)

        monkeypatch.setattr(autonomous.AutonomousSession, "authorize", near_expiry)

    class Cloud(ProjectTextFixture):
        def ready_for_deadline(self, seconds):
            return seconds == 180

        def run(self, value, emit):
            if value["phase"] == "plan":
                assert "deadline_seconds" not in value["inputs"]
                return super().run(value, emit)
            self.calls.append(value)
            previous = value["inputs"]["context"]["previous"]
            if value["phase"] == "implement":
                assert previous["edit_protocol"] == "fragments-v1"
                local = previous["local"]
                assert value["inputs"].get("deadline_seconds") == (180 if extended else None)
                if instrument == "cloud_unknown":
                    raise RuntimeError("Remote outcome unknown")
                if instrument in {"ok", "extended"} or size_trial:
                    receipt = local["observation"]
                    assert receipt == table_probe.evaluate(proposal["args"])
                    assert local["hypothesis"] == proposal["plan"] and local["parameters"] == proposal["args"]
                    assert local["engineering_assessment"] == "not_performed"
                    assert local["status"] == "succeeded" and local["outcome"] == "observed"
                    assert local["task"] == table_probe.PROJECT_TASK
                elif instrument:
                    assert local["outcome"] == "rejected" and "observation" not in local
                else:
                    assert local["outcome"] == "observed"
                if "failed_test" in previous:
                    assert "sort_title_json" in previous["failed_test"]["cases"]
                if size_trial and "size_repair" in previous:
                    assert previous["size_repair"]["reason"] == "candidate_exceeds_size"
                    assert previous["size_repair"]["size"] == {"characters": len(rejected),
                        "bytes": len(rejected.encode()), "max_characters": 4096, "max_bytes": 4400}
                    assert value["inputs"]["context"]["source"] == {"app.py": rejected}
                    if instrument == "size_unknown":
                        raise RuntimeError("Remote outcome unknown")
                    before, after, baseline = oversized, (oversized + "x" if instrument == "size_twice" else compact), rejected
                else:
                    before, after, baseline = "return []", (oversized if size_trial else compact), source
                output = {"edits": [{"path": "other.py" if instrument == "size_path" else "app.py",
                                     "before_sha256": sha(baseline), "before": before, "after": after}],
                          "explanation": "Mock change"}
            else:
                assert "deadline_seconds" not in value["inputs"]
                assert set(previous["functional_tests"]["cases"]) == set(PROFILES[SORT_PROFILE])
                assert "return ()" in value["inputs"]["context"]["source"]["app.py"]
                output = {"summary": "Review mock evidence", "coaching": ["Check ordering"]}
            return {"text": json.dumps(output), "backend": "fixture"}

    cloud, worker = Cloud(), AppWorkerFixture(pass_first=not repair)
    with server_for(tmp_path / "studio-sort", projects, cloud, worker) as server:
        frozen = call(server, "project-draft", {"job": JOB, "project": project_work.SORT_PROJECT,
                      "task": "Add title ordering.", "acceptance": "Keep the fixed sort and filter contract.",
                      "files": ["app.py"], "autonomous": {"duration_seconds": 1800, "corrections": 1,
                      **({"engineer_deadline_seconds": 180} if extended else {}),
                      **({"local_tool": "table_check"} if instrument else {})}})
        assert frozen["scope"]["acceptance_cases"] == list(PROFILES[SORT_PROFILE])
        if extended:
            assert frozen["scope"]["engineer_deadline_seconds"] == 180
            assert frozen["scope"]["autonomous"]["engineer_text_idle_seconds"] == 90
        start(server, frozen)
        final = settle(server)
        if instrument:
            assert frozen["scope"]["autonomous"]["table_check"] == table_probe.project_binding(JOB)
            assert len(model) == 1 and names(final).count("local") == 1
            assert model[0]["format"]["oneOf"][0]["properties"]["tool"]["enum"] == ["table_check"]
        if instrument == "unknown":
            assert final["autonomy"]["status"] == "unknown" and names(final) == ["plan", "local"]
            assert len(cloud.calls) == 1 and not worker.calls and final["local"]["status"] == "unknown"
            return
        if instrument == "short_window":
            assert final["autonomy"]["status"] == "constraint" and names(final) == ["plan", "local"]
            assert len(cloud.calls) == 1 and not worker.calls
            return
        if instrument == "cloud_unknown":
            team = server.training.team
            core = team.core
            phase = final["phases"]["implement"]
            run = core.runs.get(phase["id"])
            approval = phase["approval"]["id"]
            query = "SELECT * FROM events WHERE run_id=? ORDER BY id"
            journal = core.store.all(query, (run.id,))
            assert team.blocked() and run.status == "unknown" and not run.execution_stopped
            with pytest.raises(RecoveryRequired):
                team.abandon_unknown(run.id, action_hash="0" * 64, accept_unknown_cost=True, no_retry=True)
            team.abandon_unknown(run.id, action_hash=run.action_hash, accept_unknown_cost=True, no_retry=True)
            assert not team.blocked() and core.runs.get(run.id) == run
            assert core.store.all(query, (run.id,)) == journal
            assert core.approvals.get(approval).status == "consumed"
            assert request(server, "/api/team/project-start", {"job": JOB, "sha256": frozen["sha256"]})[0] == 409
            assert call(server, "project-progress", {"job": JOB})["autonomy"]["status"] == "unknown"
            assert len(cloud.calls) == 2 and not worker.calls
            return
        if size_trial:
            phase = final["phases"]["implement"]
            assert phase["status"] == "succeeded" and phase["result"]["state"] == "rejected"
            assert "apply" not in final["phases"] and "test" not in final["phases"]
            if instrument == "size_path":
                assert names(final) == ["plan", "local", "implement"] and not worker.calls
                assert final["autonomy"]["status"] == "failed"
                return
            assert phase["result"]["candidate"] == rejected
            assert names(final)[:4] == ["plan", "local", "implement", "repair"]
            assert names(final).count("repair") == 1
            if instrument in {"size_twice", "size_unknown"}:
                assert not worker.calls and len(cloud.calls) == 3
                assert final["autonomy"]["status"] == ("unknown" if instrument == "size_unknown" else "failed")
                return
            assert len(worker.calls) == 1 and worker.calls[0]["code"] == source.replace("return []", compact)
            assert (path / "app.py").read_bytes() == source.encode()
            if instrument == "size_worker_fail":
                assert names(final)[4:] == ["apply_fix", "test_fix", "rollback_fix"]
                assert final["autonomy"]["status"] == "budget" and len(cloud.calls) == 3
                return
            assert names(final)[4:] == ["apply_fix", "test_fix", "review"]
            assert final["functional_tests"]["passed"] == 11 and final["autonomy"]["status"] == "completed"
            assert cloud.calls[-1]["inputs"]["context"]["previous"]["initial_rejection"]["run"] == phase["id"]
            return
        if instrument == "ok":
            core = server.training.shared_core
            evidence = cloud.calls[1]["inputs"]["context"]["previous"]["local"]
            proof = core.artifacts.get(evidence["artifact"]["id"])
            assert proof.run_id == evidence["run"] and proof.sha256 == evidence["artifact"]["sha256"]
            assert artifact_json(core, proof.run_id, "tool-result.json")["result"] == evidence["observation"]
            forged = json.loads(json.dumps(cloud.calls[1]))
            forged["inputs"]["context"]["previous"]["local"]["artifact"]["sha256"] = "0" * 64
            assert not server.training.team.projects.permits(final["phases"]["implement"]["id"], forged)
            assert len(final["local"]["runs"]) == 1
            with pytest.raises(Conflict):
                server.training.local_engine.agents[project_work.SORT_PROJECT].prepare(
                    final["local"]["job"], task=table_probe.PROJECT_TASK, requirements=table_probe.PROJECT_REQUIREMENTS,
                    project_table=table_probe.project_binding(JOB))
        assert final["autonomy"]["status"] == "completed"
        assert final["functional_tests"]["passed"] == final["functional_tests"]["total"] == 11
        assert len(cloud.calls) == 3 + repair and len(worker.calls) == 1 + repair and len(model) == 1
        assert (path / "app.py").read_bytes() == source.encode()


@pytest.mark.parametrize("failure,terminal", [("unknown", "unknown"), ("path", "failed")])
def test_unknown_or_outside_paths_prevent_apply(tmp_path, app_registry, monkeypatch, failure, terminal):
    projects, _ = app_registry
    mock_model(monkeypatch, [reply("observed")])
    cloud = ForbiddenCloud() if failure == "path" else ProjectTextFixture()
    if failure == "unknown":
        cloud.fail_phase = "implement"
    worker = AppWorkerFixture()
    with server_for(tmp_path/"studio", projects, cloud, worker) as server:
        start(server, draft(server))
        final = settle(server)
        assert final["autonomy"]["status"] == terminal
        assert "apply" not in final["phases"] and not worker.calls
        if failure == "unknown":
            assert final["phases"]["implement"]["status"] == "unknown"
        call(server, "project-progress", {"job": JOB})
        assert len(cloud.calls) == 2


def test_local_unknown_stops_autonomy_and_retains_safe_diagnostic(tmp_path, app_registry, monkeypatch):
    projects, _ = app_registry
    local = mock_model(monkeypatch, [StudentFailure("timeout")])
    cloud, worker = ProjectTextFixture(), AppWorkerFixture()
    with server_for(tmp_path / "studio-local-unknown", projects, cloud, worker) as server:
        start(server, draft(server))
        final = settle(server)
        assert final["autonomy"]["status"] == "unknown"
        assert names(final) == ["plan", "local"]
        assert list(final["phases"]) == ["plan"] and not worker.calls
        run_id = final["local"]["runs"][0]["id"]
        core = server.training.shared_core
        assert artifact_json(core, run_id, "diagnostic.json")["classification"] == "timeout"
        assert not core.runs.get(run_id).execution_stopped
        assert call(server, "project-progress", {"job": JOB})["autonomy"]["status"] == "unknown"
    assert len(cloud.calls) == len(local) == 1


@pytest.mark.parametrize("control", ["pause", "revoke", "stop"])
def test_operator_control_fences_dispatch_between_operations(tmp_path, app_registry, monkeypatch, control):
    projects, _ = app_registry
    local = mock_model(monkeypatch, [reply("observed")])
    cloud = HeldCloud()
    try:
        with server_for(tmp_path/"studio", projects, cloud, AppWorkerFixture()) as server:
            start(server, draft(server))
            try:
                assert cloud.entered.wait(5)
                call(server, "project-control", {"job": JOB, "control": control})
            finally:
                cloud.release.set()
            final = settle(server)
            assert final["autonomy"]["status"] == {"pause":"paused","revoke":"revoked","stop":"stopped"}[control]
            assert names(final) == ["plan"] and not local
            if control == "pause":
                call(server, "project-control", {"job": JOB, "control": "resume"})
                assert "local" in names(settle(server))
            else:
                assert request(server, "/api/team/project-control", {"job": JOB, "control": "resume"})[0] == 409
    finally:
        cloud.release.set()


def test_too_short_permission_never_dispatches(tmp_path, app_registry):
    projects, _ = app_registry
    cloud = ProjectTextFixture()
    with server_for(tmp_path/"studio", projects, cloud, AppWorkerFixture()) as server:
        start(server, draft(server, seconds=60))
        final = settle(server)
        assert final["autonomy"]["status"] == "constraint" and not names(final) and not cloud.calls
        pending = final["phases"]["plan"]["approval"]
        with pytest.raises(Conflict):
            server.training.shared_core.approvals.decide(pending["id"], allow=True)


def test_two_concurrent_starts_use_one_plan_slot(tmp_path, app_registry):
    projects, _ = app_registry
    cloud = HeldCloud()
    with server_for(tmp_path/"studio", projects, cloud, AppWorkerFixture()) as server:
        snapshot = draft(server)
        try:
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda _: request(server, "/api/team/project-start",
                                                         {"job": JOB, "sha256": snapshot["sha256"]})[0], range(2)))
            assert sorted(results) == [200, 409] and cloud.entered.wait(5)
            call(server, "project-control", {"job": JOB, "control": "revoke"})
        finally:
            cloud.release.set()
        assert names(settle(server)) == ["plan"] and len(cloud.calls) == 1

def test_expiry_after_plan_blocks_followup(tmp_path, app_registry, monkeypatch):
    from ii_pro_team import project_work

    projects, _ = app_registry
    local = mock_model(monkeypatch, [reply("observed")])
    cloud = HeldCloud()
    with server_for(tmp_path/"studio", projects, cloud, AppWorkerFixture()) as server:
        start(server, draft(server))
        try:
            assert cloud.entered.wait(5)
            monkeypatch.setattr(project_work, "utc_now", lambda: "9999-12-31T00:00:00.000000+00:00")
        finally:
            cloud.release.set()
        final = settle(server)
        assert final["autonomy"]["status"] == "expired"
        assert names(final) == ["plan"] and not local


def test_revocation_after_derivation_prevents_core_consumption(tmp_path, app_registry, monkeypatch):
    from ii_pro_team.team import Team

    projects, _ = app_registry
    cloud = ProjectTextFixture()
    entered, release = Event(), Event()
    original = Team._execute

    def hold(team, run_id, approval_id):
        entered.set()
        assert release.wait(5)
        original(team, run_id, approval_id)

    monkeypatch.setattr(Team, "_execute", hold)
    with server_for(tmp_path/"studio", projects, cloud, AppWorkerFixture()) as server:
        start(server, draft(server))
        try:
            assert entered.wait(5)
            call(server, "project-control", {"job": JOB, "control": "revoke"})
        finally:
            release.set()
        final = settle(server)
        assert final["autonomy"]["status"] == "revoked" and not cloud.calls
        assert not server.training.shared_core.store.all("SELECT 1 FROM events WHERE kind='approval.consumed'")
