"""No Ollama inference or Docker launches. UI observation and HTTP boundaries."""

import hashlib
import http.client
import json
import sqlite3
import threading

import pytest

from ii_pro_core import Core, TaskStatus
from ii_pro_school.adapters import StudentFailure
from ii_pro_ui.probes import Probes, gpu_csv, number
from ii_pro_ui.reader import Source, public, read_source, snapshot
from ii_pro_ui.server import LocalServer
from ii_pro_ui.service import Observatory, ROOT


class OfflineProbes:
    def ollama(self):
        return {
            "status": "unavailable",
            "model_available": False,
            "model_digest": None,
            "version": None,
            "loaded": [],
        }

    def worker(self):
        return {"daemon": "unavailable", "linux": None, "cgroup": None}

    def resources(self):
        return {
            "cpu_percent": None,
            "ram_total_mib": None,
            "ram_available_mib": None,
            "gpus": [],
        }


def hashes(root, *, active_owner=False):
    # Windows intentionally locks owner.lock against reads while Core owns it.
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file() and not (active_owner and p.name == "owner.lock")
    }


def fixture_state(tmp_path, *, task=False):
    root = tmp_path / "state"
    with Core(root) as core:
        if task:
            value = core.tasks.create("UI fixture", idempotency_key="ui")
            core.tasks.transition(value.id, TaskStatus.PLANNED)
            core.runs.create(value.id, idempotency_key="ui-run")
    return Source("fixture", "Тестовый источник", root)


def test_actual_first_exam_is_loaded_from_real_database_without_source_changes(
    tmp_path,
):
    source = Source(
        "exam", "Первый экзамен", ROOT / ".local/qwen-first-functional-exam-20261121"
    )
    if not (source.root / "core.sqlite3").is_file():
        pytest.skip("Live exam fixture is local evidence, not included in Git")
    before = hashes(source.root)
    result = read_source(source, tmp_path / "cache")
    assert hashes(source.root) == before
    exam = result["exams"][0]
    assert (
        exam["origin"] == "model_response"
        and exam["functional"]["passed"] == exam["functional"]["total"] == 6
    )
    assert exam["total_awarded"] == 100 and exam["evidence"]["verified"]
    assert (
        exam["code_sha256"]
        == "98bcb25910974adceeedfd3d815b9497c3c3fc1f0f620693480b69278777766e"
    )
    assert "return sum(x for x in values if x > 0)" in exam["code"]


def test_empty_history_is_not_populated_with_demo_grades(tmp_path):
    source = fixture_state(tmp_path)
    service = Observatory(tmp_path, registered=[source], probes=OfflineProbes(), ttl=0)
    data = service.state()
    assert (
        data["runs"] == data["tasks"] == data["exams"] == []
        and data["latest_exam"] is None
    )
    assert data["core"] == {"data_available": True, "owner_state": "closed"}
    assert (
        data["ollama"]["status"] == "unavailable"
        and data["resources"]["cpu_percent"] is None
    )
    assert all(r["status"] == "not_connected" for r in data["team"][:2])


@pytest.mark.parametrize("fault", ["missing", "corrupt", "schema", "guard"])
def test_sqlite_errors_do_not_leak_paths_or_fake_results(tmp_path, fault):
    source = fixture_state(tmp_path)
    db = source.root / "core.sqlite3"
    if fault == "missing":
        db.unlink()
    elif fault == "corrupt":
        db.write_bytes(b"not SQLite")
    else:
        with sqlite3.connect(db) as conn:
            conn.execute(
                "PRAGMA user_version=999"
                if fault == "schema"
                else "DROP TRIGGER events_no_delete"
            )
    data = Observatory(tmp_path, registered=[source], probes=OfflineProbes()).state()
    assert data["sources"][0]["status"] == "unavailable" and data["latest_exam"] is None
    assert str(tmp_path) not in json.dumps(data) and "sqlite3." not in json.dumps(data)


def test_unknown_is_not_a_failed_grade_and_viewing_cannot_recover_it(tmp_path):
    source = fixture_state(tmp_path, task=True)
    with sqlite3.connect(source.root / "core.sqlite3") as db:
        db.execute("UPDATE runs SET status='unknown',effect_status='unknown'")
    before = hashes(source.root)
    data = Observatory(tmp_path, registered=[source], probes=OfflineProbes()).state()
    assert data["runs"][0]["status"] == "unknown" and data["latest_exam"] is None
    assert any(w["kind"] == "unknown" for w in data["warnings"])
    assert hashes(source.root) == before


def test_live_wal_data_is_read_from_private_copy_without_touching_original(tmp_path):
    state = tmp_path / "state"
    with Core(state) as core:
        core.tasks.create("Uncheckpointed fixture", idempotency_key="wal")
        assert (state / "core.sqlite3-wal").exists()
        before = hashes(state, active_owner=True)
        result = read_source(Source("live", "WAL", state), tmp_path / "cache")
        assert result["tasks"][0]["title"] == "Uncheckpointed fixture"
        assert (
            result["owner_state"] == "unconfirmed"
        )  # a journal row is NOT proof of a live OS process
        assert hashes(state, active_owner=True) == before


def test_private_snapshot_rejects_sql_writes(tmp_path):
    source = fixture_state(tmp_path)
    with snapshot(source, tmp_path / "cache") as db:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM tasks")


def test_copy_race_fails_closed(tmp_path, monkeypatch):
    from ii_pro_ui import reader

    source = fixture_state(tmp_path)
    original = reader.stamp
    count = [0]

    def race(path):
        value = original(path)
        if path.name == "core.sqlite3":
            count[0] += 1
            if count[0] > 1 and value:
                return (*value[:-1], value[-1] + 1)
        return value

    monkeypatch.setattr(reader, "stamp", race)
    with pytest.raises(ValueError, match="Busy source"):
        read_source(source, tmp_path / "cache")


def test_sensitive_text_is_not_rendered_as_private_paths_or_credentials():
    text = public(
        "file C:\\Users\\Private\\key.txt password=abc sk-12345678901234567890 /home/alice/key"
    )
    assert (
        "Private" not in text
        and "1234567890" not in text
        and "alice" not in text
        and "abc" not in text
    )


def test_sensor_missing_values_stay_null_and_real_zero_is_kept():
    g = gpu_csv("RTX 3050, 0, 0, 4096, [N/A], Not Supported\n")[0]
    assert (
        g["utilization"] == 0
        and g["used_mib"] == 0
        and g["temperature"] is None
        and g["power_w"] is None
    )
    assert (
        number("nan") is None
        and number("inf") is None
        and gpu_csv("driver unavailable") == []
    )


def test_ollama_offline_probe_cannot_generate_or_pull():
    probes = Probes()
    calls = []

    class Transport:
        def request(self, route, **kwargs):
            calls.append(route)
            raise StudentFailure("unavailable")

    probes.transport = Transport()
    assert probes.ollama()["status"] == "unavailable" and calls == ["version"]


def test_successful_probe_uses_metadata_routes_only(tmp_path):
    probes = Probes()
    calls = []

    class Transport:
        def request(self, route, **kwargs):
            calls.append(route)
            return {"version": "fixture"} if route == "version" else {"models": []}

    probes.transport = Transport()
    result = probes.ollama()
    assert result["status"] == "available" and result["model_available"] is False
    assert calls == ["version", "tags", "ps"]
    metadata = OfflineProbes()
    metadata.ollama = lambda: result
    state = Observatory(tmp_path, registered=[], probes=metadata).state()
    assert {"worker", "model"}.issubset({w["kind"] for w in state["warnings"]})


@pytest.fixture
def api(tmp_path):
    class Fake:
        calls = 0

        def state(self):
            self.calls += 1
            return {"read_only": True, "latest_exam": None}

    folder = tmp_path / "static"
    folder.mkdir()
    (folder / "index.html").write_text('<html lang="ru">Alpha</html>')
    server = LocalServer(0, Fake(), static=folder, token="test-capability")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(server, path="/api/state", method="GET", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    conn.request(method, path, headers=headers or {})
    result = conn.getresponse()
    body = result.read()
    response = (result.status, dict(result.headers), body)
    conn.close()
    return response


def test_api_requires_session_and_is_never_public(api):
    assert api.server_address[0] == "127.0.0.1"
    assert request(api)[0] == 401 and api.service.calls == 0
    code, headers, body = request(
        api, headers={"Authorization": "Bearer test-capability"}
    )
    assert code == 200 and json.loads(body)["read_only"]
    assert (
        "Access-Control-Allow-Origin" not in headers
        and "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    )
    assert headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Host": "evil.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_cross_site_and_dns_rebinding_are_rejected(api, headers):
    code, _, _ = request(
        api, headers={"Authorization": "Bearer test-capability", **headers}
    )
    assert code in (401, 403) and api.service.calls == 0


@pytest.mark.parametrize(
    "method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
)
def test_no_write_or_approval_or_execution_api_exists(api, method):
    assert (
        request(
            api, "/api/execute", method, {"Authorization": "Bearer test-capability"}
        )[0]
        == 405
    )
    assert api.service.calls == 0


@pytest.mark.parametrize(
    "path",
    [
        "/api/state?token=test-capability",
        "/assets/%2e%2e/server.py",
        "/.local/core.sqlite3",
        "/api/approve",
        "/api/run",
    ],
)
def test_no_token_query_or_host_file_or_command_routes(api, path):
    assert request(api, path, headers={"Authorization": "Bearer test-capability"})[
        0
    ] in (400, 404)


def test_static_bootstrap_never_contains_session_secret(api):
    code, _, body = request(api, "/")
    assert code == 200 and b"test-capability" not in body


def test_state_cache_limits_sensor_queries(tmp_path):
    class Counting(OfflineProbes):
        calls = 0

        def resources(self):
            self.calls += 1
            return super().resources()

    probes = Counting()
    service = Observatory(tmp_path, registered=[], probes=probes)
    assert service.state() == service.state() and probes.calls == 1


def test_launcher_chooses_a_free_loopback_port_without_starting_work(
    monkeypatch, capsys, tmp_path
):
    from ii_pro_ui import __main__ as launcher

    seen = []

    class StartupServer(LocalServer):
        def enable_training(self, root, **kwargs):
            # CLI bootstrap must never open the user's actual School database.
            super().enable_training(tmp_path / "school", **kwargs)

        def serve_forever(self, **kwargs):
            # Exercise the actual CLI bootstrap/socket binding, not UI execution.
            seen.append((self.server_address, self.origin))

    def unexpected_browser(url):
        raise AssertionError("--no-browser must not launch an app")

    monkeypatch.setattr(launcher, "LocalServer", StartupServer)
    monkeypatch.setattr(launcher.webbrowser, "open", unexpected_browser)
    monkeypatch.setattr("sys.argv", ["ii_pro_ui", "--no-browser"])
    launcher.main()
    assert len(seen) == 1 and seen[0][0][0] == "127.0.0.1"
    assert seen[0][0][1] >= 1024
    assert seen[0][1] in capsys.readouterr().out


def test_observatory_includes_the_explicit_active_studio_even_after_refresh(tmp_path):
    active = fixture_state(tmp_path, task=True)
    service = Observatory(
        tmp_path, active_root=active.root, probes=OfflineProbes(), ttl=0
    )
    for _ in range(2):
        state = service.state()
        assert any(source["key"] == "studio-active" for source in state["sources"])
        assert state["tasks"][0]["title"] == "UI fixture"
        assert state["runs"][0]["status"] == "created"


def test_disabling_docker_probes_does_not_construct_cli(monkeypatch):
    def forbidden():
        pytest.fail("Docker CLI must remain unused")

    monkeypatch.setattr("ii_pro_ui.probes.DockerCLI", forbidden)
    assert Probes(docker_enabled=False).worker()["checked"] is False


def test_dashboard_exposes_text_readiness_without_changing_sensor_snapshot(tmp_path):
    from types import SimpleNamespace

    service = Observatory(tmp_path, registered=[], probes=OfflineProbes())
    server = LocalServer(0, service)
    try:
        timing = SimpleNamespace(
            ready=lambda: True, mock=False, engineering_supported=True
        )
        server.training = SimpleNamespace(
            team=SimpleNamespace(text_executor=SimpleNamespace(adapter=timing)),
            close=lambda: None,
        )
        assert [r["status"] for r in server.dashboard()["team"]] == [
            "text_ready",
            "text_ready",
            "unavailable",
        ]
        assert service.state()["team"][0]["status"] == "not_connected"
        timing.mock = True
        assert server.dashboard()["team"][0]["status"] == "not_connected"
    finally:
        server.server_close()
