import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup as render } from "react-dom/server";
import { FunctionalView, type FunctionalCheck } from "../src/Functional.js";

function check(): FunctionalCheck {
  return {
    source_run_id: "a".repeat(32),
    run_id: "b".repeat(32),
    task_id: "c".repeat(32),
    attempt: 1,
    status: "created",
    phase: "created",
    error_code: null,
    code: "def sum_positive(values):\n    return 0\n",
    code_sha256: "d".repeat(64),
    answer_sha256: "e".repeat(64),
    origin: "model_response",
    suite_id: "sum-positive-baseline-v1",
    suite_digest: "f".repeat(64),
    package_id: "1".repeat(32),
    package_digest: "2".repeat(64),
    cases: [
      { id: "b0", input: [], expected: 0 },
      { id: "b1", input: [2, -1, 3], expected: 5 },
    ],
    worker_configuration: {
      backend: {
        profile: {
          image: "sha256:" + "3".repeat(64),
          network: "none",
          mounts: [],
          read_only: true,
        },
      },
      limits: { runtime_ms: 5000 },
    },
    approval: {
      id: "4".repeat(32),
      status: "pending",
      spec_digest: "5".repeat(64),
      expires_at: "2026-11-21T22:00:00Z",
    },
    dispatch_allowed: false,
    static: { awarded: 30, available: 30, eligible: true },
    functional: null,
    stdout: null,
    stderr: null,
    exit_code: null,
    stop_confirmed: false,
    worker_reason: null,
    evidence_sealed: false,
    observations: [],
    artifacts: [],
  };
}
test("functional preparation does not claim execution or a score", () => {
  const html = render(<FunctionalView />);
  assert.ok(html.includes("Подготовить функциональную проверку"));
  assert.ok(html.includes("Подготовка не исполняет код"));
  assert.ok(!html.includes("Функциональная оценка:"));
});
test("review contains immutable source, contract, package and Worker identity", () => {
  const c = check(),
    html = render(<FunctionalView check={c} />);
  for (const value of [
    c.source_run_id,
    c.run_id,
    c.code_sha256,
    c.answer_sha256,
    c.package_digest,
    c.suite_digest,
    c.approval!.spec_digest,
    "network",
    "none",
    "65532",
    "128 MiB",
    "PIDs 16",
    "5000",
  ])
    assert.ok(html.includes(value));
  assert.ok(html.includes("Отдельное подтверждение исполнения кода"));
  assert.ok(html.includes("свежий Approval"));
  assert.ok(
    /<button[^>]*disabled=""[^>]*>Подтвердить исполнение в Worker/.test(html),
  );
});
test("scope permits explicit confirmation only, no automatic approval", () => {
  const c = { ...check(), dispatch_allowed: true },
    html = render(<FunctionalView check={c} />);
  assert.ok(
    !/<button[^>]*disabled=""[^>]*>Подтвердить исполнение в Worker/.test(html),
  );
  assert.ok(html.includes("Отозвать разрешение Worker"));
  assert.ok(html.includes("Отказать в исполнении"));
  assert.ok(!html.includes("Функциональная оценка:"));
});
test("unknown blocks preparation and hides even contradictory stale scores", () => {
  const c = {
    ...check(),
    status: "unknown",
    phase: "unknown",
    functional: {
      passed: 6,
      total: 6,
      functional_awarded: 70,
      functional_available: 70,
      cases: [],
      resources: {},
    },
  };
  const html = render(<FunctionalView check={c} />);
  assert.ok(html.includes("повтор запрещён"));
  assert.ok(html.includes("Функциональные баллы не начислены"));
  assert.ok(!html.includes("Функциональная оценка:"));
  assert.ok(!html.includes("Подтвердить исполнение в Worker"));
});
test("measured functional scores and individual failures come from API", () => {
  const c = {
    ...check(),
    status: "succeeded",
    phase: "succeeded",
    stop_confirmed: true,
    evidence_sealed: true,
    functional: {
      passed: 1,
      total: 2,
      functional_awarded: 35,
      functional_available: 70,
      cases: [
        { id: "b0", status: "passed", diagnostic: "ok" },
        { id: "b1", status: "failed", diagnostic: "wrong_value" },
      ],
      resources: { memory_peak: 1024, cpu_usage_usec: 1000, pids_current: 1 },
    },
  };
  const html = render(<FunctionalView check={c} />);
  assert.ok(html.includes("Функциональные тесты: 1/2"));
  assert.ok(html.includes("35/70"));
  assert.ok(html.includes("провален"));
  assert.ok(html.includes("wrong_value"));
  assert.ok(html.includes("Статический допуск к Worker: 30/30"));
  assert.ok(!html.includes("100/100"));
});
test("no stop proof means no displayed functional points", () => {
  const c = {
    ...check(),
    status: "succeeded",
    phase: "succeeded",
    stop_confirmed: false,
    evidence_sealed: true,
    functional: {
      passed: 6,
      total: 6,
      functional_awarded: 70,
      functional_available: 70,
      cases: [],
      resources: {},
    },
  };
  assert.ok(
    !render(<FunctionalView check={c} />).includes("Функциональная оценка:"),
  );
});
test("code and stderr are inert escaped text", () => {
  const c = {
    ...check(),
    code: "<script>bad()</script>",
    stderr: "<img src=x onerror=bad()>",
    stdout: "",
    exit_code: 1,
  };
  const html = render(<FunctionalView check={c} />);
  assert.ok(html.includes("&lt;script&gt;"));
  assert.ok(html.includes("&lt;img"));
  assert.ok(!html.includes("<script>bad"));
});
test("revoked decision remains in history and offers no execution button", () => {
  const c = check();
  c.approval!.status = "revoked";
  const html = render(<FunctionalView check={c} />);
  assert.ok(html.includes("отозвано"));
  assert.ok(!html.includes("Подтвердить исполнение в Worker"));
});
test("busy and running conditions prevent duplicate commands", () => {
  const html = render(<FunctionalView check={check()} busy />);
  assert.equal((html.match(/disabled=""/g) || []).length, 4);
  const c = { ...check(), status: "running", phase: "running" };
  assert.ok(
    render(<FunctionalView check={c} />).includes(
      "Выполняются фиксированные тесты",
    ),
  );
});
