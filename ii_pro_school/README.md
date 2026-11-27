# School v0.1 and v0.2 readiness

School stores lesson/student/model/session/feedback/assessment records in Core's
SQLite (introduced in schema 5, extended by readiness schema 8). Core still owns Task, Run, Artifact, attempt IDs, recovery,
owner fencing, shutdown and events. No second task engine or generated-code
runner is introduced.

From `<project-checkout>`, with existing dependencies:

```powershell
# This command sends ONE real bounded inference request. Choose a fresh path.
.\.venv\Scripts\python.exe -B -m ii_pro_school --state-dir '.local/my-new-school-session' experiment

# Read existing training catalog/export without model generation:
.\.venv\Scripts\python.exe -B -m ii_pro_school --state-dir '.local/my-new-school-session' catalog
.\.venv\Scripts\python.exe -B -m ii_pro_school --state-dir '.local/my-new-school-session' export SESSION_ID

# Explicitly attributed manual note; no GPT connection is implied:
.\.venv\Scripts\python.exe -B -m ii_pro_school --state-dir '.local/my-new-school-session' feedback RUN_ID --role gpt_engineer --author 'Operator' --text 'Review requested'
```

The Python facade supports `install_curriculum`, `add_lesson`,
`register_model`, `register_student`, `create_session`, `prepare_attempt`, `execute`, `assess`,
`add_feedback`, `revise`, `draft_knowledge`, `mentor_report`, `export_session`.
Use a registered trusted StudentAdapter. StudentAdapter's metadata and response
methods can be replaced in tests without Ollama. ModelProfile is immutable and
binds the tag digest, endpoint, options and adapter revision.

Three practice lessons and three separate private control specifications are
included. Exams cannot be dispatched through the training path. Private catalog
and export are explicit trusted-operator APIs, not OS access controls. Never
share private curriculum data with the student.

Raw decoded `message.content` is persisted as exact UTF-8 bytes in an immutable
SQLite capture before Core imports the same bytes as `response.txt`. This
deliberate staging copy preserves the answer if artifact publication fails.
Receipt, request hashes, statistics, observations and original answer are
included in deterministic JSON session exports. Copy the four files under
`STATE/evidence` for portable evidence; SQLite remains the authoritative history.
Reopen the dedicated state directory to inspect existing runs; do not rerun an
experiment command against it. Re-running inference is a new explicit experiment.

`assess` is AST-only, with at most 45/100 rubric points established and 55 points
pending functional checks. It does not execute/import/eval candidate code, prove
functional correctness, certify safety, take an exam or impersonate GPT review.
Obvious I/O calls are flagged; AST restrictions are not an isolation boundary.
Knowledge records can only be drafts with `unverified` status.

For repairs: explicitly call `revise` after a confirmed response, then prepare
sequence 2 or 3. The next immutable prompt contains the previous raw response,
its Run ID, and attributed manual mentor notes. Nothing edits the prior answer.
Oversized revision context is rejected, not silently rewritten. No implicit
retry or automatic loop is provided. Unresolved School runs block fresh
proposals in that database, even in another School session.

Ollama transport is loopback-only, ignores proxies, refuses redirects and
downloads, and limits HTTP bytes/deadlines. Interrupting its socket is not proof
that server-side inference stopped. Such failures remain `unknown` under Core's
existing reconciliation rules. The adapter advertises no cancellation support.
The first experiment checks at least 4096 MiB available RAM and refuses another
loaded model; this is a lightweight admission check, not OS CPU/GPU isolation.
Context/output/thread limits are request options. Model weights and saved
Ollama configuration are not modified. A 30-second request keep_alive is an
ephemeral cache policy, not a forced stop of inference.

## Functional evaluation: restricted v0.2 readiness profile

The existing facade optionally accepts `worker=WorkerAdapter(...)`. This adds
`school.functional` on the same Core/SQLite schema 8; it does not introduce a
second run manager. Old `assess` and historical v0.1 exports retain their
original AST-only rubric. The new rubric is separate: 30 static points
(syntax/signature/restricted language), plus up to 70 measured functional
points. No confirmed execution means no functional score or combined total.

The supported exercise is ONLY `sum_positive(values)` for bounded integer
lists. The strict package contains inline UTF-8 source, private input/expected
cases, identities and digests, never file paths or archives. Host preparation
uses AST parsing only. Compilation/execution occurs inside the reviewed Docker
runtime. Imports, attributes, subscripts, I/O, while, recursion, arbitrary calls
and unbounded iterators are rejected. This language restriction is defense in
depth and does not replace container isolation.

Trusted operator API, in order:

1. `functional.install()` and normal `register_student(...)`.
2. `create_cycle(student_id, seed=42)` snapshots the registered frozen model;
   the experiment seed must match its actual generation options.
3. `prepare_model_request(cycle, suite_id)` creates a normal School inference
   Run using public material only. It DOES NOT call Ollama. The operator must
   separately authorize any real inference and later call existing `execute`.
4. `submit_model_response(cycle, suite_id, response_run_id)` links an exact
   confirmed response to its proposal sequence. `submit_text(...)` saves an
   external, unverified answer. Neither method executes it.
5. `prepare(submission_id)` freezes one package; `prepare_run(package_id)`
   creates one Core Task/Run. Inspect the raw answer, static report, source
   digest, test package and `core.approvals.describe(...)` before granting any
   permission.
6. `functional.execute(package_id, approval_id=...)` requires an explicit Core
   Approval. Model/external answers additionally require their exact SHA-256 in
   both the Worker's immutable `python_sources` and the facade's immutable
   `learner_authorizations`. Both default to empty. Changing a source, tests,
   adapter or resource configuration requires a fresh operation approval.
7. `collect(package_id)` revalidates sealed evidence and saves a measured grade.
   Repeated collection/confirmed execution reads the same result; it does not
   start another container. `export_cycle` and `comparison` preserve raw answers,
   provenance and separate static/functional scores.

`sum-positive-baseline-v1` → `sum-positive-practice-v1` (two attempts with
attributed manual feedback before the second) → `sum-positive-control-v1` use
three disjoint six-case suites. Baseline/control permit one submission each.
`student_material` and inference prompts exclude private cases. Private suites
are trusted-host data; never give the student repository access. Feedback
does not claim an actual connected GPT mentor. Comparing different finite
suites is not proof of general quality, causation or verified knowledge.

`submit_developer_fixture` accepts only three fixed developer constants and
labels them as fixtures, never Qwen answers. Live readiness tests use only these
constants, plus the already authorized finite Worker regression matrix. There
is no default command that runs model-generated code.

Schema 8 adds immutable history and aggregate payload/BLOB quotas, with a
verified pre-upgrade backup and transactional migration. A readiness database
permits at most 32 cycles. Unknown evaluations block new submissions/dispatch;
confirmed stop alone does not establish an absent effect. Never auto-retry or
mark unknown work graded. Quotas do not cap the physical database/WAL or all
legacy filesystem storage. Legacy Windows path races and Unicode aliases remain
outside the permitted School functional route.

The readiness evidence and first-exam plan are held with the private research materials.
Execution of Qwen code still requires separate user authorization. Do not
execute proposals on the host or mark drafts verified.
