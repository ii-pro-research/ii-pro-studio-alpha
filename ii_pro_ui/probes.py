"""Fixed read-only diagnostics. No model generation, downloads or controls."""

import csv
import ctypes
import io
import math
import os
from pathlib import Path
import subprocess
import threading

from ii_pro_school.adapters import LoopbackTransport, StudentFailure
from ii_pro_school.experiment import memory_snapshot
from ii_pro_worker.docker_cli import DockerCLI


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def gpu_csv(text):
    result = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) == 6:
            result.append(
                dict(
                    name=row[0].strip()[:120],
                    utilization=number(row[1]),
                    used_mib=number(row[2]),
                    total_mib=number(row[3]),
                    temperature=number(row[4]),
                    power_w=number(row[5]),
                )
            )
    return result


class FixedGPU:
    """One pending process maximum; bounded capture, no forced termination."""

    def __init__(self):
        self.lock, self.busy = threading.Lock(), False

    def read(self):
        if os.name != "nt":
            return []
        exe = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32/nvidia-smi.exe"
        )
        if not exe.is_file():
            return []
        with self.lock:
            if self.busy:
                return []
            self.busy = True
        done, chunks = threading.Event(), [bytearray(), bytearray()]

        def worker():
            try:
                # NVML on Windows needs normal system/profile/temp directories;
                # forward only this allowlist, never arbitrary application secrets.
                env = {
                    k: os.environ[k]
                    for k in (
                        "SystemRoot",
                        "WINDIR",
                        "PATH",
                        "SystemDrive",
                        "ProgramFiles",
                        "ProgramFiles(x86)",
                        "ProgramW6432",
                        "LOCALAPPDATA",
                        "USERPROFILE",
                        "TEMP",
                        "TMP",
                    )
                    if k in os.environ
                }
                proc = subprocess.Popen(
                    [
                        str(exe),
                        "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                        "--format=csv,noheader,nounits",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    shell=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )

                def drain(stream, target):
                    with stream:
                        while data := stream.read(4096):
                            target.extend(data[: max(0, 8192 - len(target))])

                readers = [
                    threading.Thread(target=drain, args=(stream, target), daemon=True)
                    for stream, target in zip(
                        (proc.stdout, proc.stderr), chunks, strict=True
                    )
                ]
                for t in readers:
                    t.start()
                proc.wait()
                for t in readers:
                    t.join()
                if proc.returncode != 0:
                    chunks[0].clear()
            except OSError:
                chunks[0].clear()
            finally:
                with self.lock:
                    self.busy = False
                done.set()

        threading.Thread(
            target=worker, daemon=True, name="studio-gpu-diagnostic"
        ).start()
        if not done.wait(2):
            return []  # no kill; the pending slot prevents diagnostic floods
        return gpu_csv(chunks[0].decode("utf-8", errors="replace"))


class Probes:
    def __init__(self, *, docker_enabled=True):
        self.transport = LoopbackTransport("http://127.0.0.1:11434")
        self.docker = DockerCLI() if docker_enabled else None
        self.gpu = FixedGPU()
        self.previous_cpu = None

    def cpu(self):
        if os.name != "nt":
            return None
        idle, kernel, user = (ctypes.c_ulonglong() for _ in range(3))
        if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
        current = (idle.value, kernel.value + user.value)
        previous, self.previous_cpu = self.previous_cpu, current
        if not previous or current[1] <= previous[1]:
            return None
        return round(
            max(
                0,
                min(
                    100,
                    100 * (1 - (current[0] - previous[0]) / (current[1] - previous[1])),
                ),
            ),
            1,
        )

    def resources(self):
        memory = memory_snapshot()
        return {
            "cpu_percent": self.cpu(),
            "ram_total_mib": memory.get("total_mib"),
            "ram_available_mib": memory.get("available_mib"),
            "gpus": self.gpu.read(),
        }

    def ollama(self):
        try:
            # Only these three fixed metadata routes; never chat/show/download.
            version = self.transport.request("version", timeout=2)
            tags = self.transport.request("tags", timeout=2).get("models", [])
            loaded = self.transport.request("ps", timeout=2).get("models", [])
            model = next(
                (m for m in tags if m.get("name") == "qwen7b-fast:latest"), None
            )
            return {
                "status": "available",
                "version": version.get("version"),
                "model_available": model is not None,
                "model_digest": model.get("digest") if model else None,
                "loaded": [
                    {
                        "name": m.get("name"),
                        "size_mib": round(m["size"] / 1048576)
                        if type(m.get("size")) is int
                        else None,
                        "vram_mib": round(m["size_vram"] / 1048576)
                        if type(m.get("size_vram")) is int
                        else None,
                    }
                    for m in loaded[:20]
                ],
            }
        except (StudentFailure, ValueError, TypeError, KeyError):
            return {
                "status": "unavailable",
                "version": None,
                "model_available": False,
                "model_digest": None,
                "loaded": [],
            }

    def worker(self):
        if self.docker is None:
            return {
                "daemon": "unavailable",
                "linux": None,
                "cgroup": None,
                "checked": False,
            }
        try:
            value = self.docker.json(
                [
                    "--context",
                    "desktop-linux",
                    "info",
                    "--format",
                    '{"os":{{json .OSType}},"cgroup":{{json .CgroupVersion}}}',
                ]
            )
            return {
                "daemon": "available",
                "linux": value.get("os") == "linux",
                "cgroup": value.get("cgroup"),
            }
        except (OSError, RuntimeError, ValueError):
            return {"daemon": "unavailable", "linux": None, "cgroup": None}
