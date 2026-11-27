"""Fixed-profile Docker launcher. CLI exit is never container stop evidence."""
from __future__ import annotations

import json
import queue
import re
import threading
import time

from .docker_cli import DockerCLI
from .errors import DockerApprovalRequired, DockerConfigMismatch, DockerStartUncertain, DockerUnavailable
from .identity import archived_school_review, deployment_review, source_hashes
from .profile import DockerProfile
from .wire import MAX_FRAME, fingerprint

INSPECT = ('{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},'
           '"config":{{json .Config}},"host":{{json .HostConfig}},"state":{{json .State}},'
           '"mounts":{{json .Mounts}},"networks":{{json .NetworkSettings.Networks}}}')
IMAGE = ('{"id":{{json .Id}},"os":{{json .Os}},"arch":{{json .Architecture}},'
         '"labels":{{json (index .Config "Labels")}},"env":{{json (index .Config "Env")}},'
         '"volumes":{{json (index .Config "Volumes")}}}')
INFO = ('{"os":{{json .OSType}},"arch":{{json .Architecture}},'
        '"security":{{json .SecurityOptions}},"cgroup":{{json .CgroupVersion}}}')


def validate_inspection(info, profile, job_id, run_id, epoch, container_id=None, expected_env=None):
    """Reject configuration drift, foreign identities, broad mounts and rights."""
    try:
        host, config = info["host"], info["config"]
        if not re.fullmatch(r"[a-f0-9]{64}", info["id"]) or (container_id and info["id"] != container_id):
            raise ValueError
        if (info["name"] != "/" + profile.container_name(job_id) or info["image"] != profile.image
                or any(config["Labels"].get(k) != v for k, v in profile.labels(job_id, run_id, epoch).items())):
            raise ValueError
        expected = {"Privileged": False, "ReadonlyRootfs": True, "NetworkMode": "none",
                    "Memory": 134217728, "MemorySwap": 134217728, "NanoCpus": 500000000,
                    "PidsLimit": 16, "Init": True, "PidMode": "", "UTSMode": "",
                    "IpcMode": "private", "CgroupnsMode": "private", "Runtime": "runc"}
        if any(type(host.get(k)) is not type(v) or host.get(k) != v for k, v in expected.items()):
            raise ValueError
        if (host.get("CapDrop") != ["ALL"] or host.get("CapAdd") or host.get("Binds") or host.get("Mounts")
                or host.get("Devices") or host.get("DeviceRequests") or host.get("PortBindings")
                or host.get("UsernsMode") not in ("", "private")
                or host.get("SecurityOpt") != ["no-new-privileges:true"]
                or host.get("LogConfig", {}).get("Type") != "none"):
            raise ValueError
        wanted_tmpfs = dict(item.split(":", 1) for item in profile.describe()["tmpfs"])
        if host.get("Tmpfs") != wanted_tmpfs:
            raise ValueError
        if any(m.get("Type") != "tmpfs" or m.get("Destination") not in wanted_tmpfs for m in info["mounts"]):
            raise ValueError
        if set(info["networks"]) - {"none"}:
            raise ValueError
        command = profile.describe()["entrypoint"]
        if (config["User"] != "65532:65532" or config["WorkingDir"] != "/work"
                or config["Entrypoint"] != command[:1] or config["Cmd"] != command[1:]
                or config.get("Volumes") or config.get("ExposedPorts") or config.get("Tty")):
            raise ValueError
        if expected_env is not None and sorted(config.get("Env") or []) != sorted(expected_env):
            raise ValueError
        state = info["state"]
        if type(state["Running"]) is not bool or type(state["ExitCode"]) is not int:
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise DockerConfigMismatch("Container identity or effective profile mismatch") from exc
    return info


def public_observation(info):
    """Do not publish environment values or the complete Docker inspect."""
    host, config, state = info["host"], info["config"], info["state"]
    return {"id": info["id"], "image": info["image"], "labels": config["Labels"],
            "user": config["User"], "read_only": host["ReadonlyRootfs"], "network": host["NetworkMode"],
            "cap_drop": host["CapDrop"], "security_opt": host["SecurityOpt"],
            "memory": host["Memory"], "memory_swap": host["MemorySwap"], "nano_cpus": host["NanoCpus"],
            "pids_limit": host["PidsLimit"], "tmpfs": host["Tmpfs"], "mounts": info["mounts"],
            "privileged": host["Privileged"], "cap_add": host.get("CapAdd"),
            "devices": host.get("Devices"), "device_requests": host.get("DeviceRequests"),
            "binds": host.get("Binds"), "port_bindings": host.get("PortBindings"),
            "exposed_ports": config.get("ExposedPorts"), "volumes": config.get("Volumes"),
            "ipc_namespace": host["IpcMode"], "pid_namespace": host["PidMode"],
            "uts_namespace": host["UTSMode"], "cgroup_namespace": host["CgroupnsMode"],
            "user_namespace": host.get("UsernsMode"), "runtime": host["Runtime"], "init": host["Init"],
            "entrypoint": config["Entrypoint"], "command": config["Cmd"], "working_dir": config["WorkingDir"],
            "environment_keys": sorted(x.split("=", 1)[0] for x in config.get("Env") or []),
            "state": {k: state.get(k) for k in ("Status", "Running", "ExitCode", "OOMKilled", "StartedAt", "FinishedAt")}}


class DockerBackend:
    name = "docker-fixed"

    def __init__(self, profile, *, approved_review=None, cli=None, legacy_school=False):
        if type(profile) is not DockerProfile:
            raise ValueError("Frozen DockerProfile required")
        self._profile, self._approved_review = profile, approved_review
        self._legacy_school = legacy_school
        self._cli = cli or DockerCLI()
        self._image_env = None

    @property
    def profile(self):
        return self._profile

    @property
    def review_digest(self):
        review = archived_school_review(self.profile) if self._legacy_school else deployment_review(self.profile)
        return fingerprint(review)

    def configuration(self):
        return {"backend": self.name, "profile": self.profile.describe(), "profile_digest": self.profile.digest,
                "sources": source_hashes(), "review_digest": self.review_digest,
                "launch_enabled": self._approved_review == self.review_digest,
                "cli": self._cli.identity(), "configuration_digest": fingerprint(self.profile.describe())}

    def _authorize(self):
        if self._approved_review is None or self._approved_review != self.review_digest:
            raise DockerApprovalRequired("Explicit operator approval of the current Docker review digest required")

    def prepare(self):
        self._authorize()  # no Docker calls before deployment approval
        prefix = ["--context", self.profile.context]
        info = self._cli.json([*prefix, "info", "--format", INFO])
        if (info.get("os") != "linux" or info.get("arch") not in ("amd64", "x86_64")
                or info.get("cgroup") != "2" or "name=seccomp,profile=builtin" not in info.get("security", [])):
            raise DockerConfigMismatch("Linux amd64, cgroup v2 and builtin seccomp required")
        image = self._cli.json([*prefix, "image", "inspect", "--format", IMAGE, self.profile.image])
        if (image.get("id") != self.profile.image or image.get("os") != "linux" or image.get("arch") != "amd64"
                or (image.get("labels") or {}).get("ii-pro.review") != self.review_digest or image.get("volumes")):
            raise DockerConfigMismatch("Local image does not match approved build")
        self._image_env = tuple(image.get("env") or [])

    def locator(self, job_id, run_id, epoch, container_id=None):
        return {"kind": "docker", "name": self.profile.container_name(job_id), "container_id": container_id,
                "job_id": job_id, "run_id": run_id, "epoch": epoch, "profile_digest": self.profile.digest}

    def inspect_owned(self, identity):
        cid = identity.get("container_id")
        if cid is not None and (type(cid) is not str or not re.fullmatch(r"[a-f0-9]{64}", cid)):
            raise DockerConfigMismatch("Invalid container ID")
        expected = self.locator(identity["job_id"], identity["run_id"], identity["epoch"], cid)
        if identity != expected:
            raise DockerConfigMismatch("Saved container identity mismatch")
        info = self._cli.json(["--context", self.profile.context, "container", "inspect", "--format", INSPECT, cid or expected["name"]])
        validate_inspection(info, self.profile, identity["job_id"], identity["run_id"], identity["epoch"], cid, self._image_env)
        if info["config"]["Labels"].get("ii-pro.review") != self.review_digest:
            raise DockerConfigMismatch("Container build review mismatch")
        return info

    def start(self, *, job_id=None, run_id=None, epoch=None):
        self._authorize()
        if self._image_env is None:
            raise DockerConfigMismatch("Docker readiness check required before dispatch")
        args = self.profile.create_argv(job_id, run_id, epoch)
        cid = self._cli.call(args).decode("ascii").strip()
        if not re.fullmatch(r"[a-f0-9]{64}", cid):
            raise DockerConfigMismatch("Create did not return an exact container ID")
        identity = self.locator(job_id, run_id, epoch, cid)
        info = self.inspect_owned(identity)
        if info["state"].get("Status") != "created" or info["state"]["Running"]:
            raise DockerConfigMismatch("New container is not in created state")
        # Once this Popen succeeds, return a monitor even if the CLI later fails.
        # No subsequent exception may be interpreted as proof of no execution.
        process = None
        try:
            process = self._cli.spawn(["--context", self.profile.context, "start", "--attach", "--interactive", cid])
            return DockerHandle(self, identity, process, public_observation(info))
        except BaseException as exc:
            raise DockerStartUncertain(identity, process) from exc

    def stopped_observation(self, identity):
        self._authorize()
        info = self.inspect_owned(identity)
        state = info["state"]
        if (state.get("Status") != "exited" or state["Running"] or state.get("Dead")
                or not state.get("StartedAt") or state["StartedAt"].startswith("0001-")
                or not state.get("FinishedAt") or state["FinishedAt"].startswith("0001-")):
            raise DockerConfigMismatch("Stopped execution has not been authoritatively observed")
        return public_observation(info)

    def remove_stopped(self, identity):
        observation = self.stopped_observation(identity)
        cid = observation["id"]
        result = self._cli.call(["--context", self.profile.context, "container", "rm", cid]).decode("ascii").strip()
        if result != cid:
            raise DockerUnavailable("Container removal not confirmed")
        return cid


class QueuedInput:
    """Two bounded frames; a blocked Docker stdin never blocks the Supervisor."""
    def __init__(self, process):
        self.process, self.queue, self.closed = process, queue.Queue(maxsize=2), threading.Event()
        self.thread = threading.Thread(target=self._write, daemon=True, name="ii-pro-docker-input")
        self.thread.start()

    def write(self, data):
        if not isinstance(data, bytes) or len(data) > MAX_FRAME + 4 or self.closed.is_set():
            raise OSError("Invalid or closed Docker input")
        try:
            self.queue.put_nowait(data)
        except queue.Full as exc:
            raise OSError("Docker input quota exceeded") from exc
        return len(data)

    def flush(self):
        pass

    def _write(self):
        try:
            while not self.closed.is_set() and self.process.poll() is None:
                try:
                    data = self.queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                self.process.stdin.write(data)
                self.process.stdin.flush()
        except (OSError, ValueError):
            pass

    def close(self):
        self.closed.set()
        self.thread.join(timeout=0.5)
        if self.thread.is_alive():
            raise DockerUnavailable("Docker input writer still active")
        self.process.stdin.close()


class DockerHandle:
    def __init__(self, backend, identity, process, observation):
        self.backend, self.identity, self.process = backend, identity, process
        self.stdin, self.stdout, self.stderr = QueuedInput(process), process.stdout, process.stderr
        self._exit_code, self._observation, self._error = None, observation, None
        self._lock = threading.Lock()
        self._observer = threading.Thread(target=self._observe, daemon=True, name="ii-pro-docker-inspect")
        self._observer.start()

    def _observe(self):
        while True:
            try:
                info = self.backend.inspect_owned(self.identity)
                observation = public_observation(info)
                with self._lock:
                    self._observation, self._error = observation, None
                # Both independent observations are required. CLI exit alone
                # never sets _exit_code. Created/dead/missing is not exited.
                if self.process.poll() is not None and info["state"].get("Status") == "exited":
                    stopped = self.backend.stopped_observation(self.identity)
                    with self._lock:
                        self._observation, self._exit_code = stopped, stopped["state"]["ExitCode"]
                    return
            except (DockerUnavailable, DockerConfigMismatch):
                with self._lock:
                    self._error = "container_state_unavailable"
            time.sleep(0.1)

    @property
    def observations(self):
        with self._lock:
            return {"container": json.loads(json.dumps(self._observation)), "control_error": self._error,
                    "attach_exit_code": self.process.poll()}

    def poll(self):
        with self._lock:
            return self._exit_code

    def cleanup(self):
        if self.poll() is None:
            raise DockerUnavailable("Cannot close a job without container stop evidence")
        self._observer.join(timeout=0.5)
        self.stdin.close()
        self.stdout.close()
        self.stderr.close()
        # Removal is a separate, journaled adapter operation AFTER durable stop.
