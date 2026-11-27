"""Standalone, bounded, closed protocol; deliberately has no Core imports."""
from __future__ import annotations

import hashlib
import json
import re
import struct

MAX_FRAME = 16384
MAX_OUTPUT = 4096
TYPES = {"request", "cancel", "accepted", "heartbeat", "result"}


class ProtocolError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ProtocolError("Unexpected fields")


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ProtocolError("Invalid integer")


def text(value, limit):
    if type(value) is not str or "\0" in value:
        raise ProtocolError("Invalid text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ProtocolError("Invalid UTF-8 text") from exc
    if size > limit:
        raise ProtocolError("Text quota exceeded")


def validate_task(task):
    exact(task, {"fixture", "arguments"})
    kind, args = task["fixture"], task["arguments"]
    if type(kind) is not str:
        raise ProtocolError("Invalid fixture")
    if kind == "python_test":
        exact(args, {"package"})
        try:
            if __package__:
                from .school_contract import validate_package
            else:
                from school_contract import validate_package
            validate_package(args["package"])
        except ValueError as exc:
            raise ProtocolError("Invalid bounded Python test package") from exc
    elif kind == "app_test":
        exact(args, {"package"})
        try:
            if __package__:
                from .app_contract import validate_app_package
            else:
                from app_contract import validate_app_package
            validate_app_package(args["package"])
        except (ValueError, SyntaxError) as exc:
            raise ProtocolError("Invalid fixed application test package") from exc
    elif kind == "echo":
        exact(args, {"text"})
        text(args["text"], 1024)
    elif kind == "sum":
        exact(args, {"values"})
        if type(args["values"]) is not list or len(args["values"]) > 32:
            raise ProtocolError("Invalid list")
        for n in args["values"]:
            integer(n, -1000, 1000)
    elif kind == "sleep":
        exact(args, {"milliseconds"})
        integer(args["milliseconds"], 0, 1500)
    elif kind in {"raise", "overflow", "disconnect", "contract", "smoke", "isolation", "memory",
                  "malformed", "oversized", "bounded_wait"}:
        exact(args, set())
    else:
        raise ProtocolError("Only fixed fixtures are permitted")
    return task


def validate(packet):
    exact(packet, {"version", "job_id", "run_id", "epoch", "seq", "type", "payload"})
    integer(packet["version"], 1, 1)
    integer(packet["epoch"], 1, 2**53 - 1)
    integer(packet["seq"], 0, 10000)
    for key in ("job_id", "run_id"):
        if type(packet[key]) is not str or not re.fullmatch(r"[a-f0-9]{32}", packet[key]):
            raise ProtocolError("Invalid identity")
    kind, payload = packet["type"], packet["payload"]
    if type(kind) is not str or kind not in TYPES:
        raise ProtocolError("Unsupported message")
    if kind == "request":
        exact(payload, {"task", "spec_digest", "max_runtime_ms"})
        validate_task(payload["task"])
        integer(payload["max_runtime_ms"], 10, 5000)
        if type(payload["spec_digest"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", payload["spec_digest"]):
            raise ProtocolError("Invalid operation digest")
    elif kind == "result":
        exact(payload, {"status", "stdout", "stderr", "exit_code", "request_digest"})
        if payload["status"] not in ("ok", "error", "cancelled", "output_limit"):
            raise ProtocolError("Invalid result status")
        text(payload["stdout"], MAX_OUTPUT)
        text(payload["stderr"], MAX_OUTPUT)
        integer(payload["exit_code"], 0, 255)
        if type(payload["request_digest"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", payload["request_digest"]):
            raise ProtocolError("Invalid request digest")
        expected = {"ok": 0, "error": 1, "cancelled": 2, "output_limit": 3}
        if payload["exit_code"] != expected[payload["status"]]:
            raise ProtocolError("Contradictory result")
    else:
        exact(payload, set())
    return packet


def encode(packet):
    value = canonical(validate(packet)).encode("utf-8")
    if len(value) > MAX_FRAME:
        raise ProtocolError("Frame quota exceeded")
    return struct.pack(">I", len(value)) + value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("Duplicate key")
        result[key] = value
    return result


def decode(value):
    if len(value) > MAX_FRAME:
        raise ProtocolError("Frame quota exceeded")
    try:
        result = json.loads(value.decode("utf-8"), object_pairs_hook=_pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError("Nonfinite value")))
        return validate(result)
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError) as exc:
        raise ProtocolError("Invalid protocol message") from exc


def read(stream):
    def take(count):
        result = bytearray()
        while len(result) < count:
            part = stream.read(count - len(result))
            if not part:
                raise EOFError("Protocol channel closed")
            result.extend(part)
        return bytes(result)
    length = struct.unpack(">I", take(4))[0]
    if not 1 <= length <= MAX_FRAME:
        raise ProtocolError("Invalid frame length")
    return decode(take(length))


def packet(request, kind, seq, payload):
    return {**{k: request[k] for k in ("version", "job_id", "run_id", "epoch")},
            "seq": seq, "type": kind, "payload": payload}
