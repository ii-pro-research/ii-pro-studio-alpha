"""Benign, finite, constant probes. No commands, host paths or test code input."""
import errno
import importlib.util
import os
from pathlib import Path
import platform
import socket
import sys


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8")[:4096].strip()
    except OSError:
        return None


def resources():
    keys = ("memory.current", "memory.peak", "memory.max", "pids.current", "pids.max", "cpu.max", "cpu.stat")
    return {key: _read("/sys/fs/cgroup/" + key) for key in keys}


def smoke():
    return {"python": platform.python_version(), "effective_uid": os.geteuid() if hasattr(os, "geteuid") else None,
            "effective_gid": os.getegid() if hasattr(os, "getegid") else None, "resources": resources()}


def read_probe(path):
    """Ordinary bounded read of a fixed synthetic canary; never return contents."""
    try:
        with Path(path).open("rb") as stream:
            stream.read(1)
    except OSError as exc:
        return {"readable": False, "errno": exc.errno}
    return {"readable": True, "errno": 0}


def isolation(cancelled, *, memory=False):
    if (sys.platform != "linux" or os.geteuid() != 65532
            or Path(__file__).resolve() != Path("/opt/worker/probes.py")
            or _read("/sys/fs/cgroup/memory.max") != "134217728"):
        raise RuntimeError("Isolation probes require the fixed Docker sandbox")
    allocation = bytearray(8 * 1024 * 1024) if memory else bytearray()
    for index in range(0, len(allocation), 4096):
        allocation[index] = 1
    cancelled.wait(0.15)
    result = smoke()
    result["allocation_bytes"] = len(allocation)
    if memory:
        return result
    with socket.socket() as channel:
        channel.settimeout(0.2)
        network_errno = channel.connect_ex(("198.51.100.1", 9))  # reserved TEST-NET, no real service
    result["network"] = {"interfaces": [name for _, name in socket.if_nameindex()], "connect_errno": network_errno}
    routes = (_read("/proc/net/route") or "").splitlines()[1:]
    result["external_ipv4_routes"] = [line.split() for line in routes if line.split() and line.split()[0] != "lo"]
    ipv6 = (_read("/proc/net/ipv6_route") or "").splitlines()
    result["external_ipv6_routes"] = [line.split() for line in ipv6 if line.split() and line.split()[-1] != "lo"]
    result["core_importable"] = importlib.util.find_spec("ii_pro_core") is not None
    result["core_canary_visible"] = Path("/ii_pro_core_canary.sqlite3").exists()
    result["docker_socket_present"] = Path("/var/run/docker.sock").exists()
    result["gpu_devices"] = sorted(str(p) for p in Path("/dev").glob("nvidia*"))
    result["dri_present"] = Path("/dev/dri").exists()
    result["core_environment_keys"] = [k for k in os.environ if k.startswith("CORE_PRIVATE_")]
    # These are synthetic fixtures in our test directory, never real secrets.
    # No namespace/proc/fd tricks: just ordinary read attempts, no host mount.
    prefix = "/run/desktop/mnt/host/d/II PRO/core/.local/worker-v021-tests/stage2-canary/"
    result["host_canaries"] = {name: read_probe(prefix + name) for name in ("core.sqlite3", "private-canary.txt")}
    mounts = _read("/proc/mounts") or ""
    root_options = next((line.split()[3].split(",") for line in mounts.splitlines()
                         if len(line.split()) >= 4 and line.split()[1] == "/"), [])
    result["root_mount_read_only"] = "ro" in root_options
    try:
        descriptor = os.open("/opt/worker/readonly-test", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        result["root_write_errno"] = exc.errno
    else:
        os.close(descriptor)
        Path("/opt/worker/readonly-test").unlink()  # only the file just created by this probe
        result["root_write_errno"] = 0
    target = Path("/tmp/ii-pro-tmpfs-test")
    with target.open("xb") as stream:
        stream.write(b"fixed-private-scratch")
    result["tmpfs_write_ok"] = target.read_bytes() == b"fixed-private-scratch"
    target.unlink()
    status = _read("/proc/self/status") or ""
    result["process_security"] = {key: next((line.split(":", 1)[1].strip() for line in status.splitlines()
                                            if line.startswith(key + ":")), None)
                                  for key in ("CapEff", "NoNewPrivs", "Seccomp")}
    result["network_unreachable"] = network_errno in (errno.ENETUNREACH, errno.EHOSTUNREACH)
    return result
