import argparse
from pathlib import Path
import webbrowser

from .probes import Probes
from .server import LocalServer
from .service import Observatory
from ii_pro_core.errors import CoreError


def main():
    parser = argparse.ArgumentParser(
        description="II PRO Studio — School Mock / Ollama Qwen7B"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="0 — автоматически выбрать свободный loopback-порт",
    )
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--no-docker-probe",
        action="store_true",
        help="Показывать Worker по журналу без запуска Docker CLI",
    )
    parser.add_argument("--state-dir", type=Path, help="Каталог общей базы Studio")
    parser.add_argument(
        "--local-projects", type=Path, help="Файл регистрации снимков Git"
    )
    parser.add_argument(
        "--local-agent-live",
        action="store_true",
        help="Разрешить локальный backend; для каждого шага требуется Approval в Studio",
    )
    parser.add_argument(
        "--import-mentor-memory",
        type=Path,
        help="Импортировать проверяемый комплект mentor-repair со статусом tested",
    )
    parser.add_argument(
        "--gpt-auth-dir",
        type=Path,
        help="Явно подключить защищённое хранилище SIWC Studio",
    )
    parser.add_argument(
        "--gpt-text-run",
        help="Отдельно согласованный GPT Text Run; дополнительно требуется свежий Approval",
    )
    parser.add_argument(
        "--diagnose-codex",
        metavar="REQUEST_ID",
        help="Один явный metadata-only Run; 32 hex, без GPT-потоков и инструментов",
    )
    parser.add_argument(
        "--reconcile-codex",
        metavar="RUN_ID",
        help="Отдельная проверка остановленного metadata-only Run; без автоматического повтора",
    )
    parser.add_argument(
        "--worker-run",
        help="Отдельно разрешённый Worker Run; необходим новый Core Approval",
    )
    parser.add_argument(
        "--ollama-run",
        help="Разрешённый пользователем конкретный Run (32 hex); отдельный Approval всё равно обязателен",
    )
    parser.add_argument("--engineering-image", help="Закреплённый локальный Worker image ID")
    parser.add_argument("--engineering-review", help="Согласованный SHA-256 конфигурации Worker")
    parser.add_argument("--engineering-worker-run", help="Допуск ровно одного инженерного Run")
    args = parser.parse_args()
    engineering_options = (args.engineering_image, args.engineering_review)
    if (any(engineering_options) and not all(engineering_options)
            or args.engineering_worker_run and not all(engineering_options)):
        parser.error("Для инженерных тестов нужны закреплённый образ и согласованный review digest")
    if args.local_agent_live and not args.local_projects:
        parser.error("--local-agent-live требует --local-projects")
    if args.port != 0 and not 1024 <= args.port <= 65535:
        parser.error("Используйте 0 или порт от 1024 до 65535")
    if not (Path(__file__).parent / "static/index.html").is_file():
        parser.error(
            "Нет сборки интерфейса: выполните npm ci и npm run build в папке ui"
        )
    state_dir = (
        args.state_dir or Path(__file__).resolve().parents[1] / ".local/school-v04-ui"
    )
    try:
        server = LocalServer(
            args.port,
            Observatory(
                active_root=state_dir,
                probes=Probes(docker_enabled=not args.no_docker_probe),
            ),
        )
    except OSError:
        parser.error(
            "Порт занят. Выберите другой через --port; чужие приложения не останавливаются."
        )
    url = server.origin + "/#session=" + server.token
    try:
        from .local_engine import load_projects
        from ii_pro_team.workbench import EngineeringWorker

        server.enable_training(
            state_dir,
            authorized_real_run=args.ollama_run,
            authorized_worker_run=args.worker_run,
            authorized_text_run=args.gpt_text_run,
            gpt_auth_dir=args.gpt_auth_dir,
            local_projects=load_projects(args.local_projects)
            if args.local_projects
            else None,
            local_live=args.local_agent_live,
            engineering_worker=EngineeringWorker(*engineering_options, args.engineering_worker_run)
            if all(engineering_options) else None,
        )
        if args.import_mentor_memory:
            from ii_pro_core.memory import import_mentor_repair

            with server.training.lock:
                server.training._open()
                import_mentor_repair(
                    server.training.shared_core.memory, args.import_mentor_memory
                )
        if args.reconcile_codex or args.diagnose_codex:
            with server.training.lock:
                server.training._open()
                if args.reconcile_codex:
                    server.training.diagnostics.reconcile_closed(args.reconcile_codex)
                if args.diagnose_codex:
                    result = server.training.diagnostics.run(args.diagnose_codex)
                    print(
                        "Codex diagnostics Run: "
                        + result.id
                        + " / "
                        + str(result.status),
                        flush=True,
                    )
    except (CoreError, OSError):
        server.server_close()
        parser.error(
            "Учебная база недоступна или занята другой панелью. Чужие процессы не останавливаются."
        )
    print(
        "II PRO Studio · School v0.5 · каждый real/Worker запуск требует точного Run permit + Approval · Ctrl+C завершает сервер",
        flush=True,
    )
    print(url, flush=True)  # local capability; not logged by the HTTP server
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
