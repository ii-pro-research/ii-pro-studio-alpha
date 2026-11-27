"""Trusted protocol monitor; each backend supplies independent job-stop proof."""
from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
import time

from .wire import MAX_OUTPUT, ProtocolError, encode, fingerprint, packet, read


class WorkerUncertain(RuntimeError):
    pass


@dataclass(frozen=True)
class Limits:
    runtime_ms: int = 1000
    heartbeat_ms: int = 500
    cancel_grace_ms: int = 200

    def __post_init__(self):
        for value, low, high in ((self.runtime_ms, 10, 5000), (self.heartbeat_ms, 100, 2000),
                                 (self.cancel_grace_ms, 10, 500)):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Invalid fixed-fixture limits")


class Supervisor:
    def __init__(self, limits):
        self.limits = limits

    def run(self, handle, request, cancellation, publish, release):
        """publish is fenced durable storage. release only after observed exit.

        If monitoring fails, a bounded-memory reaper retains the Core lease.
        Neither heartbeat nor result is accepted as evidence of process exit.
        """
        messages = queue.Queue(maxsize=16)
        stop_reader = threading.Event()
        stderr_bytes = bytearray()
        stderr_exceeded = threading.Event()

        def post(value):
            while not stop_reader.is_set():
                try:
                    messages.put(value, timeout=0.02)
                    return
                except queue.Full:
                    continue

        def receive():
            total = 0
            try:
                while True:
                    value = read(handle.stdout)
                    total += len(encode(value))
                    if total > 65536:
                        raise ProtocolError("Channel quota exceeded")
                    post(("frame", value))
            except EOFError:
                post(("eof", None))
            except Exception:
                post(("protocol_error", None))
                # Drain without retaining data. Never allocate based on a bad header.
                try:
                    while handle.stdout.read(1024):
                        pass
                except (OSError, ValueError):
                    pass

        def receive_stderr():
            try:
                while data := handle.stderr.read(1024):
                    available = max(0, MAX_OUTPUT - len(stderr_bytes))
                    stderr_bytes.extend(data[:available])
                    if len(data) > available:
                        stderr_exceeded.set()
            except (OSError, ValueError):
                pass

        readers = [threading.Thread(target=receive, daemon=True), threading.Thread(target=receive_stderr, daemon=True)]
        for thread in readers:
            thread.start()
        started = last_heartbeat = time.monotonic()
        result, accepted, eof, seq, reason = None, False, False, 0, None
        cancel_sent = False
        receipt = None

        def cancel():
            nonlocal cancel_sent
            if not cancel_sent:
                cancel_sent = True
                try:
                    handle.stdin.write(encode(packet(request, "cancel", 1, {})))
                    handle.stdin.flush()
                except (OSError, ValueError):
                    pass

        def make_receipt(stopped):
            value = {"job_id": request["job_id"], "run_id": request["run_id"], "epoch": request["epoch"],
                    "request_digest": fingerprint(request["payload"]), "reason": reason,
                    "accepted": accepted, "result": result, "process_exit_code": handle.poll(),
                    "bootstrap_stderr": bytes(stderr_bytes).decode("utf-8", errors="replace"),
                    "elapsed_ms": int((time.monotonic() - started) * 1000), "stop_confirmed": stopped}
            if hasattr(handle, "observations"):
                value["backend_observations"] = handle.observations
            return value

        def finish_stopped():
            stop_reader.set()
            for thread in readers:
                thread.join(timeout=0.2)
            if any(t.is_alive() for t in readers):
                raise WorkerUncertain("Channels have not closed")
            handle.cleanup()

        try:
            publish("running", handle.identity, None, False)
            handle.stdin.write(encode(request))
            handle.stdin.flush()
            while True:
                now = time.monotonic()
                if cancellation.is_set():
                    reason = "cancelled"
                    break
                if (now - started) * 1000 >= self.limits.runtime_ms:
                    reason = "timeout"
                    break
                if stderr_exceeded.is_set():
                    reason = "stderr_limit"
                    break
                if not eof and (now - last_heartbeat) * 1000 >= self.limits.heartbeat_ms:
                    reason = "heartbeat_lost"
                    break
                try:
                    kind, value = messages.get(timeout=0.01)
                except queue.Empty:
                    if handle.poll() is not None and eof:
                        break
                    continue
                if kind == "eof":
                    eof = True
                    if result is None:
                        reason = "channel_lost"
                        break
                    continue
                if kind == "protocol_error":
                    reason = "protocol_error"
                    break
                if any(value[k] != request[k] for k in ("job_id", "run_id", "epoch", "version")) or value["seq"] != seq:
                    reason = "scope_or_sequence"
                    break
                seq += 1
                last_heartbeat = time.monotonic()
                if not accepted:
                    if value["type"] != "accepted":
                        reason = "missing_acceptance"
                        break
                    accepted = True
                    publish("running", handle.identity, make_receipt(False), False)
                elif value["type"] == "heartbeat" and result is None:
                    pass
                elif value["type"] == "result" and result is None:
                    if value["payload"]["request_digest"] != fingerprint(request["payload"]):
                        reason = "result_scope"
                        break
                    result = value["payload"]
                else:
                    reason = "unexpected_message"
                    break
            if reason is not None:
                cancel()
                deadline = time.monotonic() + self.limits.cancel_grace_ms / 1000
                while handle.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
            stopped = handle.poll() is not None
            if stopped:
                stop_reader.set()
                for thread in readers:
                    thread.join(timeout=0.2)
                if any(t.is_alive() for t in readers):
                    reason = reason or "channels_not_closed"
                if stderr_exceeded.is_set():
                    reason = reason or "stderr_limit"
            if reason is None and (result is None or result["status"] != "ok" or handle.poll() != result["exit_code"]):
                reason = "worker_error_or_channel_loss"
            receipt = make_receipt(stopped)
            publish("exited" if stopped else "unknown", handle.identity, receipt, stopped)
            if reason is not None or not stopped:
                raise WorkerUncertain(reason or "Stop not confirmed")
            return receipt
        finally:
            # Storage errors / ownership loss also retain real lifecycle control.
            if handle.poll() is None:
                cancel()

            def reap():
                try:
                    while handle.poll() is None:
                        if cancellation.wait(0.02):
                            cancel()
                            time.sleep(0.02)
                    finish_stopped()
                    final = make_receipt(True)
                    publish("exited", handle.identity, final, True)
                except Exception:
                    # Failed receipt does not permit retry. Durable launch intent
                    # remains quarantined after restart; the local process did exit.
                    pass
                finally:
                    if handle.poll() is not None:
                        release()

            if handle.poll() is None:
                threading.Thread(target=reap, daemon=True, name="ii-pro-fixture-reaper").start()
            else:
                try:
                    finish_stopped()
                    if receipt is None or not receipt["stop_confirmed"]:
                        try:
                            publish("exited", handle.identity, make_receipt(True), True)
                        except Exception:
                            pass  # conservative quarantine survives failed storage
                finally:
                    release()
