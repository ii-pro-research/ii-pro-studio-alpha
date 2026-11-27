"""Verify the fixed application grader without launching candidate code."""

import json
import sqlite3
from contextlib import closing, contextmanager

import pytest

from ii_pro_core.errors import ValidationError
from ii_pro_core.security import Action
from ii_pro_worker import app_runner
from ii_pro_worker.app_contract import (
    FILTER_PROFILE,
    PROFILE,
    SORT_PROFILE,
    app_package,
    validate_app_result,
)
from ii_pro_worker.wire import ProtocolError, fingerprint, validate_task

SOURCE = "def create_app(db_path):\n    raise AssertionError('never execute this fixture')\n"


def test_action_accepts_exact_worker_profile_and_still_scans_candidate_code():
    def parameters(code):
        return {"fixture": "app_test", "arguments": {"package": app_package(code, FILTER_PROFILE)}}

    action = Action.build("worker.fixed", parameters(SOURCE))
    assert json.loads(action.parameters_json) == parameters(SOURCE)
    assert Action.from_json(action.json) == action
    for token in ("sk-" + "x" * 20, f"{FILTER_PROFILE}-extra", f"sk-prefix-{FILTER_PROFILE}",
                  f"access_token={FILTER_PROFILE}"):
        with pytest.raises(ValidationError, match="Credential-like"):
            Action.build("worker.fixed", parameters(SOURCE + f"\n# {token}"))
    altered = parameters(SOURCE)
    package = altered["arguments"]["package"]
    package["profile"] += "-other"
    package["digest"] = fingerprint({k: v for k, v in package.items() if k != "digest"})
    with pytest.raises(ValidationError, match="Invalid fixed Worker task"):
        Action.build("worker.fixed", altered)


def test_candidate_fixture_is_bound_and_host_execution_is_refused():
    frozen = app_package(SOURCE)
    validate_task({"fixture": "app_test", "arguments": {"package": frozen}})
    altered = {**frozen, "cases": ["restart"] * 4}
    altered["digest"] = fingerprint({k: v for k, v in altered.items() if k != "digest"})
    with pytest.raises(ProtocolError):
        validate_task({"fixture": "app_test", "arguments": {"package": altered}})
    with pytest.raises(RuntimeError, match="Fixed Docker"):
        app_runner.evaluate(frozen)


@pytest.mark.parametrize("persist", [True, False])
def test_grader_requires_restart_and_independent_sqlite_evidence(monkeypatch, tmp_path, persist):
    states, expected = [], [{"id": 1, "title": "", "completed": False}]

    @contextmanager
    def child(code, database):
        assert code == SOURCE
        states.append(database)
        with closing(sqlite3.connect(database)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS tasks(id,title,completed)")

            def request(method, path, **fields):
                if path == "/":
                    return {"status": 200, "type": "text/html", "text": "<form></form>"}
                if path == "/tasks.json":
                    return {"status": 200, "type": "application/json", "text": json.dumps(expected)}
                if "title" in fields:
                    expected[0]["title"] = fields["title"]
                if path.endswith("/complete"):
                    expected[0]["completed"] = True
                if persist:
                    db.execute("DELETE FROM tasks")
                    db.execute("INSERT INTO tasks VALUES(1,?,?)", (expected[0]["title"], int(expected[0]["completed"])))
                    db.commit()
                assert method == "POST"
                return {"status": 303}

            yield request

    monkeypatch.setattr(app_runner, "application", child)
    checks = app_runner.check_application(SOURCE, tmp_path / "data.sqlite")
    assert len(states) == 2 and states[0] == states[1]
    assert sum(status == "passed" for status, _ in checks) == (4 if persist else 1)
    package = app_package(SOURCE)
    forged = {"method": PROFILE, "package_digest": package["digest"], "passed": 4, "total": 4,
              "cases": [{"id": case, "status": status, "diagnostic": diagnostic}
                        for case, (status, diagnostic) in zip(package["cases"], checks, strict=True)]}
    if persist:
        assert validate_app_result(forged, package)["passed"] == 4
    else:
        with pytest.raises(ValueError):
            validate_app_result(forged, package)


@pytest.mark.parametrize("filters", [True, False])
def test_filter_contract_distinguishes_filtered_and_unfiltered_responses(monkeypatch, tmp_path, filters):
    @contextmanager
    def child(code, database):
        assert code == SOURCE
        with closing(sqlite3.connect(database)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY,title,completed)")

            def request(method, path, **fields):
                route, _, query = path.partition("?status=")
                if method == "POST":
                    if route == "/tasks":
                        db.execute("INSERT INTO tasks(title, completed) VALUES(?,0)", (fields["title"],))
                    elif route.endswith("/edit"):
                        db.execute("UPDATE tasks SET title=? WHERE id=1", (fields["title"],))
                    elif route.endswith("/complete"):
                        db.execute("UPDATE tasks SET completed=1 WHERE id=1")
                    db.commit()
                    return {"status": 303}
                rows = [dict(id=i, title=title, completed=bool(done))
                        for i, title, done in db.execute("SELECT id,title,completed FROM tasks ORDER BY id")]
                if filters and query in ("open", "completed"):
                    rows = [row for row in rows if row["completed"] == (query == "completed")]
                if route == "/":
                    return {"status": 200, "type": "text/html", "text": "<form>" + str([x["title"] for x in rows])}
                return {"status": 200, "type": "application/json", "text": json.dumps(rows)}

            yield request

    monkeypatch.setattr(app_runner, "application", child)
    checks = app_runner.check_application(SOURCE, tmp_path / "data.sqlite", FILTER_PROFILE)
    assert len(checks) == len(app_package(SOURCE, FILTER_PROFILE)["cases"]) == 7
    assert [status for status, _ in checks] == ["passed"] * 4 + (["passed"] * 3 if filters else ["failed"] * 2 + ["passed"])


@pytest.mark.parametrize(("defect", "case"), [
    (None, None), ("case_sensitive", "sort_title_json"), ("html_order", "sort_title_html"),
    ("split_filter", "sort_with_status"), ("get_writes", "sort_default_and_unknown"),
])
def test_sort_cases_reject_wrong_evidence_without_running_candidate(monkeypatch, tmp_path, defect, case):
    from urllib.parse import parse_qs, urlsplit

    keys = ("sort_title_json", "sort_title_html", "sort_with_status", "sort_default_and_unknown")
    calls = set()

    @contextmanager
    def child(code, database):
        assert code == SOURCE
        calls.add(database.name)
        with closing(sqlite3.connect(database)) as db:
            db.execute("CREATE TABLE tasks(id INTEGER PRIMARY KEY,title,completed)")

            def request(method, path, **fields):
                if method == "POST":
                    if path == "/tasks":
                        db.execute("INSERT INTO tasks(title,completed) VALUES(?,0)", (fields["title"],))
                    else:
                        db.execute("UPDATE tasks SET completed=1 WHERE id=?", (int(path.split("/")[2]),))
                    db.commit()
                    return {"status": 303}
                query = parse_qs(urlsplit(path).query)
                status, sort = query.get("status", ["all"])[0], query.get("sort", ["id"])[0]
                html = urlsplit(path).path == "/"
                ids = {(False, "all"): (1, 2, 3, 4), (False, "open"): (1, 3),
                       (True, "all"): (2, 3, 4, 1), (True, "open"): (3, 1),
                       (True, "completed"): (2, 4)}[sort == "title", status]
                if defect == "case_sensitive" and not html and ids == (2, 3, 4, 1):
                    ids = (3, 4, 1, 2)
                if defect == "html_order" and html and ids == (2, 3, 4, 1):
                    ids = (1, 2, 3, 4)
                if defect == "split_filter" and html and ids == (3, 1):
                    ids = (2, 3, 4, 1)
                if defect == "get_writes" and not query:
                    db.execute("INSERT INTO tasks(title,completed) VALUES('temporary',0)")
                    db.commit()
                titles = ("Zulu", "alpha", "Alpha", "Beta")
                if html:
                    sort = "title" if sort == "title" else "id"
                    status_opt = ''.join(f'<option{" selected" if s == status else ""}>{s}' for s in ("all", "open", "completed"))
                    sort_opt = ''.join(f'<option{" selected" if s == sort else ""}>{s}' for s in ("id", "title"))
                    body = f'<form><select name=status>{status_opt}</select><select name=sort>{sort_opt}</select></form>'
                    body += ''.join(f'<p>{titles[i-1]}</p>' for i in ids)
                    return {"status": 200, "type": "text/html", "text": body}
                return {"status": 200, "type": "application/json", "text": json.dumps([
                    {"id": i, "title": titles[i-1], "completed": i in (2, 4)} for i in ids])}

            yield request

    monkeypatch.setattr(app_runner, "application", child)
    assert app_package(SOURCE, SORT_PROFILE)["cases"] == app_package(SOURCE, FILTER_PROFILE)["cases"] + list(keys)
    for key in keys:
        assert app_runner._sort_case(SOURCE, tmp_path / (key + ".sqlite3"), key) is (key != case)
    assert len(calls) == 4
