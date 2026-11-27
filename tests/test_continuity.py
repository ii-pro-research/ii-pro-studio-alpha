import http.client
import io
import json
from pathlib import Path
from threading import Thread
from zipfile import ZipFile

import pytest

from ii_pro_core import Core
from ii_pro_core.errors import CoreError, ValidationError
from ii_pro_core.executors import ExecutionResult, ProducedArtifact
from ii_pro_ui import continuity
from ii_pro_ui.server import LocalServer


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for name in continuity.DOCS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# M.1 проект", encoding="utf-8")
    continuity.git(root, "init", "-b", "fixture")
    continuity.git(root, "add", ".")
    continuity.git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                   "commit", "-m", "Initial")
    return root


def test_archive_preserves_inert_state_without_exporting_permissions(repo):
    state = repo / "state"
    with Core(state) as core:
        task = core.tasks.create("PRIVATE LOG", idempotency_key="pending")
        core.tasks.transition(task.id, "planned")
        run = core.runs.create(task.id, idempotency_key="pending")
        review_task = core.tasks.create("Review", idempotency_key="review")
        core.tasks.transition(review_task.id, "planned")
        review = core.runs.create(review_task.id, idempotency_key=f"project:{'a' * 32}:review")
        with core.store.transaction() as conn:
            conn.execute("UPDATE runs SET status='unknown', effect_status='unknown' WHERE id=?", (run.id,))
            core.store.event(conn, "team.unknown_abandoned", abandoned_run_id=run.id, no_retry=True)
            core.store.event(conn, "team.project_auto_end", job="a" * 32, reason="completed")
            row = conn.execute("SELECT * FROM runs WHERE id=?", (review.id,)).fetchone()
            core.artifacts.import_blobs(conn, row, ExecutionResult((
                ProducedArtifact("engineering.json", b'{"functional_tests":{"passed":11,"total":11}}'),
                ProducedArtifact("shadow-material.json", b'{"status":"unreviewed","private":"PRIVATE LOG"}'),
            ), storage="sqlite"))
            conn.execute("UPDATE runs SET status='succeeded' WHERE id=?", (review.id,))
        package, manifest = continuity.create(state, root=repo)
    assert continuity.verify(package) == manifest
    # A recipient can read the useful snapshot without Git or the source database.
    with ZipFile(io.BytesIO(package)) as archive:
        snapshot = json.loads(archive.read("state.json"))
        assert snapshot["core"]["recent_runs"][0]["no_retry"]
        assert snapshot["core"]["recent_runs"][0]["cloud_abandoned"]
        assert snapshot["core"]["recent_runs"][0]["status"] == "unknown"
        assert snapshot["core"]["recent_trials"][0]["outcome"] == "completed"
        assert snapshot["core"]["recent_trials"][0]["learning"] == "unreviewed"
        assert snapshot["core"]["recent_trials"][0]["functional"] == {"passed": 11, "total": 11}
        assert "M.1" in archive.read("docs/ROADMAP.md").decode()
        assert all(b"PRIVATE LOG" not in archive.read(name) for name in archive.namelist())
        assert not any("sqlite" in name or "approval" in name for name in archive.namelist())


def test_manual_text_opt_in_and_secret_filter(repo, tmp_path):
    path = repo / ".local/continuity/chat.txt"
    path.parent.mkdir(parents=True)
    path.write_text("password = SYNTHETIC-SECRET", encoding="utf-8")
    package, manifest = continuity.create(tmp_path / "absent", root=repo)
    assert continuity.TRANSCRIPT not in manifest["files"]
    with pytest.raises(ValidationError):
        continuity.create(tmp_path / "absent", root=repo, include_transcript=True)
    path.write_text("Запись пользователя о проекте.", encoding="utf-8")
    package, manifest = continuity.create(tmp_path / "absent", root=repo, include_transcript=True)
    assert continuity.verify(package)["files"][continuity.TRANSCRIPT]["origin"] == "manual_unverified"
    outside = tmp_path / "outside.txt"
    outside.write_text("Outside data", encoding="utf-8")
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("Symbolic links unavailable")
    with pytest.raises(CoreError):
        continuity.create(tmp_path / "absent", root=repo, include_transcript=True)


@pytest.mark.parametrize("mode", ["corrupt", "traversal", "checksum"])
def test_verify_rejects_bad_packages(repo, tmp_path, mode):
    package, _ = continuity.create(tmp_path / "absent", root=repo)
    if mode == "corrupt":
        broken = package[:-25]
    else:
        out = io.BytesIO()
        with ZipFile(io.BytesIO(package)) as source, ZipFile(out, "w") as dest:
            for name in source.namelist():
                dest.writestr(name, b"changed" if mode == "checksum" and name == "state.json" else source.read(name))
            if mode == "traversal":
                dest.writestr("../escape", b"outside")
        broken = out.getvalue()
    with pytest.raises(ValidationError):
        continuity.verify(broken)


def test_loopback_download_requires_session(repo, tmp_path, monkeypatch):
    create = continuity.create
    monkeypatch.setattr(continuity, "create", lambda state_root: create(state_root, root=repo))
    server = LocalServer(0, None)
    server.enable_training(tmp_path / "absent")
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.03})
    thread.start()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    try:
        conn.request("GET", "/api/continuity")
        response = conn.getresponse()
        assert response.status == 401
        response.read()
        conn.request("GET", "/api/continuity", headers={"Authorization": f"Bearer {server.token}"})
        response = conn.getresponse()
        assert response.status == 200
        assert "II_PRO_CONTINUE.zip" in response.getheader("Content-Disposition")
        assert response.getheader("X-II-Pro-Integrity") == "verified"
        assert continuity.verify(response.read())["stage"] == "M.1"
        assert not (Path(tmp_path) / "absent").exists()
    finally:
        conn.close()
        server.shutdown()
        thread.join()
        server.server_close()
