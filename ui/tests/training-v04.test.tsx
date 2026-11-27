import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup as render } from "react-dom/server";
import {
  AttemptView,
  TrainingView,
  attemptPhase,
  resumableRequest,
  type TrainingAttempt,
  type TrainingState,
} from "../src/Training.js";

const real: TrainingAttempt = {
  run_id: "a".repeat(32),
  task_id: "b".repeat(32),
  session_id: "c".repeat(32),
  lesson_id: "bug-positive-sum",
  attempt: 1,
  status: "created",
  phase: "created",
  effect_status: "not_started",
  error_code: null,
  backend: "Ollama Qwen7B",
  real_inference: true,
  model_id: "ollama-qwen7b",
  created_at: "2026-11-21T19:00:00Z",
  prompt: "Immutable original prompt",
  approval: {
    id: "d".repeat(32),
    status: "pending",
    spec_digest: "e".repeat(64),
    expires_at: "2026-11-22T19:00:00Z",
  },
  response: null,
  response_sha256: null,
  assessment: null,
  artifacts: [],
  dispatch_allowed: false,
  profile: {
    model: "qwen7b-fast:latest",
    expected_digest: "f".repeat(64),
    options_json: '{"seed":42}',
    deadline_seconds: 180,
  },
  executor: "school_ollama",
  executor_revision: "ui-ollama-text-v1",
  policy_revision: "core-policy-v1",
};
function state(): TrainingState {
  return {
    mode: "mock_and_ollama",
    real_inference_enabled: false,
    code_execution_enabled: false,
    stopping: false,
    models: [
      {
        id: "mock-qwen7b",
        label: "MockExecutor — тестовое выполнение",
        target: "qwen7b-fast:latest",
        actual_model: "mock-qwen7b-ui:fixture",
        options: { seed: 42 },
        deadline_seconds: 180,
      },
      {
        id: "ollama-qwen7b",
        label: "Ollama Qwen7B — реальная генерация",
        target: "qwen7b-fast:latest",
        actual_model: "qwen7b-fast:latest",
        options: { seed: 42 },
        deadline_seconds: 180,
        expected_digest: "f".repeat(64),
      },
    ],
    lessons: [
      {
        id: "bug-positive-sum",
        revision: 1,
        title_ru: "Сумма положительных",
        requirements: "Fixed",
        source: "",
        restrictions: [],
        reference_criteria: [],
        public_examples: [],
        rubric: { syntax: 10 },
      },
    ],
    attempts: [real],
  };
}
test("both real and mock modes are explicitly named", () => {
  const html = render(<TrainingView data={state()} />);
  assert.ok(html.includes("MockExecutor — тестовое выполнение"));
  assert.ok(html.includes("Ollama Qwen7B — реальная генерация"));
  assert.ok(html.includes("REAL / Ollama"));
});
test("real confirmation stays disabled without a separate host permit", () => {
  const html = render(<TrainingView data={state()} />);
  assert.ok(
    /<button[^>]*disabled=""[^>]*>Подтвердить запрос Qwen7B<\/button>/.test(
      html,
    ),
  );
  assert.ok(html.includes("отдельное подтверждение пользователя в Codex"));
  for (const text of [
    real.profile!.model,
    real.profile!.expected_digest,
    real.executor_revision!,
    real.policy_revision!,
    real.prompt,
    real.approval!.spec_digest,
  ])
    assert.ok(html.includes(text));
});
test("host permit allows explicit confirmation but does not itself launch or approve", () => {
  const data = state();
  data.attempts = [{ ...real, dispatch_allowed: true }];
  data.real_inference_enabled = true;
  const html = render(<TrainingView data={data} />);
  assert.ok(
    !/<button[^>]*disabled=""[^>]*>Подтвердить запрос Qwen7B<\/button>/.test(
      html,
    ),
  );
  assert.ok(html.includes("Подтвердить запрос Qwen7B"));
  assert.ok(html.includes("Подготовлено · ожидает решения"));
  assert.ok(!html.includes("Статический анализ:"));
});
test("real response and mock fixture cannot be confused", () => {
  const html = render(
    <AttemptView
      attempt={{
        ...real,
        status: "succeeded",
        phase: "succeeded",
        response: "ORIGINAL_QWEN_TEXT",
      }}
    />,
  );
  assert.ok(html.includes("Исходный ответ · Ollama Qwen7B"));
  assert.ok(html.includes("Оригинальный ответ локальной модели"));
  assert.ok(!html.includes("не Qwen"));
  assert.ok(!html.includes("Тестовая фикстура разработчика"));
});
test("real token statistics use measured nanoseconds and preserve genuine zero", () => {
  const html = render(
    <AttemptView
      attempt={{
        ...real,
        status: "succeeded",
        phase: "succeeded",
        response: "test",
        elapsed_seconds: 1.5,
        statistics: {
          eval_count: 48,
          eval_duration: 100000000,
          prompt_eval_count: 0,
        },
      }}
    />,
  );
  assert.ok(html.includes("1.50 с"));
  assert.ok(html.includes("480.00 ток/с"));
  assert.ok(html.includes("Входных токенов: 0"));
});
test("missing real metrics stay unavailable", () => {
  const html = render(
    <AttemptView
      attempt={{
        ...real,
        status: "unknown",
        phase: "unknown",
        response: "partial",
      }}
    />,
  );
  assert.ok(html.includes("Нет данных"));
  assert.ok(!html.includes("0.00 ток/с"));
  assert.ok(!html.includes("Статический анализ:"));
});
test("progress names the actual executor, never a fake percentage", () => {
  assert.equal(
    attemptPhase({ ...real, phase: "running" }),
    "Генерация Qwen7B через Ollama",
  );
  assert.equal(
    attemptPhase({ ...real, phase: "dispatching" }),
    "Ожидание запуска Ollama",
  );
  assert.ok(
    attemptPhase({ ...real, real_inference: false, phase: "running" }).includes(
      "MockExecutor",
    ),
  );
});
test("redacted display distinguishes original stored bytes from browser presentation", () => {
  const html = render(
    <AttemptView
      attempt={{
        ...real,
        status: "succeeded",
        phase: "succeeded",
        response: "[скрыто]",
        response_redacted: true,
      }}
    />,
  );
  assert.ok(html.includes("Оригинальные байты сохранены в School"));
});
test("unknown real operation exposes no repeat or code-execution control", () => {
  const data = state();
  data.attempts = [
    { ...real, status: "unknown", phase: "unknown", dispatch_allowed: true },
  ];
  const html = render(<TrainingView data={data} />);
  assert.ok(html.includes("повтор заблокирован"));
  assert.ok(!html.includes("Подтвердить запрос Qwen7B"));
  assert.ok(!html.includes("Запустить код"));
  assert.ok(!html.includes("Запустить Docker"));
});

test("a fresh UI session resumes the same prepared real request, never a new Run", () => {
  assert.equal(
    resumableRequest([real], real.lesson_id, "ollama-qwen7b"),
    real.session_id,
  );
  assert.equal(
    resumableRequest([real], real.lesson_id, "mock-qwen7b"),
    undefined,
  );
  assert.equal(
    resumableRequest(
      [{ ...real, status: "unknown" }],
      real.lesson_id,
      "ollama-qwen7b",
    ),
    undefined,
  );
  const html = render(<TrainingView data={state()} />);
  assert.ok(html.includes('value="ollama-qwen7b" selected=""'));
});
