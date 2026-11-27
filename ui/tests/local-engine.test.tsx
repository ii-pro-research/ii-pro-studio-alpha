import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import { JobView, MemoryView, SkillView, WorkspaceModes, type Job } from "../src/LocalEngine.js";

const job: Job = {
  project: "pricing", job: "fixture", task: "Проверить скидку", requirements: "Сохранить интерфейс функции",
  status: "awaiting_approval", can_approve: true, scope_changed: false, dispatch_pending: false,
  approval: { id: "id-approval", run: "id-run", digest: "scope-digest", status: "pending", expires_at: "2030-01-01T00:00:00Z", operation: { model: "qwen7b-fast:latest" } },
  results: {}, runs: [{ id: "id-run", status: "created", error_code: null, proposal: null, memory: [], approvals: [], artifacts: [] }],
};
const display = (value: Job, busy = false) => renderToStaticMarkup(<JobView job={value} busy={busy} command={() => {}} />);

test("skills expose provenance and absence of measured effectiveness", () => {
  const html = renderToStaticMarkup(<SkillView skills={[{
    id: "compare", version: 1, title: "Сравнение правки", status: "unreviewed", sha256: "fixture-hash",
    applies_to: "Код", inputs: ["кандидат"], steps: ["Сравнить"], tools: ["finish"], criterion: "Вывод",
    limitation: "Статика", evidence: [["J", "evidence-hash"]], history: ["v1"], trust_history: [],
  }]} pilot={[{ job: "a", case: "threshold", arm: "a", status: "planned", pass: null,
    run: null, seconds: null, error: null, tokens: null }]} prepare={() => {}} />);
  assert.match(html, /Сравнение правки · v1 · Ожидает проверки/);
  assert.match(html, /Фактических результатов пока нет/);
  assert.match(html, /evidence-hash/);
  assert.match(html, /Подготовить следующий слот A\/B/);
});

test("local consent exposes exact request and respects server execution gate", () => {
  assert.match(display(job), /Разрешить один шаг/);
  assert.match(display(job), /qwen7b-fast:latest/);
  assert.match(display({ ...job, can_approve: false }), /<button type="button" disabled="">Разрешить один шаг/);
  assert.match(display(job, true), /<button type="button" disabled="">Разрешить один шаг/);
  assert.doesNotMatch(display({ ...job, dispatch_pending: true }), /Разрешить один шаг/);
  assert.match(display({ ...job, approval: { ...job.approval!, status: "stale" } }), /Подготовить новое разрешение/);
});

test("unknown, budget, stop and stale snapshot cannot offer another step", () => {
  for (const status of ["unknown", "budget_exhausted", "stopped", "stopping_after_current_step"]) {
    const html = display({ ...job, status });
    assert.doesNotMatch(html, /Разрешить один шаг|Подготовить следующий шаг|Подготовить новое разрешение/);
  }
  assert.match(display({ ...job, status: "unknown" }), /Повтор заблокирован/);
  assert.match(display({ ...job, status: "budget_exhausted" }), /Лимит шагов исчерпан/);
  assert.match(display({ ...job, status: "running" }), /Остановить дальнейшие шаги/);
  assert.match(display({ ...job, scope_changed: true }), /Снимок проекта изменился/);
});

test("Local recovery requires evidence and a separate decision without changing historical unknown", () => {
  const blocked = { target: "historic-run", original_status: "unknown", error_code: "executor_failed",
    eligible: false, closed: false, confirmed_stop: false, evidence: null, approval: null };
  const view = (recovery: NonNullable<Job["runs"][number]["recovery"]>) => display({
    ...job, status: "unknown", approval: null, runs: [{ ...job.runs[0], status: "unknown", recovery }],
  });
  assert.match(view(blocked), /В Core нет достаточных доказательств/);
  assert.doesNotMatch(view(blocked), /Подготовить разрешение на сверку|Подтвердить административное закрытие/);
  const eligible = { ...blocked, eligible: true, evidence: { diagnostic_sha256: "sealed" } };
  assert.match(view(eligible), /Подготовить разрешение на сверку/);
  assert.match(view({ ...eligible, approval: { id: "approval", run: "new-run", digest: "digest", expires_at: "2030-01-01" } }),
    /Подтвердить административное закрытие/);
  const closed = view({ ...eligible, eligible: false, closed: true });
  assert.match(closed, /Административно закрыт/);
  assert.match(closed, /Исходный статус unknown/);
  assert.doesNotMatch(closed, /Разрешить один шаг|Подтвердить административное закрытие/);
});

test("Local environment evidence does not grant a restart or generation", () => {
  const view = (fence: NonNullable<NonNullable<Job["runs"][number]["recovery"]>["fence"]>) => display({
    ...job, status: "unknown", approval: null, runs: [{ ...job.runs[0], status: "unknown", recovery: {
      target: "known-run", original_status: "unknown", error_code: null,
      eligible: false, closed: false, confirmed_stop: false, evidence: null, approval: null, fence,
    } }],
  });
  assert.match(view({ status: "needs_witness" }), /Зафиксировать текущее поколение/);
  assert.doesNotMatch(view({ status: "restart_unproven" }), /Подготовить разрешение на новое поколение/);
  assert.match(view({ status: "ready" }), /Подготовить разрешение на новое поколение/);
  assert.match(view({ status: "ready", approval: { id: "a", run: "r", digest: "d", expires_at: "2030-01-01" } }), /Подтвердить новое поколение Local/);
  assert.match(view({ status: "fenced", epoch_run: "epoch" }), /Исторический результат неизвестен/);
  assert.doesNotMatch(view({ status: "fenced", epoch_run: "epoch" }), /Разрешить один шаг/);
});

test("observed diff, syntax, handoff and untrusted output render as data", () => {
  const html = display({
    ...job, status: "waiting_for_engineer", approval: null,
    results: { "1": { tool: "propose_patch", status: "observed", result: {
      syntax: "valid", diff: "+ <script>inert</script>", functional_tests: "not_run",
    } } },
    runs: [{ ...job.runs[0], status: "failed", error_code: "executor_failed", proposal: "<script>inert</script>",
      memory: [{ id: "memory-fixture" }] }],
    handoff: { status: "waiting_for_engineer", version: 1 },
  });
  assert.match(html, /Предложенный diff/);
  assert.match(html, /Функциональные тесты не выполнялись/);
  assert.match(html, /Пакет передачи GPT Engineer/);
  assert.match(html, /executor_failed/);
  assert.match(html, /memory-fixture/);
  assert.doesNotMatch(html, /<script>/);
});

test("memory shows evidence and revocation without write controls", () => {
  const html = renderToStaticMarkup(<MemoryView entries={[{
    id: "mentor", body: { title: "Регулярные выражения", author: "GPT mentor", observation: "Наблюдение", limitations: ["Восемь тестов"], evidence: { passed: 8 }, source: { external: "mentor-repair" }, revision: "fixture" },
    history: [
      { status: "tested", reviewer: "operator", reason: "targeted only", created_at: "2026-11-25" },
      { status: "revoked", reviewer: "operator", reason: "withdrawn", created_at: "2026-11-26" },
    ],
  }]} />);
  for (const label of ["GPT mentor", "Отозвано", "Источник и доказательства", "История статусов", "Восемь тестов", "исключена из доверенного"]) assert.ok(html.includes(label));
  assert.doesNotMatch(html, /<button/);
});

test("all three workspace modes are present", () => {
  const html = renderToStaticMarkup(<WorkspaceModes mode="Local" select={() => {}} />);
  assert.match(html, /aria-pressed="true">Local/);
  assert.match(html, /aria-pressed="false">GPT/);
  assert.match(html, /aria-pressed="false">Team/);
});
