"""Authenticated loopback observations and explicitly scoped School commands."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path, PurePosixPath
import secrets
import threading
from urllib.parse import unquote, urlsplit

from ii_pro_core.errors import CoreError
from ii_pro_core.security import no_links
from ii_pro_school.adapters import _strict_json


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port, service, *, static=None, token=None):
        self.service = service
        self.static = (
            Path(static) if static is not None else Path(__file__).parent / "static"
        )
        self.token = token or secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.training = None
        self.slots = threading.BoundedSemaphore(12)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    def enable_training(
        self,
        root,
        *,
        authorized_real_run=None,
        authorized_worker_run=None,
        include_team=True,
        authorized_text_run=None,
        gpt_auth_dir=None,
        local_projects=None,
        local_live=False,
        engineering_worker=None,
    ):
        from .training import Training

        self.training = Training(
            root,
            self.token,
            include_ollama=True,
            authorized_real_run=authorized_real_run,
            include_worker=True,
            authorized_worker_run=authorized_worker_run,
            include_team=include_team,
            authorized_text_run=authorized_text_run,
            gpt_auth_dir=gpt_auth_dir,
            local_projects=local_projects,
            local_live=local_live,
            engineering_worker=engineering_worker,
        )

    def dashboard(self):
        state = self.service.state()
        team = self.training.team if self.training else None
        executor = team.text_executor if team else None
        adapter = executor.adapter if executor else None
        if not adapter or adapter.mock or not adapter.ready():
            return state
        ready = {"GPT Architect"}
        if getattr(adapter, "engineering_supported", False):
            ready.add("GPT Engineer")
        return {
            **state,
            "team": [
                {**role, "status": "text_ready"} if role["role"] in ready else role
                for role in state["team"]
            ],
        }

    def server_close(self):
        if self.training is not None:
            self.training.close()
        super().server_close()

    def verify_request(self, request, client_address):
        return client_address[0] == "127.0.0.1" and self.slots.acquire(blocking=False)

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "IIProStudio"
    sys_version = ""

    def log_message(self, *_):
        pass  # no URLs, session credentials, paths or query data in logs

    def setup(self):
        super().setup()
        self.connection.settimeout(12)

    def reply(self, code, value, mime="application/json; charset=utf-8", *, headers=()):
        try:
            return self._reply(code, value, mime, headers=headers)
        except (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
            TimeoutError,
        ):
            # Reload/navigation may abandon a poll, including during headers.
            # Do not attempt a second response or interpret this as Run failure.
            self.close_connection = True

    def _reply(self, code, value, mime, *, headers=()):
        data = (
            value
            if isinstance(value, bytes)
            else json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        )
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        for name, value in headers:
            self.send_header(name, value)
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def trusted_request(self):
        host = urlsplit(self.server.origin).netloc
        return self.headers.get("Host") == host and self.headers.get("Origin") in (
            None,
            self.server.origin,
        )

    def do_GET(self):
        if not self.trusted_request():
            return self.reply(403, {"error": "Запрос с другого адреса запрещён"})
        url = urlsplit(self.path)
        if url.query:
            return self.reply(400, {"error": "Параметры URL не поддерживаются"})
        path = unquote(url.path)
        if path.startswith("/api/"):
            supplied = self.headers.get("Authorization", "")
            if not secrets.compare_digest(
                supplied.encode("utf-8"),
                ("Bearer " + self.server.token).encode("utf-8"),
            ) or self.headers.get("Sec-Fetch-Site") in ("cross-site", "none"):
                return self.reply(401, {"error": "Нужен доступ к локальной сессии"})
            if path == "/api/health":
                return self.reply(
                    200, {"status": "ok", "read_only": True, "api_version": 1}
                )
            if path == "/api/session":
                return self.reply(
                    200,
                    {
                        "csrf": self.server.csrf,
                        "training_mode": "mock_and_ollama"
                        if self.server.training
                        else "disabled",
                    },
                )
            if path == "/api/continuity" and self.server.training:
                from .continuity import FILENAME, create

                try:
                    archive, manifest = create(self.server.training.root)
                except Exception:
                    return self.reply(503, {"error": "Не удалось создать и проверить пакет"})
                return self.reply(200, archive, "application/zip", headers=(
                    ("Content-Disposition", f'attachment; filename="{FILENAME}"'),
                    ("X-II-Pro-Stage", manifest["stage"]),
                    ("X-II-Pro-Head", manifest["head"]),
                    ("X-II-Pro-Created", manifest["created_at"]),
                    ("X-II-Pro-Integrity", "verified"),
                ))
            if path == "/api/school/training" and self.server.training:
                try:
                    return self.reply(200, self.server.training.state())
                except Exception:
                    return self.reply(
                        503, {"error": "История занятий временно недоступна"}
                    )
            if path == "/api/school/education" and self.server.training:
                try:
                    with self.server.training.lock:
                        self.server.training._open()
                        education = self.server.training.education
                        return self.reply(200, education.state() if education else None)
                except Exception:
                    return self.reply(503, {"error": "Эксперимент School временно недоступен"})
            if path in {"/api/team/state", "/api/team/evolution"} and self.server.training:
                try:
                    with self.server.training.lock:
                        self.server.training._open()
                        team = self.server.training.team
                        if team is None:
                            return self.reply(200, {"enabled": False, "jobs": []})
                        return self.reply(200, team.evolution.state() if path.endswith("/evolution") else team.state())
                except Exception:
                    return self.reply(
                        503,
                        {"error": "Журнал команды недоступен; запуск не повторяется"},
                    )
            if path == "/api/team/codex" and self.server.training:
                try:
                    with self.server.training.lock:
                        self.server.training._open()
                        diagnostics = self.server.training.diagnostics
                        return self.reply(
                            200,
                            diagnostics.state()
                            if diagnostics
                            else {"connection": "disabled"},
                        )
                except Exception:
                    return self.reply(
                        503,
                        {
                            "error": "Диагностика Codex недоступна. Автоматического подключения нет."
                        },
                    )
            if path.startswith("/api/local-engine/") and self.server.training:
                try:
                    training = self.server.training
                    with training.lock:
                        training._open()
                        engine = training.local_engine
                        if engine is None:
                            return self.reply(
                                404, {"error": "Локальный инженер отключён"}
                            )
                        if path == "/api/local-engine/state":
                            return self.reply(200, engine.state())
                        parts = path.split("/")
                        if len(parts) == 6 and parts[3] == "jobs":
                            return self.reply(200, engine.detail(parts[4], parts[5]))
                    return self.reply(404, {"error": "Раздел не найден"})
                except CoreError:
                    return self.reply(404, {"error": "Задача или проект недоступны"})
                except Exception:
                    return self.reply(
                        503, {"error": "Журнал локального инженера недоступен"}
                    )
            if path != "/api/state":
                return self.reply(404, {"error": "Раздел не найден"})
            try:
                return self.reply(200, self.server.dashboard())
            except Exception:
                return self.reply(
                    503,
                    {
                        "error": "Не удалось обновить наблюдения. Управление выполнением отключено."
                    },
                )
        # No file browsing and no route accepting a host pathname.
        if path == "/":
            relative = "index.html"
        elif (
            path.startswith("/assets/")
            and "\\" not in path
            and ".." not in PurePosixPath(path).parts
        ):
            relative = path[1:]
        else:
            return self.reply(404, {"error": "Страница не найдена"})
        target = self.server.static / relative
        try:
            no_links(target)
            if (
                not target.resolve().is_relative_to(self.server.static.resolve())
                or not target.is_file()
            ):
                raise FileNotFoundError()
            mime = {
                ".html": "text/html; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".svg": "image/svg+xml",
            }.get(target.suffix)
            if mime is None:
                raise FileNotFoundError()
            return self.reply(200, target.read_bytes(), mime)
        except (OSError, ValueError):
            return self.reply(404, {"error": "Ресурс недоступен"})

    def discard_small_body(self):
        # Windows can reset a connection with unread bytes before the caller
        # receives the rejection. Only drain one small unambiguous frame.
        lengths = self.headers.get_all("Content-Length", [])
        if (
            len(lengths) == 1
            and lengths[0].isascii()
            and lengths[0].isdigit()
            and len(lengths[0]) <= 4
            and int(lengths[0]) <= 8192
            and not self.headers.get("Transfer-Encoding")
        ):
            previous = self.connection.gettimeout()
            try:
                self.connection.settimeout(
                    min(previous, 1.0) if previous is not None else 1.0
                )
                self.rfile.read(int(lengths[0]))
            except OSError:
                pass  # Reject and close even if the sender stops supplying bytes.
            finally:
                try:
                    self.connection.settimeout(previous)
                except OSError:
                    pass

    def no_write(self):
        self.close_connection = True
        self.discard_small_body()
        self.reply(405, {"error": "Alpha работает только в режиме просмотра"})

    def do_POST(self):
        self.close_connection = True
        if (
            self.path
            not in (
                "/api/school/prepare",
                "/api/school/decision",
                "/api/school/functional/prepare",
                "/api/school/functional/decision",
                "/api/school/functional/revoke",
                "/api/school/education/plan",
                "/api/school/education/pilot",
                "/api/school/education/exam",
                "/api/school/education/functional",
                "/api/school/education/lesson",
                "/api/school/education/feedback",
                "/api/school/education/knowledge",
                "/api/school/education/decision",
                "/api/team/prepare",
                "/api/team/advance",
                "/api/team/decision",
                "/api/team/revoke",
                "/api/team/stop",
                "/api/team/project-draft",
                "/api/team/project-start",
                "/api/team/project-progress",
                "/api/team/project-recover",
                "/api/team/project-stop",
                "/api/team/project-control",
                "/api/team/evolution-freeze",
                "/api/team/evolution-prepare",
                "/api/team/evolution-decision",
                "/api/local-engine/prepare",
                "/api/local-engine/skill-pilot",
                "/api/local-engine/decision",
                "/api/local-engine/recovery",
                "/api/local-engine/recovery-decision",
                "/api/local-engine/stop",
            )
            or self.server.training is None
        ):
            return self.no_write()
        # Localhost is not authorization. Require an unguessable per-process
        # capability AND a separate same-origin CSRF token, no cookie authority.
        if (
            not self.trusted_request()
            or self.headers.get("Origin") != self.server.origin
            or self.headers.get("Sec-Fetch-Site") != "same-origin"
            or not secrets.compare_digest(
                self.headers.get("Authorization", "").encode(),
                ("Bearer " + self.server.token).encode(),
            )
            or not secrets.compare_digest(
                self.headers.get("X-CSRF-Token", "").encode(), self.server.csrf.encode()
            )
        ):
            # Consume only a small, framed body before closing a rejected POST.
            # Otherwise Windows may reset a connection with unread request bytes
            # and the browser loses the explicit 403. No command is evaluated.
            self.discard_small_body()
            return self.reply(
                403,
                {
                    "error": "Нужна текущая локальная сессия и подтверждённый источник запроса"
                },
            )
        lengths = self.headers.get_all("Content-Length", [])
        if (
            self.headers.get("Transfer-Encoding")
            or len(lengths) != 1
            or len(lengths[0]) > 4
            or not lengths[0].isascii()
            or not lengths[0].isdigit()
            or not 0 < int(lengths[0]) <= 4096
            or self.headers.get("Content-Type") != "application/json"
        ):
            self.discard_small_body()
            return self.reply(400, {"error": "Нужен ограниченный JSON-запрос"})
        try:
            value = _strict_json(self.rfile.read(int(lengths[0])))
            if not isinstance(value, dict):
                raise ValueError()
        except (ValueError, UnicodeError, RecursionError):
            return self.reply(400, {"error": "Некорректный JSON"})
        try:
            training = self.server.training
            if self.path.startswith("/api/local-engine/"):
                with training.lock:
                    training._open()
                    engine = training.local_engine
                    if engine is None:
                        raise CoreError("Local Engineer is disabled")
                    method = {
                        "prepare": engine.prepare,
                        "skill-pilot": engine.prepare_skill_pilot,
                        "decision": engine.decision,
                        "stop": engine.stop,
                    }
                    route = self.path.rsplit("/", 1)[-1]
                    if route in {"recovery", "recovery-decision"}:
                        result = engine.recover(value, decide=route == "recovery-decision")
                    else:
                        result = method[route](value)
                return self.reply(200, result)
            if self.path.startswith("/api/school/education/"):
                with training.lock:
                    training._open()
                    education = training.education
                    if education is None:
                        raise CoreError("School experiment is disabled")
                    method = {
                        "plan": education.create_plan, "pilot": education.prepare_pilot, "exam": education.exam,
                        "functional": education.functional, "lesson": education.lesson,
                        "feedback": education.feedback, "knowledge": education.knowledge,
                        "decision": education.decide_knowledge,
                    }[self.path.rsplit("/", 1)[-1]]
                    result = method(value)
                    return self.reply(200, result if result is not None else education.state())
            if self.path.startswith("/api/team/"):
                with training.lock:
                    training._open()
                    if training.team is None:
                        raise CoreError("Team is disabled")
                    if self.path.startswith("/api/team/project-"):
                        result = training.team.projects.command(self.path.rsplit("-", 1)[-1], value)
                        return self.reply(200, result)
                    if self.path.startswith("/api/team/evolution-"):
                        method = {"freeze": training.team.evolution.freeze,
                                  "prepare": training.team.evolution.prepare,
                                  "decision": training.team.evolution.decision}[self.path.rsplit("-", 1)[-1]]
                        return self.reply(200, method(value))
                method = {
                    "prepare": training.team.prepare,
                    "advance": training.team.advance,
                    "decision": training.team.decide,
                    "revoke": training.team.revoke,
                    "stop": training.team.stop,
                }[self.path.rsplit("/", 1)[-1]]
            elif self.path.startswith("/api/school/functional/"):
                with training.lock:
                    training._open()
                method = {
                    "prepare": training.functional.prepare,
                    "decision": training.functional.decide,
                    "revoke": training.functional.revoke,
                }[self.path.rsplit("/", 1)[-1]]
            else:
                method = (
                    training.prepare
                    if self.path.endswith("/prepare")
                    else training.decide
                )
            return self.reply(200, method(value))
        except CoreError:
            return self.reply(
                409,
                {
                    "error": "Операция отклонена Core. Обновите состояние; unknown нельзя повторять из интерфейса."
                },
            )
        except Exception:
            return self.reply(
                503,
                {
                    "error": "Результат команды не подтверждён. Обновите историю; автоматического повтора нет."
                },
            )

    do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = no_write
