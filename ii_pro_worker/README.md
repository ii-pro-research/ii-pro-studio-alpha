# Worker v0.2.1 — fixed-fixture execution

This is a limited fixed-fixture runner, **not an accepted sandbox for generated
or hostile code**. `NativeFixtureBackend` runs the same Windows user and has
that user's OS permissions. `-I -S`, a private cwd and an environment allowlist
do not deny filesystem or network access.

Finite reviewed fixtures: `echo`, `sum`, `sleep`, `raise`, `overflow`,
`disconnect`, `contract`, `smoke`, `isolation`, `memory`. The last two require
the fixed Docker sandbox, including an in-worker OS/path/cgroup guard.
Stage-two fixtures also include `malformed` (one invalid JSON frame),
`oversized` (one 16385-byte frame), and `bounded_wait` (a cooperative wait with
a fixed three-second end). These are finite fault fixtures, never arbitrary
programs, infinite loops or resource-exhaustion workloads.
There is no shell, executable, source code,
file path, import name, package installation or external service in the request.
`sleep` is capped at 1500 ms. No fixture creates child processes.

## Composition and consent

The host creates a `NativeFixtureBackend(scratch_root,
allow_trusted_fixtures=True)`, registers `WorkerAdapter(backend)` in
`Core(..., executors=[worker])`, then calls `worker.bind(core)` before accepting
work. The trusted scratch directory must already exist and have no reparse
points. Do not use working project directories for fixtures.

Create a task with `executor="worker"`, transition it to `planned`, and create
a Run with `Action.build("worker.fixed", {"fixture": "sum", "arguments":
{"values": [1, 2, 3]}})`. Every Worker action requires explicit Approval.
Use `core.approvals.request(run.id)` and `describe(approval.id)` for operator
review; only the trusted operator channel may call `decide(..., allow=True)`.
The adapter, protocol and Worker cannot approve anything. No approval is
automatically granted by this package.

After operator consent, `core.runs.execute(run.id, approval_id=approval.id)`
returns the existing Core Run contract. Backend sources, revision, limits,
Python runtime, action, attempt and policy are covered by H03 consent scope.
Changed profiles require another review. Settings are read-only and execution
uses the approved Limits snapshot.

## Protocol

Each frame is a 4-byte unsigned big-endian length followed by JSON, at most
16384 bytes. Duplicate fields, unknown fields/types, nonfinite numbers,
invalid UTF-8, bool-as-int and unexpected directions/sequences are rejected.
The envelope has exactly `version=1`, `job_id`, `run_id`, `epoch`, `seq`, `type`,
`payload`. IDs are lowercase 32-hex, epoch is the Core ownership generation.

| Direction | Message | Payload |
|---|---|---|
| Supervisor → Worker, seq 0 | request | task `{fixture, arguments}`, spec_digest, max_runtime_ms |
| Supervisor → Worker, seq 1 | cancel | empty object |
| Worker → Supervisor, seq 0 | accepted | empty object |
| Worker → Supervisor, consecutive seq | heartbeat | empty object |
| Worker → Supervisor, final seq | result | status, stdout, stderr, exit_code, request_digest |

`request_digest` covers the complete request payload including the operation
digest. Result statuses are `ok/0`, `error/1`, `cancelled/2`, `output_limit/3`.
The native process's actual exit code must agree before success. Channel EOF,
result text or a heartbeat never substitutes for observed process termination.
Protocol stdout is binary frames; task stdout/stderr are bounded text fields.
Bootstrap stderr is captured separately and bounded.

Supervisor defaults: runtime 1000 ms, heartbeat idle 500 ms, cooperative cancel
grace 200 ms; runtime can be explicitly profiled between 10 and 5000 ms. At most
64 KiB decoded frames per job, 16 queued frames, 4 KiB each task stdout/stderr,
4 KiB bootstrap stderr. This finite native harness has no OS CPU/RAM/pid quota
or hard process-creation timeout. Do not generalize its lifecycle monitor to
untrusted code or Docker CLI attachment.

## Evidence and recovery

Successful receipts, stdout and stderr become sealed SQLite BLOB artifacts.
`Artifact.relative_path` starting `db/` is a logical locator, **not a host file**.
Read it with `core.artifacts.read(artifact.id)`. Core verifies the complete
manifest, byte count and SHA-256. The Worker cannot choose paths or filenames.
The importer never calls the legacy filesystem writer for these artifacts.

Timeout/error/channel loss after dispatch yields Core `unknown`. The job
ledger keeps bounded receipts even when no successful artifacts are published.
Observed job exit and Core effect reconciliation are separate facts. A stopped
job never automatically resolves or retries an unknown Run.

An external execution lease outlives the synchronous adapter call. If a fixture
ignores cancellation, `Core.close()` raises `CoreBusy` and retains ownership
until the monitor observes exit. No host process is killed. Retry `close()` only
after active execution drains. After owner crash, an unconfirmed durable intent
quarantines **all new Worker dispatch** when the adapter binds again. A reused
PID, absence of a PID, new Approval, or operator `confirm_stopped` alone cannot
clear that quarantine. There is deliberately no blind "unlock" API. Persisted
unknown effects still require the existing registered domain verifier.

## Docker deployment and review gate

See [proposed profile](../deployment/worker/docker-profile.review.json),
[runtime lock](../deployment/worker/runtime.lock.json) and
The private v0.2.1 verification report records the original approval. The approved image is derived from
an official Python 3.12.16 Linux/amd64 platform digest and exactly five stdlib
runner files. The physical build context excludes Core, School, `.local`,
secrets and databases; it is assembled separately. No pip/apt/RUN installation.

`DockerProfile` fixes CPU 0.5, memory/swap 128 MiB, 16 pids, uid/gid 65532,
network none, readonly root, cap-drop ALL, no-new-privileges, default seccomp,
private namespaces, runc and init. Only `/tmp` has a 1 MiB noexec/nosuid/nodev
tmpfs. There are no user mounts, volumes, GPU devices, exposed ports or Docker
socket. The image must be a pinned local sha256 ID. There is no native fallback.

`python -B -m ii_pro_worker.acceptance --review` only prints the review. Pull,
build and smoke require the digest explicitly approved by the operator via
`--approved-review`; printing or reading a review is not consent. That consent
covers the base pin, actual runner bytes, recipe and resource profile. Separate
Core Approval still binds each actual built image/operation/attempt and the
trusted host implementation. Image/daemon/profile mismatches fail before start.

`DockerBackend` creates a new container, validates actual inspect data, and
starts one CLI `start --attach --interactive` with bounded queued stdin.
The monitor requires both CLI completion and independently observed container
`exited` state with valid timestamps/identity. It never equates attach exit,
404, dead, created, cancel ack or timeout with stop. Daemon failure retains the
external Core lease and unknown state. Control requests have bounded output,
timeouts and at most four unfinished CLI commands; no process is forcibly killed.

After restart, `recover_docker_job(job_id)` queries only the saved ID/name,
verifies original labels/profile and persists stop-only evidence. It never
changes an unknown Run's effect or retries it. Missing/created/running jobs
remain quarantined. A failed create acknowledgement may leave an unstarted
owned object; it is not blindly adopted, started or deleted.

`cleanup_docker_job(job_id)` requires a durable stop receipt, re-inspects the
exact owned container and uses `container rm ID` without force or volumes.
No container is removed inside the handle finalizer. Job/Run/Artifact history
and stop receipts survive removal. No global stop/prune/list-driven cleanup.

Live tests are deliberately outside default `tests/`: select only the explicitly
authorized files/cases with `--docker-image` and `--docker-review`. The older
suite includes scenarios outside the finite readiness authorization; do not run
the whole directory automatically. See the report for actual evidence and
remaining untested security cases. Ordinary Docker shares a kernel and is not
absolute protection.

## School functional readiness profile

`worker.fixed` additionally accepts `python_test` with exactly one `package`
argument validated by `school_contract.py`. The inline package has immutable
source/test/package digests, at most 4096 source bytes, 16 cases of 32 bounded
integers and 10000 canonical JSON bytes. It accepts no paths, mounts, import
locations, shell commands or arbitrary pytest arguments. The new Docker-only
`python_runner.py` executes the finite integer-function profile with a minimal
builtin set; it is never imported by host analysis. Its own location/UID/cgroup
checks supplement the launcher's actual inspect validation.

`WorkerAdapter(..., python_sources=(EXACT_SOURCE_SHA256, ...))` defaults to no
authorized Python sources. The allowlist is frozen, bound into the Core
operation approval and capped at eight sources. `python_test` refuses native
subprocess execution even for developer fixtures. The current live tests
authorize only the three source constants from `functional_curriculum.py`.
Learner-origin execution has a second default-deny gate in School. No command,
model response or Worker can grant these host-side permissions to itself.

Results include scope digests, each case's pass/fail/error, counts and cgroup
resource measurements. A completed grade can report zero passed tests while
the infrastructure Run correctly succeeds. Channel/format/timeout uncertainty
still yields `unknown`, never a fabricated failed grade. The bounded output is
imported as bytes into sealed SQLite artifacts, not through the legacy file API.

The container resource/isolation settings are unchanged. Source review hashes
cover all five runtime files and the recipe; `.gitattributes` preserves their
exact reviewed bytes. The historical `docker-profile.review.json` remains the
first-smoke proposal. The current source review/build pin and scope are in
[school readiness review](../deployment/worker/school-v02.review.json) and
private School readiness report.
