"""Called ONLY inside the approved Docker Worker, never by host analysis."""
import os
from pathlib import Path
import sys

from school_contract import validate_package, validate_result
from probes import resources


def evaluate(package):
    if sys.platform != "linux" or os.geteuid() != 65532 or Path(__file__).resolve() != Path("/opt/worker/python_runner.py"):
        raise RuntimeError("Python evaluation requires the fixed Docker sandbox")
    if Path("/sys/fs/cgroup/memory.max").read_text().strip() != "134217728":
        raise RuntimeError("Expected memory boundary")
    validate_package(package)
    pricing = package["function"] == "discounted"
    namespace = {"__builtins__": {} if pricing else {"sum": sum, "len": len, "min": min, "max": max, "abs": abs}}
    exec(compile(package["code"], "<bounded-student-function>", "exec"), namespace)
    function = namespace[package["function"]]
    results = []
    for case in package["cases"]:
        try:
            actual = function(*case["input"]) if pricing else function(list(case["input"]))
            passed = type(actual) in ((int, float) if pricing else (int,)) and actual == case["expected"]
            status, diagnostic = ("passed", "ok") if passed else ("failed", "wrong_value")
        except Exception:
            status, diagnostic = "error", "candidate_exception"
        results.append({"id": case["id"], "status": status, "diagnostic": diagnostic})
    usage = resources()
    measured = {"memory_peak": int(usage["memory.peak"]), "pids_current": int(usage["pids.current"]),
                "cpu_usage_usec": int(next(line.split()[1] for line in usage["cpu.stat"].splitlines() if line.startswith("usage_usec ")))}
    result = {"method": "pricing-arithmetic-tests-v1" if pricing else "integer-tests-v1",
              "package_id": package["id"], "package_digest": package["digest"],
              "source_sha256": package["source_sha256"], "suite_digest": package["suite_digest"], "cases": results,
              "passed": sum(r["status"] == "passed" for r in results), "total": len(results), "resources": measured}
    return validate_result(result, package)
