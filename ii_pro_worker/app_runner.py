"""Trusted finite WSGI harness. Candidate code runs in a child inside Docker only."""

import ctypes
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode

if __package__:
    from .app_contract import (
        FILTER_PROFILE,
        PROFILE,
        PROFILES,
        SORT_PROFILE,
        validate_app_package,
        validate_app_result,
    )
    from .wire import canonical
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from app_contract import (
        FILTER_PROFILE,
        PROFILE,
        PROFILES,
        SORT_PROFILE,
        validate_app_package,
        validate_app_result,
    )
    from wire import canonical


LIMIT = 12288


def require_container():
    if (sys.platform != "linux" or os.geteuid() != 65532
            or Path(__file__).resolve() != Path("/opt/worker/app_runner.py")
            or Path("/sys/fs/cgroup/memory.max").read_text().strip() != "134217728"):
        raise RuntimeError("Fixed Docker application sandbox required")


def message(source):
    raw = source.readline(LIMIT + 1)
    if not raw or len(raw) > LIMIT or not raw.endswith(b"\n"):
        raise ValueError("Application framing failed")
    return json.loads(raw)


def send(stream, value):
    raw = canonical(value).encode()
    if len(raw) >= LIMIT:
        raise ValueError("Application output quota")
    stream.write(raw + b"\n")
    stream.flush()


@contextmanager
def application(code, database):
    with subprocess.Popen([sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()), "--child"],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          cwd=database.parent, env={"LANG": "C.UTF-8"}, close_fds=True,
                          start_new_session=True) as child:
        send(child.stdin, {"code": code, "db": str(database)})

        def request(method, path, **fields):
            send(child.stdin, {"method": method, "path": path, "body": urlencode(fields)})
            response = message(child.stdout)
            if (type(response) is not dict or set(response) != {"status", "type", "text"}
                    or type(response["status"]) is not int
                    or not isinstance(response["type"], str) or not isinstance(response["text"], str)):
                raise ValueError("Invalid application response")
            return response

        try:
            yield request
        finally:
            child.stdin.close()
            child.wait(timeout=0.5)


def child():
    require_container()
    init = message(sys.stdin.buffer)
    ns = {"__name__": "candidate_app"}
    exec(compile(init["code"], "<bounded-project-app>", "exec"), ns)
    app = ns["create_app"](init["db"])
    while True:
        line = sys.stdin.buffer.peek(1)
        if not line:
            return
        request = message(sys.stdin.buffer)
        data = request["body"].encode()
        metadata = {}

        def start_response(status, headers, exc_info=None):
            metadata.update(status=int(status.split()[0]), type=dict((k.lower(), v) for k, v in headers).get("content-type", ""))

        path, _, query = request["path"].partition("?")
        response = app({"REQUEST_METHOD": request["method"], "PATH_INFO": path,
                        "QUERY_STRING": query, "CONTENT_LENGTH": str(len(data)),
                        "CONTENT_TYPE": "application/x-www-form-urlencoded", "wsgi.input": io.BytesIO(data),
                        "wsgi.version": (1, 0), "wsgi.url_scheme": "http",
                        "wsgi.errors": sys.stderr, "wsgi.multithread": False,
                        "wsgi.multiprocess": False, "wsgi.run_once": False,
                        "SERVER_NAME": "localhost", "SERVER_PORT": "80",
                        "SERVER_PROTOCOL": "HTTP/1.1", "SCRIPT_NAME": ""}, start_response)
        chunks = bytearray()
        try:
            for chunk in response:
                if not isinstance(chunk, bytes) or len(chunks) + len(chunk) > 8192:
                    raise ValueError("Application response too large")
                chunks.extend(chunk)
        finally:
            if hasattr(response, "close"):
                response.close()
        send(sys.stdout.buffer, {**metadata, "text": chunks.decode()})


class _TaskPage(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.titles = []
        self.forms = []
        self.form = self.options = None
        self.feed(markup)
        self.close()

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "form" and (attrs.get("method") or "get").lower() == "get" and attrs.get("action") in {None, "", "/"}:
            self.form = {}
            self.forms.append(self.form)
        elif tag == "select" and self.form is not None:
            self.options = self.form[attrs.get("name")] = []
        elif tag == "option" and self.options is not None:
            self.options.append((attrs.get("value"), "selected" in attrs, ""))

    def handle_endtag(self, tag):
        if tag in {"select", "form"}:
            self.options = None
        if tag == "form":
            self.form = None

    def handle_data(self, data):
        if self.options:
            value, selected, label = self.options[-1]
            self.options[-1] = (value, selected, label + data)
        elif data.strip() in {"Zulu", "alpha", "Alpha", "Beta"}:
            self.titles.append(data.strip())

    def selectors_match(self, status, sort):
        def active(options, expected, allowed):
            values = [(label.strip() if value is None else value, flag) for value, flag, label in options]
            return {value for value, _ in values} >= allowed and (
                [value for value, flag in values if flag] or [values[0][0]]) == [expected]

        return any(active(form.get("status", []), status, {"all", "open", "completed"})
                   and active(form.get("sort", []), sort, {"id", "title"}) for form in self.forms)


def _sort_case(code, database, case):
    titles = ("Zulu", "alpha", "Alpha", "Beta")
    rows = [(i, title, int(i in {2, 4})) for i, title in enumerate(titles, 1)]

    def saved():
        if database.is_symlink() or database.stat().st_size > 1048576:
            return None
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.2) as conn:
            conn.execute("PRAGMA trusted_schema=OFF")
            return conn.execute("SELECT id,title,completed FROM tasks ORDER BY id LIMIT 5").fetchall()

    with application(code, database) as call:
        for i, title, done in rows:
            if call("POST", "/tasks", title=title)["status"] not in {200, 201, 204, 302, 303}:
                return False
            if done and call("POST", f"/tasks/{i}/complete")["status"] not in {200, 201, 204, 302, 303}:
                return False
        if saved() != rows:
            return False

        def listing(query, ids):
            response = call("GET", "/tasks.json" + query)
            data = json.loads(response["text"]) if response["status"] == 200 else None
            expected = [{"id": i, "title": titles[i - 1], "completed": i in {2, 4}} for i in ids]
            return (response["type"].startswith("application/json") and data == expected
                    and all(type(row["completed"]) is bool and type(row["id"]) is int for row in data))

        def page(query, ids, status, sort):
            response = call("GET", "/" + query)
            parsed = _TaskPage(response["text"])
            return (response["status"] == 200 and response["type"].startswith("text/html")
                    and parsed.titles == [titles[i - 1] for i in ids] and parsed.selectors_match(status, sort))

        if case == "sort_title_json":
            return listing("?sort=title", (2, 3, 4, 1))
        if case == "sort_title_html":
            return page("?sort=title", (2, 3, 4, 1), "all", "title")
        if case == "sort_with_status":
            for status, ids in (("open", (3, 1)), ("completed", (2, 4))):
                query = f"?status={status}&sort=title"
                if not listing(query, ids) or not page(query, ids, status, "title"):
                    return False
            return True
        defaults = [(q, (1, 2, 3, 4), "all") for q in ("?sort=id", "", "?sort=", "?sort=other")]
        defaults.append(("?status=open&sort=other", (1, 3), "open"))
        return (all(listing(q, ids) and page(q, ids, status, "id") for q, ids, status in defaults)
                and saved() == rows)


def check_application(code, database, profile=PROFILE):
    old, new = "Example item 731", "Updated item 429"
    completed = []

    def saved(title, done):
        if not database.is_file() or database.is_symlink() or database.stat().st_size > 1048576:
            return False
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.2) as conn:
            conn.execute("PRAGMA trusted_schema=OFF")
            return conn.execute("SELECT id,title,completed FROM tasks ORDER BY id LIMIT 2").fetchall() == [(1, title, done)]

    def result(ok):
        completed.append(("passed", "ok") if ok else ("failed", "response_mismatch"))

    def response_ok(value):
        return value["status"] in {200, 201, 204, 302, 303}

    def inventory(call, title, done):
        answer = call("GET", "/tasks.json")
        return answer["status"] == 200 and json.loads(answer["text"]) == [{"id": 1, "title": title, "completed": done}]

    try:
        with application(code, database) as call:
            page = call("GET", "/")
            result(page["status"] == 200 and page["type"].startswith("text/html") and "<form" in page["text"].lower())
            result(response_ok(call("POST", "/tasks", title=old)) and inventory(call, old, False) and saved(old, 0))
            result(response_ok(call("POST", "/tasks/1/edit", title=new))
                   and response_ok(call("POST", "/tasks/1/complete")) and inventory(call, new, True) and saved(new, 1))
        with application(code, database) as call:
            result(inventory(call, new, True) and saved(new, 1))
            if profile in {FILTER_PROFILE, SORT_PROFILE}:
                second = "Open item 842"
                ready = response_ok(call("POST", "/tasks", title=second))
                both = [{"id": 1, "title": new, "completed": True},
                        {"id": 2, "title": second, "completed": False}]
                for selector, expected, shown, hidden in (
                    ("open", both[1:], (second,), (new,)),
                    ("completed", both[:1], (new,), (second,)),
                    ("all", both, (new, second), ()),
                ):
                    filtered = call("GET", "/tasks.json?status=" + selector)
                    html = call("GET", "/?status=" + selector)
                    result(ready and filtered["status"] == html["status"] == 200
                           and json.loads(filtered["text"]) == expected
                           and all(x in html["text"] for x in shown) and not any(x in html["text"] for x in hidden)
                           and (selector != "all" or json.loads(call("GET", "/tasks.json")["text"]) == both))
    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError):
        pass
    if profile == SORT_PROFILE:
        completed.extend([("error", "application_error")] * (len(PROFILES[FILTER_PROFILE]) - len(completed)))
        for case in PROFILES[SORT_PROFILE][len(PROFILES[FILTER_PROFILE]):]:
            try:
                result(_sort_case(code, database.with_name(case + ".sqlite3"), case))
            except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError):
                completed.append(("error", "application_error"))
    return completed + [("error", "application_error")] * (len(PROFILES[profile]) - len(completed))


def evaluate(package):
    require_container()
    validate_app_package(package)
    # A same-UID child must not be able to open the Supervisor protocol FDs
    # through /proc or inspect the trusted grader's memory.
    if ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) != 0:
        raise RuntimeError("Application grader isolation failed")
    with tempfile.TemporaryDirectory(prefix="app-", dir="/tmp") as directory:
        checks = check_application(package["code"], Path(directory) / "tasks.sqlite3", package["profile"])
    cases = PROFILES[package["profile"]]
    result = {"method": package["profile"], "package_digest": package["digest"],
              "cases": [{"id": name, "status": status, "diagnostic": reason}
                        for name, (status, reason) in zip(cases, checks, strict=True)],
              "passed": sum(status == "passed" for status, _ in checks), "total": len(cases)}
    return validate_app_result(result, package)


if __name__ == "__main__" and sys.argv == [__file__, "--child"]:
    child()
