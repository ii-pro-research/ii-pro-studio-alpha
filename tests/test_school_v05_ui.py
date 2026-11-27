"""Developer fixtures only. Fake Docker transport, real Core/Worker/Supervisor."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import io
import json
import socket
import time

import pytest

from ii_pro_core.errors import Conflict, RecoveryRequired, ValidationError
from ii_pro_school.adapters import ModelProfile, StudentAdapter, StudentReply
from ii_pro_school.functional_curriculum import CORRECT, FIXTURES, developer_sources
from ii_pro_worker import WorkerAdapter
from ii_pro_worker.docker_launcher import DockerBackend
from ii_pro_worker.profile import DockerProfile
from ii_pro_worker.supervisor import Limits
from ii_pro_worker.wire import read, encode, packet, fingerprint
from ii_pro_ui.functional import IMAGE, REVIEW, selected_source
from ii_pro_ui.training import Training
from test_school_v03_ui import preparation, decision, wait


class DeveloperStudent(StudentAdapter):
    revision = "v05-developer-fixture"

    def __init__(self, code):
        super().__init__(ModelProfile.build("developer-fixture", "d" * 64))
        assert code in FIXTURES.values()
        self.code = code

    def approval_configuration(self):
        return {
            "profile": asdict(self.profile),
            "source": hashlib.sha256(self.code.encode()).hexdigest(),
        }

    def probe(self):
        return {"backend": "developer_fixture", "model_called": False}

    def respond(self, prompt):
        return StudentReply(
            "```python\n" + self.code + "```\n", {"backend": "developer_fixture"}
        )


class FakeCLI:
    def identity(self):
        return {"executable": "developer-fake-docker", "sha256": "0" * 64}


class Handle:
    def __init__(self, identity, fault=None):
        self.identity = identity
        self.code = None
        self.fault = fault
        self.reader, self.writer = socket.socketpair()
        self.stdout = self.reader.makefile("rb")
        self.stderr = io.BytesIO(b"")
        self.stdin = self
        self.observations = [{"developer_fixture_simulation": True}]

    def write(self, data):
        request = read(io.BytesIO(data))
        if request["type"] != "request":
            return len(data)
        package = request["payload"]["task"]["arguments"]["package"]
        assert package["code"] in FIXTURES.values(), (
            "Never execute or simulate learner bytes in these tests"
        )
        if self.fault == "disconnect":
            self.code = 0
            self.writer.shutdown(socket.SHUT_WR)
            return len(data)
        cases = []
        for c in package["cases"]:
            if package["code"] == FIXTURES["ordinary_error"]:
                status, diagnostic = "error", "candidate_exception"
            else:
                # Fixed reference simulation, never compile/exec candidate text.
                positive = [n for n in c["input"] if n > 0]
                actual = (
                    sum(positive)
                    if package["code"] == CORRECT
                    else (positive[-1] if positive else 0)
                )
                status, diagnostic = (
                    ("passed", "ok")
                    if actual == c["expected"]
                    else ("failed", "wrong_value")
                )
            cases.append({"id": c["id"], "status": status, "diagnostic": diagnostic})
        result = {
            "method": "integer-tests-v1",
            "package_id": package["id"],
            "package_digest": package["digest"],
            "source_sha256": package["source_sha256"],
            "suite_digest": package["suite_digest"],
            "cases": cases,
            "passed": sum(c["status"] == "passed" for c in cases),
            "total": len(cases),
            "resources": {
                "memory_peak": 1024,
                "pids_current": 1,
                "cpu_usage_usec": 1000,
            },
        }
        failed = self.fault == "container_error"
        payload = {
            "status": "error" if failed else "ok",
            "stdout": "" if failed else json.dumps(result),
            "stderr": "synthetic worker failure" if failed else "",
            "exit_code": 1 if failed else 0,
            "request_digest": fingerprint(request["payload"]),
        }
        self.writer.sendall(
            encode(packet(request, "accepted", 0, {}))
            + encode(packet(request, "result", 1, payload))
        )
        self.code = payload["exit_code"]
        self.writer.shutdown(socket.SHUT_WR)
        return len(data)

    def flush(self):
        pass

    def poll(self):
        return self.code

    def cleanup(self):
        self.stdout.close()
        self.reader.close()
        self.writer.close()


@pytest.fixture
def transport(monkeypatch):
    control = {"calls": 0, "fault": None}

    def prepare(self):
        if control["fault"] == "unavailable":
            from ii_pro_worker.errors import DockerUnavailable

            raise DockerUnavailable("fixture unavailable")

    def start(self, *, job_id, run_id, epoch):
        control["calls"] += 1
        return Handle(self.locator(job_id, run_id, epoch, "a" * 64), control["fault"])

    monkeypatch.setattr(DockerBackend, "prepare", prepare)
    monkeypatch.setattr(DockerBackend, "start", start)
    return control


def worker_factory(root, authorized):
    return WorkerAdapter(
        DockerBackend(DockerProfile(IMAGE), approved_review=REVIEW, cli=FakeCLI()),
        limits=Limits(runtime_ms=5000, heartbeat_ms=2000, cancel_grace_ms=500),
        python_sources=developer_sources(),
    )


def open_training(path, *, authorized=None, code=CORRECT, session="a"):
    return Training(
        path,
        session,
        include_ollama=True,
        include_worker=True,
        authorized_worker_run=authorized,
        adapter_factory=lambda: DeveloperStudent(code),
        worker_factory=worker_factory,
    )


def source(t):
    a = t.prepare(preparation())
    t.decide(decision(a))
    assert wait(t)["status"] == "succeeded"
    return a["run_id"]


def prepare(t, source_id):
    return t.functional.prepare({"source_run_id": source_id})


def finish(t, run_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with t.lock:
            if not t.threads:
                return t.functional.view(run_id)
        time.sleep(0.005)
    pytest.fail("Functional fixture did not finish")


def authorize(path, transport, code=CORRECT):
    t = open_training(path, code=code)
    sid = source(t)
    check = prepare(t, sid)
    t.close()
    t = open_training(path, authorized=check["run_id"], code=code, session="b")
    check = prepare(t, sid)
    return t, check


def test_prepare_does_not_run_worker_and_keeps_learner_provenance(tmp_path, transport):
    t = open_training(tmp_path / "state")
    try:
        sid = source(t)
        before = t.school.response(sid)
        check = prepare(t, sid)
        assert (
            check["status"] == "created"
            and not check["dispatch_allowed"]
            and check["functional"] is None
        )
        assert check["code"] == CORRECT and check["origin"] == "model_response"
        assert len(check["cases"]) == 6 and transport["calls"] == 0
        assert (
            t.school.response(sid) == before
            and check["code_sha256"] in developer_sources()
        )
        with pytest.raises(Conflict):
            t.functional.decide(decision(check))
        assert transport["calls"] == 0
    finally:
        t.close()


def test_parallel_prepare_repairs_same_cycle_package_and_run(tmp_path, transport):
    t = open_training(tmp_path / "state")
    try:
        sid = source(t)
        with ThreadPoolExecutor(8) as pool:
            checks = list(pool.map(lambda _: prepare(t, sid), range(16)))
        assert (
            len({c["run_id"] for c in checks})
            == len({c["package_id"] for c in checks})
            == 1
        )
        assert t.school.store.one("SELECT COUNT(*) FROM school_v2_cycles")[0] == 1
        assert t.school.store.one("SELECT COUNT(*) FROM school_v2_submissions")[0] == 1
        assert transport["calls"] == 0
    finally:
        t.close()


@pytest.mark.parametrize(
    "fixture,passed", [("correct", 6), ("overwrite_bug", 3), ("ordinary_error", 0)]
)
def test_existing_supervisor_route_grades_developer_programs(
    tmp_path, transport, fixture, passed
):
    t, c = authorize(tmp_path / "state", transport, FIXTURES[fixture])
    try:
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda _: t.functional.decide(decision(c)), range(12)))
        r = finish(t, c["run_id"])
        assert (
            r["status"] == "succeeded" and r["stop_confirmed"] and r["evidence_sealed"]
        )
        assert (
            r["functional"]["passed"] == passed and len(r["functional"]["cases"]) == 6
        )
        assert r["functional"]["functional_awarded"] == 70 * passed // 6
        assert (
            r["stdout"]
            and r["stderr"] == ""
            and r["exit_code"] == 0
            and len(r["artifacts"]) == 3
        )
        assert transport["calls"] == 1
        t.functional.decide(decision(c))
        prepare(t, c["source_run_id"])
        assert transport["calls"] == 1
    finally:
        t.close()


@pytest.mark.parametrize("fault", ["container_error", "disconnect", "unavailable"])
def test_container_fault_and_unknown_never_replay(tmp_path, transport, fault):
    t, c = authorize(tmp_path / "state", transport)
    try:
        transport["fault"] = fault
        t.functional.decide(decision(c))
        r = finish(t, c["run_id"])
        if fault == "unavailable":
            assert r["status"] == "failed" and transport["calls"] == 0
        else:
            assert r["status"] == "unknown" and r["functional"] is None
            assert r["worker_reason"] and transport["calls"] == 1
            if fault == "container_error":
                assert r["stderr"] == "synthetic worker failure"
            with pytest.raises(RecoveryRequired):
                prepare(t, c["source_run_id"])
            with pytest.raises(RecoveryRequired):
                t.functional.decide(decision(c))
            with pytest.raises(RecoveryRequired):
                t.prepare(preparation("c"))
    finally:
        t.close()


def test_revoke_and_new_approval_preserve_history(tmp_path, transport):
    t, c = authorize(tmp_path / "state", transport)
    try:
        body = decision(c)
        body.pop("allow")
        r = t.functional.revoke(body)
        assert r["approval"]["status"] == "revoked"
        with pytest.raises(Conflict):
            t.functional.decide(decision(c))
        new = prepare(t, c["source_run_id"])
        assert (
            new["run_id"] == c["run_id"]
            and new["approval"]["id"] != c["approval"]["id"]
        )
        t.functional.decide(decision(new))
        assert finish(t, c["run_id"])["functional"]["passed"] == 6
        assert transport["calls"] == 1
    finally:
        t.close()


def test_reject_does_not_execute(tmp_path, transport):
    t, c = authorize(tmp_path / "state", transport)
    try:
        assert (
            t.functional.decide(decision(c, False))["approval"]["status"] == "rejected"
        )
        with pytest.raises(Conflict):
            t.functional.decide(decision(c))
        assert transport["calls"] == 0
    finally:
        t.close()


def test_history_restart_and_old_session_scope(tmp_path, transport):
    path = tmp_path / "state"
    t, c = authorize(path, transport)
    t.close()
    t = open_training(path, authorized=c["run_id"], session="c")
    try:
        assert t.functional.state()[0]["run_id"] == c["run_id"]
        with pytest.raises(Conflict):
            t.functional.decide(decision(c))
        new = prepare(t, c["source_run_id"])
        t.functional.decide(decision(new))
        r = finish(t, c["run_id"])
        assert r["functional"]["passed"] == 6 and transport["calls"] == 1
    finally:
        t.close()
    t = open_training(path, session="d")
    try:
        assert (
            t.functional.state()[0]["functional"] == r["functional"]
            and transport["calls"] == 1
        )
    finally:
        t.close()


def test_unknown_restart_keeps_no_points(tmp_path, transport):
    path = tmp_path / "state"
    t, c = authorize(path, transport)
    transport["fault"] = "disconnect"
    t.functional.decide(decision(c))
    assert finish(t, c["run_id"])["status"] == "unknown"
    t.close()
    t = open_training(path, authorized=c["run_id"], session="c")
    try:
        assert (
            t.functional.state()[0]["status"] == "unknown"
            and t.functional.state()[0]["functional"] is None
        )
        with pytest.raises(RecoveryRequired):
            t.functional.decide(decision(c))
        assert transport["calls"] == 1
    finally:
        t.close()


@pytest.mark.parametrize("field", ["run_id", "approval_id", "spec_digest"])
def test_changed_confirmation_identity_rejected(tmp_path, transport, field):
    from ii_pro_core.errors import CoreError

    t, c = authorize(tmp_path / "state", transport)
    try:
        body = decision(c)
        body[field] = "0" * len(body[field])
        with pytest.raises(CoreError):
            t.functional.decide(body)
        assert transport["calls"] == 0
    finally:
        t.close()


def test_changed_code_or_tests_cannot_replace_approved_package(tmp_path, transport):
    import sqlite3

    t, c = authorize(tmp_path / "state", transport)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with t.school.store.transaction() as db:
                db.execute("UPDATE school_v2_submissions SET code='changed'")
        with pytest.raises(sqlite3.IntegrityError):
            with t.school.store.transaction() as db:
                db.execute("UPDATE school_v2_packages SET body_json='{}'")
        t.functional.worker._limits = Limits(
            runtime_ms=4000, heartbeat_ms=2000, cancel_grace_ms=500
        )
        with pytest.raises(Conflict):
            t.functional.decide(decision(c))
        assert transport["calls"] == 0
    finally:
        t.close()


def test_existing_exam_reader_does_not_promote_practice_check_to_exam(
    tmp_path, transport
):
    from ii_pro_ui.reader import Source, read_source

    t = open_training(tmp_path / "state")
    try:
        sid = source(t)
        prepare(t, sid)
        view = read_source(Source("ui", "ui", t.root), tmp_path / "cache")
        assert view["exams"] == []
        assert selected_source(t.root, t.functional.state()[0]["run_id"]) == (
            hashlib.sha256(CORRECT.encode()).hexdigest(),
        )
    finally:
        t.close()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"source_run_id": "x"},
        {"source_run_id": "0" * 32, "code": "arbitrary"},
        {"source_run_id": ["bad"]},
    ],
)
def test_preparation_schema_is_strict(tmp_path, transport, body):
    t = open_training(tmp_path / "state")
    try:
        source(t)
        with pytest.raises(ValidationError):
            t.functional.prepare(body)
        assert transport["calls"] == 0
    finally:
        t.close()


def test_source_run_scope_is_bound_and_same_code_does_not_authorize_another_run(
    tmp_path, transport
):
    t, c = authorize(tmp_path / "state", transport)
    try:
        sid = source(t)  # Same inference request key returns the original terminal Run.
        assert sid == c["source_run_id"]
        a = t.prepare(preparation("c"))
        t.decide(decision(a))
        wait(t)
        other = prepare(t, a["run_id"])
        assert (
            other["code_sha256"] == c["code_sha256"] and other["run_id"] != c["run_id"]
        )
        with pytest.raises(Conflict):
            t.functional.decide(decision(other))
        spec = t.school.core.approvals.describe(c["approval"]["id"])
        assert json.loads(spec["description"])["source_run_id"] == c["source_run_id"]
        assert transport["calls"] == 0
    finally:
        t.close()


def test_api_security_is_preserved_for_new_write_routes(tmp_path, transport):
    import threading, http.client
    from ii_pro_ui.server import LocalServer

    server = LocalServer(0, object(), token="test")
    server.training = open_training(tmp_path / "state")
    sid = source(server.training)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()

    def post(route, body, override=None):
        headers = {
            "Authorization": "Bearer test",
            "Origin": server.origin,
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/json",
            "X-CSRF-Token": server.csrf,
        }
        headers.update(override or {})
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request(
            "POST", "/api/school/functional/" + route, json.dumps(body), headers
        )
        r = conn.getresponse()
        status = r.status
        data = json.loads(r.read())
        conn.close()
        return status, data

    try:
        for invalid in (
            {"X-CSRF-Token": "wrong"},
            {"Origin": "https://foreign.example"},
            {"Authorization": "Bearer wrong"},
        ):
            assert post("prepare", {"source_run_id": sid}, invalid)[0] == 403
        status, c = post("prepare", {"source_run_id": sid})
        assert status == 200
        assert post("decision", decision(c))[0] == 409
        scope = decision(c)
        scope.pop("allow")
        assert post("revoke", scope)[1]["approval"]["status"] == "revoked"
        assert transport["calls"] == 0
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_unfinished_response_cannot_prepare_or_consume_cycle_quota(tmp_path, transport):
    t = open_training(tmp_path / "state")
    try:
        a = t.prepare(preparation())
        with pytest.raises(ValidationError):
            prepare(t, a["run_id"])
        assert t.school.store.one("SELECT COUNT(*) FROM school_v2_cycles")[0] == 0
        assert transport["calls"] == 0
    finally:
        t.close()


def test_expired_approval_is_not_used(tmp_path, transport, monkeypatch):
    t, c = authorize(tmp_path / "state", transport)
    try:
        monkeypatch.setattr(
            "ii_pro_core.approvals.utc_now", lambda: "2999-01-01T00:00:00+00:00"
        )
        with pytest.raises(Conflict):
            t.functional.decide(decision(c))
        assert t.functional.view(c["run_id"])["approval"]["status"] == "expired"
        assert transport["calls"] == 0
    finally:
        t.close()
