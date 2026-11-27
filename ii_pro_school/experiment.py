"""One explicitly requested local inference experiment. Never run model code."""
import ctypes
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

from ii_pro_core.security import canonical, safe_root
from ii_pro_core.storage import utc_now

from .adapters import ModelProfile, OllamaStudentAdapter, StudentFailure
from .school import School

FAST_DIGEST = "e272f2e7e6b42653ba8f199076db6a3a0e71ae518a2eb7edcdff8fb8b764aa4f"


def memory_snapshot():
    if os.name == "nt":
        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]
        status = Status()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {"status": "unavailable"}
        return {"status": "observed", "total_mib": status.total // 1048576,
                "available_mib": status.available // 1048576, "gpu_free_mib": None}
    return {"status": "unavailable"}  # fail closed for this Windows-first demo


def run_first_experiment(state_dir, *, profile=None, adapter=None, resources=None):
    state = Path(state_dir).absolute()
    if state.exists() and any(state.iterdir()):
        raise ValueError("Experiment requires a fresh dedicated state directory; no implicit repeat")
    state = safe_root(state)
    evidence = safe_root(state / "evidence")
    profile = profile or (adapter.profile if adapter is not None else ModelProfile.build("qwen7b-fast:latest", FAST_DIGEST))
    if adapter is not None and adapter.profile != profile:
        raise ValueError("Experiment and adapter profiles differ")
    adapter = adapter or OllamaStudentAdapter(profile)
    result = {"format": "ii-pro-school-experiment-v1", "started_at": utc_now(),
              "status": "diagnostic", "profile": asdict(profile), "profile_digest": profile.id,
              "code_executed": False, "exam_taken": False, "mentor_connected": False,
              "verified_knowledge": False, "session_id": None, "run_id": None}
    try:
        result["resources_before"] = (resources or memory_snapshot)()
        if result["resources_before"].get("available_mib", 0) < 4096:
            raise StudentFailure("insufficient_or_unknown_memory")
        # Refuse competing loaded models, without stopping/unloading anything.
        if isinstance(adapter, OllamaStudentAdapter):
            loaded = adapter.transport.request("ps").get("models", [])
            if any(m.get("name") != profile.model for m in loaded):
                raise StudentFailure("other_model_loaded")
        with School(state, adapter) as school:
            school.install_curriculum()
            school.register_student("qwen7b", "Qwen2.5-Coder-7B local student")
            session_id = school.create_session("qwen7b")
            result["session_id"] = session_id
            run = school.prepare_attempt(session_id, "bug-positive-sum")
            result["run_id"] = run.id
            result["task_id"] = run.task_id
            result["lesson_id"] = "bug-positive-sum"
            result["prompt"] = json.loads(run.action_json)["parameters"]["prompt"]
            run = school.execute(run.id)
            result["run_status"] = run.status
            if run.status == "succeeded":
                result["assessment"] = school.assess(run.id)
                raw = school.response(run.id).encode("utf-8")
                (evidence / "response.txt").write_bytes(raw)
                result["response_sha256"] = hashlib.sha256(raw).hexdigest()
                (evidence / "mentor_report.json").write_text(canonical(school.mentor_report(run.id)), encoding="utf-8")
                result["status"] = "response_received_static_only"
            else:
                result["diagnostics"] = [asdict(e) for e in school.core.events(run.task_id) if e.kind == "school.adapter_diagnostic"]
            (evidence / "session.json").write_text(school.export_session(session_id), encoding="utf-8")
    except Exception as exc:
        # Never print server messages, env or arbitrary exception strings.
        result["error_type"] = type(exc).__name__
        result["error_code"] = exc.code if isinstance(exc, StudentFailure) else "experiment_error"
    finally:
        result["finished_at"] = utc_now()
        (evidence / "experiment.json").write_text(canonical(result), encoding="utf-8")
    return result
