"""Only trusted finite fixtures. Never eval/exec/import user-selected code."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import threading

# -I -S deliberately excludes the application and third-party packages.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wire import MAX_FRAME, MAX_OUTPUT, encode, fingerprint, packet, read  # noqa: E402


def main():
    # An unbuffered duplicate avoids a daemon reader holding CPython's stdin
    # buffered lock during interpreter shutdown.
    input_stream = os.fdopen(os.dup(sys.stdin.fileno()), "rb", buffering=0)
    request = read(input_stream)
    if request["type"] != "request" or request["seq"] != 0:
        raise ValueError("First frame must be request sequence zero")
    cancelled, finished = threading.Event(), threading.Event()
    lock, sequence = threading.Lock(), 0

    def send(kind, payload):
        nonlocal sequence
        with lock:
            sys.stdout.buffer.write(encode(packet(request, kind, sequence, payload)))
            sys.stdout.buffer.flush()
            sequence += 1

    def receive_cancel():
        try:
            value = read(input_stream)
            if value != packet(request, "cancel", 1, {}):
                raise ValueError("Invalid cancellation scope")
        except (EOFError, ValueError, OSError):
            pass
        cancelled.set()  # loss of supervisor channel is cancellation too

    def heartbeat():
        while not finished.wait(0.05):
            try:
                send("heartbeat", {})
            except (OSError, ValueError):
                cancelled.set()
                return

    send("accepted", {})
    threading.Thread(target=receive_cancel, daemon=True).start()
    pulse = threading.Thread(target=heartbeat, daemon=True)
    pulse.start()
    task = request["payload"]["task"]
    kind, args = task["fixture"], task["arguments"]
    status, stdout, stderr, exit_code = "ok", "", "", 0
    try:
        if kind == "echo":
            stdout = args["text"]
        elif kind == "python_test":
            from python_runner import evaluate
            from wire import canonical
            stdout = canonical(evaluate(args["package"]))
        elif kind == "app_test":
            from app_runner import evaluate
            from wire import canonical
            stdout = canonical(evaluate(args["package"]))
        elif kind == "sum":
            stdout = str(sum(args["values"]))
        elif kind == "sleep":
            cancelled.wait(args["milliseconds"] / 1000)
        elif kind == "bounded_wait":
            cancelled.wait(3.0)  # fixed finite task, never an infinite loop
        elif kind in {"malformed", "oversized"}:
            finished.set()
            pulse.join()
            body = b'{"type":' if kind == "malformed" else b"x" * (MAX_FRAME + 1)
            # Deliberate bounded protocol faults: one invalid frame, exit 0.
            # The Supervisor must not confuse process success with valid output.
            sys.stdout.buffer.write(len(body).to_bytes(4, "big") + body)
            sys.stdout.buffer.flush()
            return 0
        elif kind == "raise":
            raise RuntimeError("Fixed fixture error")
        elif kind == "overflow":
            # Exactly one byte beyond the cap, never a resource-heavy flood.
            stdout = "x" * (MAX_OUTPUT + 1)
        elif kind == "disconnect":
            os._exit(7)  # finite fixture; no children or external side effects
        elif kind == "contract":
            from wire import canonical
            stdout = canonical({"core_importable": importlib.util.find_spec("ii_pro_core") is not None,
                                "environment_keys": sorted(os.environ),
                                "docker_socket_present": Path("/var/run/docker.sock").exists()})
        elif kind in {"smoke", "isolation", "memory"}:
            from probes import isolation, smoke
            from wire import canonical
            stdout = canonical(smoke() if kind == "smoke" else isolation(cancelled, memory=kind == "memory"))
        if cancelled.is_set():
            status, stdout, stderr, exit_code = "cancelled", "", "Cancellation observed", 2
    except Exception:
        status, stderr, exit_code = "error", "Fixed fixture RuntimeError", 1
    if len(stdout.encode("utf-8")) > MAX_OUTPUT or len(stderr.encode("utf-8")) > MAX_OUTPUT:
        status, stdout, stderr, exit_code = "output_limit", "", "Fixed output exceeded quota", 3
    finished.set()
    pulse.join()
    send("result", {"status": status, "stdout": stdout, "stderr": stderr, "exit_code": exit_code,
                    "request_digest": fingerprint(request["payload"])})
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
