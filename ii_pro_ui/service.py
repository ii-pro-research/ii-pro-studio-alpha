"""Aggregate safe source snapshots and honest live sensor availability."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import threading
import time

from .probes import Probes
from .reader import Source, public, read_source, sources

ROOT = Path(__file__).resolve().parents[1]


class Observatory:
    def __init__(
        self, root=ROOT, *, registered=None, probes=None, ttl=3, active_root=None
    ):
        self.root = Path(root)
        self.dynamic_sources = registered is None
        self.active_root = (
            Path(active_root).resolve() if active_root is not None else None
        )
        self.sources = tuple(registered) if registered is not None else self._sources()
        self.cache = self.root / ".local/ui-alpha/snapshots"
        self.probes = probes or Probes()
        self.ttl, self.lock, self.cached, self.at = ttl, threading.Lock(), None, 0

    def _sources(self):
        archives = sources(self.root)
        if self.active_root is None or any(
            source.root.resolve() == self.active_root for source in archives
        ):
            return archives
        return (
            *archives,
            Source("studio-active", "Текущая база Studio", self.active_root),
        )

    def state(self):
        with self.lock:
            if self.cached is not None and time.monotonic() - self.at < self.ttl:
                return self.cached
            if self.dynamic_sources:
                self.sources = self._sources()
            with ThreadPoolExecutor(max_workers=3) as pool:
                ollama = pool.submit(self.probes.ollama)
                worker = pool.submit(self.probes.worker)
                resources = pool.submit(self.probes.resources)
                observations, warnings = [], []
                for source in self.sources:
                    try:
                        observations.append(read_source(source, self.cache))
                    except Exception:
                        # Never expose sqlite errors, absolute paths or stack traces.
                        observations.append(
                            {
                                "key": source.key,
                                "title": source.title,
                                "status": "unavailable",
                            }
                        )
                        warnings.append(
                            {
                                "kind": "source",
                                "message": "Недоступен источник: "
                                + source.title
                                + ". Данные не подменяются сохранённой оценкой.",
                            }
                        )
                loaded = [s for s in observations if s["status"] == "available"]

                def combined(key):
                    return [item for s in loaded for item in s[key]]

                exams = sorted(
                    combined("exams"), key=lambda x: x["created_at"], reverse=True
                )
                attempts = sorted(
                    combined("attempts"), key=lambda x: x["created_at"], reverse=True
                )
                runs = sorted(
                    combined("runs"), key=lambda x: x["created_at"], reverse=True
                )
                live_ollama, live_worker, metrics = (
                    ollama.result(),
                    worker.result(),
                    resources.result(),
                )
            if live_ollama["status"] != "available":
                warnings.append(
                    {
                        "kind": "ollama",
                        "message": "Нет связи с Ollama. Сохранённые учебные результаты доступны отдельно.",
                    }
                )
            if (
                live_ollama["status"] == "available"
                and not live_ollama["model_available"]
            ):
                warnings.append(
                    {
                        "kind": "model",
                        "message": "Ollama доступен, но qwen7b-fast:latest не найдена. Сохранённый экзамен не подменяется.",
                    }
                )
            if live_worker["daemon"] != "available":
                warnings.append(
                    {
                        "kind": "worker",
                        "message": (
                            "Диагностика Docker отключена. Состояние Worker показано по журналу."
                            if live_worker.get("checked") is False
                            else "Нет связи с Docker. Состояние последнего Worker показано только по журналу."
                        ),
                    }
                )
            if not metrics["gpus"]:
                warnings.append(
                    {
                        "kind": "sensor",
                        "message": "Датчики NVIDIA недоступны. Неизвестные показатели отображаются как «Нет данных».",
                    }
                )
            if any(r["status"] == "unknown" for r in runs):
                warnings.append(
                    {
                        "kind": "unknown",
                        "message": "Есть неопределённый результат. Автоматический повтор запрещён.",
                    }
                )
            models = combined("models")
            pinned = next(
                (m["digest"] for m in models if m["name"] == "qwen7b-fast:latest"), None
            )
            if (
                live_ollama["model_digest"]
                and pinned
                and live_ollama["model_digest"] != pinned
            ):
                warnings.append(
                    {
                        "kind": "model",
                        "message": "Текущий digest Qwen отличается от сохранённого учебного профиля.",
                    }
                )
            self.cached = {
                "api_version": 1,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "read_only": True,
                "sources": [
                    {
                        "key": s["key"],
                        "title": s["title"],
                        "status": s["status"],
                        "owner_state": s.get("owner_state"),
                        "schema": s.get("schema"),
                    }
                    for s in observations
                ],
                "core": {
                    "data_available": bool(loaded),
                    "owner_state": (
                        "unconfirmed"
                        if any(s["owner_state"] != "closed" for s in loaded)
                        else "closed"
                    )
                    if loaded
                    else "unavailable",
                },
                "ollama": live_ollama,
                "worker": {**live_worker, "jobs": combined("jobs")},
                "resources": metrics,
                "tasks": combined("tasks"),
                "runs": runs,
                "students": combined("students"),
                "models": models,
                "lessons": combined("lessons"),
                "attempts": attempts,
                "feedback": combined("feedback"),
                "exams": exams,
                "latest_exam": next(
                    (
                        x
                        for x in exams
                        if x["functional"] and x["origin"] == "model_response"
                    ),
                    None,
                ),
                "warnings": warnings,
                "project": self.project(),
                "team": [
                    {
                        "role": "GPT Architect",
                        "description": "Учебный план и критерии",
                        "status": "not_connected",
                    },
                    {
                        "role": "GPT Engineer",
                        "description": "Разбор решений и обратная связь",
                        "status": "not_connected",
                    },
                    {
                        "role": "Qwen7B",
                        "description": "Локальный ученик · II PRO",
                        "status": "available"
                        if live_ollama["model_available"]
                        else "unavailable",
                    },
                ],
            }
            self.at = time.monotonic()
            return self.cached

    def project(self):
        branch, commit = None, None
        try:
            head = (self.root / ".git/HEAD").read_text().strip()
            if head.startswith("ref: refs/heads/"):
                ref = head[5:]
                if ".." not in ref and "\\" not in ref:
                    branch = public(ref.removeprefix("refs/heads/"), 100)
                    commit = (self.root / ".git" / ref).read_text().strip()[:7]
            elif len(head) == 40:
                commit = head[:7]
        except OSError:
            pass
        return {
            "name": "II PRO Core",
            "branch": branch,
            "commit": commit,
            "language": "Python + TypeScript",
            "components": {
                "core": (self.root / "ii_pro_core").is_dir(),
                "school": (self.root / "ii_pro_school").is_dir(),
                "worker": (self.root / "ii_pro_worker").is_dir(),
            },
        }
