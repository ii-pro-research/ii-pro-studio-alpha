import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup as render } from "react-dom/server";
import {
  AttemptView,
  TrainingView,
  type TrainingAttempt,
  type TrainingState,
} from "../src/Training.js";

const attempt: TrainingAttempt = {
  run_id: "a".repeat(32),
  task_id: "b".repeat(32),
  session_id: "c".repeat(32),
  lesson_id: "bug-positive-sum",
  attempt: 1,
  status: "created",
  phase: "created",
  effect_status: "not_started",
  error_code: null,
  backend: "MockExecutor",
  real_inference: false,
  created_at: "2026-11-21T18:00:00Z",
  prompt: "Fixed prompt",
  approval: {
    id: "d".repeat(32),
    status: "pending",
    spec_digest: "e".repeat(64),
    expires_at: "2026-11-22T18:00:00Z",
  },
  response: null,
  response_sha256: null,
  assessment: null,
  artifacts: [],
};
function state(): TrainingState {
  return {
    mode: "mock_only",
    real_inference_enabled: false,
    code_execution_enabled: false,
    stopping: false,
    models: [
      {
        id: "mock-qwen7b",
        label: "Qwen7B — только MockExecutor",
        target: "qwen7b-fast:latest",
        actual_model: "mock-qwen7b-ui:fixture",
        options: { seed: 42 },
        deadline_seconds: 180,
      },
    ],
    lessons: [
      {
        id: "bug-positive-sum",
        revision: 1,
        title_ru: "Сумма положительных",
        requirements: "Исправить сумму",
        source: "def sum_positive(values): pass",
        restrictions: [],
        reference_criteria: ["Без изменения входа"],
        public_examples: [],
        rubric: { syntax: 10, functional_pending: 55 },
      },
    ],
    attempts: [],
  };
}

test("lesson preparation exposes real protocol choices without claiming inference", () => {
  const html = render(<TrainingView data={state()} />);
  assert.ok(html.includes("Подготовить занятие"));
  assert.ok(html.includes("MockExecutor"));
  assert.ok(
    html.includes("Реальный вызов Ollama и исполнение кода заблокированы"),
  );
  assert.ok(html.includes("Без изменения входа"));
  assert.ok(!html.includes("Подтвердить MockExecutor"));
  assert.ok(!html.includes("100/100"));
});
test("pending approval requires a separate explicit decision and shows exact identity", () => {
  const data = state();
  data.attempts = [attempt];
  const html = render(<TrainingView data={data} />);
  for (const text of [
    "Подтвердить MockExecutor",
    "Отклонить",
    attempt.task_id,
    attempt.run_id,
    attempt.approval!.spec_digest,
    "Точный запрос этой операции",
  ])
    assert.ok(html.includes(text));
});
test("unknown blocks preparing another lesson and never shows confirmation", () => {
  const data = state();
  data.attempts = [{ ...attempt, status: "unknown", phase: "unknown" }];
  const html = render(<TrainingView data={data} />);
  assert.ok(html.includes('disabled=""'));
  assert.ok(html.includes("повтор заблокирован"));
  assert.ok(!html.includes("Подтвердить MockExecutor"));
});
test("model output is escaped and mock provenance stays visible", () => {
  const html = render(
    <AttemptView
      attempt={{
        ...attempt,
        status: "succeeded",
        phase: "succeeded",
        response: "<script>fetch('/api/run')</script>",
      }}
    />,
  );
  assert.ok(html.includes("&lt;script&gt;"));
  assert.ok(!html.includes("<script>"));
  assert.ok(html.includes("Модель не вызывалась"));
  assert.ok(!html.includes("из 45"));
});
test("static grade never claims functional score or independent GPT review", () => {
  const html = render(
    <AttemptView
      attempt={{
        ...attempt,
        status: "succeeded",
        phase: "succeeded",
        assessment: {
          score_awarded: 20,
          score_available: 45,
          scores: { syntax: 10, signature: 10 },
          findings: ["Static fixture"],
          functional_status: "not_executed",
        },
      }}
    />,
  );
  assert.ok(html.includes("20 из 45"));
  assert.ok(html.includes("Функциональная оценка отсутствует"));
  assert.ok(html.includes("Экспертиза GPT не проводилась"));
  assert.ok(!html.includes("100/100"));
});
test("running and failed states are honest, with no invented progress percentage", () => {
  const running = render(
    <AttemptView
      attempt={{ ...attempt, status: "running", phase: "running" }}
    />,
  );
  assert.ok(running.includes("Получение тестового ответа MockExecutor"));
  assert.ok(!running.includes("100%"));
  const failed = render(
    <AttemptView
      attempt={{
        ...attempt,
        status: "failed",
        phase: "failed",
        error_code: "executor_not_started",
        diagnostics: ["ollama_unavailable"],
      }}
    />,
  );
  assert.ok(failed.includes("ollama_unavailable"));
  assert.ok(failed.includes("Автоматического повтора нет"));
});
test("busy command disables preparation and confirmation", () => {
  const data = state();
  data.attempts = [attempt];
  const html = render(<TrainingView data={data} busy />);
  assert.equal((html.match(/disabled=""/g) || []).length, 5);
});
test("history and rejected decisions survive in rendered API state", () => {
  const data = state();
  data.attempts = [
    { ...attempt, approval: { ...attempt.approval!, status: "rejected" } },
  ];
  const html = render(<TrainingView data={data} />);
  assert.ok(html.includes("отклонено"));
  assert.ok(html.includes("История занятий интерфейса · 1"));
  assert.ok(!html.includes("Подтвердить MockExecutor"));
});
