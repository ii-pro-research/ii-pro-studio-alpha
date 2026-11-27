import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup as render } from "react-dom/server";
import {
  App,
  Badge,
  ExamCard,
  metric,
  Resources,
  SchoolView,
} from "../src/App.js";
import type { State, Exam } from "../src/types.js";

function empty(): State {
  return {
    api_version: 1,
    read_only: true,
    observed_at: "2026-11-21T17:00:00Z",
    sources: [],
    core: { data_available: false, owner_state: "unavailable" },
    ollama: {
      status: "unavailable",
      version: null,
      model_available: false,
      model_digest: null,
      loaded: [],
    },
    worker: { daemon: "unavailable", linux: null, cgroup: null, jobs: [] },
    resources: {
      cpu_percent: null,
      ram_total_mib: null,
      ram_available_mib: null,
      gpus: [],
    },
    tasks: [],
    runs: [],
    students: [],
    models: [],
    lessons: [],
    attempts: [],
    feedback: [],
    exams: [],
    latest_exam: null,
    warnings: [],
    project: {
      name: "Fixture",
      branch: null,
      commit: null,
      language: "Python",
      components: {},
    },
    team: [
      {
        role: "GPT Architect",
        description: "Test fixture",
        status: "not_connected",
      },
      {
        role: "GPT Engineer",
        description: "Test fixture",
        status: "not_connected",
      },
    ],
  };
}
function measured(): Exam {
  return {
    id: "fixture",
    source: "fixture",
    suite_id: "fixture",
    created_at: "2026-11-21T17:00:00Z",
    run_id: "fixture-run",
    status: "succeeded",
    origin: "model_response",
    code: "def sum_positive(values):\n    return 1\n",
    code_redacted: false,
    code_sha256: "a".repeat(64),
    static_awarded: 30,
    static_available: 30,
    total_awarded: 76,
    functional: {
      passed: 4,
      total: 6,
      functional_awarded: 46,
      cases: [
        { id: "x1", status: "passed", diagnostic: "ok" },
        { id: "x2", status: "failed", diagnostic: "wrong_value" },
      ],
      resources: { memory_peak: 100, cpu_usage_usec: 10, pids_current: 1 },
    },
    evidence: null,
  };
}

test("unavailable sensors never become invented zero", () => {
  assert.equal(metric(null, " %"), "Нет данных");
  assert.equal(metric(0, " %"), "0 %");
  assert.ok(render(<Resources data={empty()} />).includes("Нет данных"));
});
test("unknown has a separate warning state", () => {
  const html = render(<Badge status="unknown" />);
  assert.ok(html.includes("Результат неизвестен"));
  assert.ok(html.includes("badge warn"));
  assert.ok(!html.includes("badge bad"));
});
test("an empty history never manufactures the first grade", () => {
  const html = render(<App data={empty()} initialPage="history" />);
  assert.ok(html.includes("История пока пуста"));
  assert.ok(!html.includes("100/100"));
});
test("exam card displays API grade rather than hardcoded 100", () => {
  const html = render(<ExamCard exam={measured()} onOpen={() => {}} />);
  assert.ok(html.includes("76"));
  assert.ok(html.includes("4/6"));
});
test("unmeasured exam has no combined score", () => {
  const data = empty();
  const exam = {
    ...measured(),
    functional: null,
    total_awarded: null,
    status: "unknown",
  };
  data.exams = [exam];
  const html = render(<SchoolView data={data} />);
  assert.ok(html.includes("Не выполнялись"));
  assert.ok(html.includes("Баллы не начислены"));
  assert.ok(!html.includes("Целостность проверена"));
});
test("model source is text, never executable markup", () => {
  const data = empty();
  data.exams = [{ ...measured(), code: "<script>window.danger()</script>" }];
  const html = render(<SchoolView data={data} />);
  assert.ok(html.includes("&lt;script&gt;"));
  assert.ok(!html.includes("<script>window"));
});
test("GPT mentors remain disconnected", () => {
  const html = render(<App data={empty()} initialPage="team" />);
  assert.ok(html.includes("GPT Architect"));
  assert.equal((html.match(/Не подключён/g) || []).length, 2);
});
test("future actions are explicitly disabled", () => {
  const html = render(<App data={empty()} initialPage="settings" />);
  assert.equal((html.match(/disabled=""/g) || []).length, 3);
  assert.ok(html.includes("STOP ALL"));
  assert.ok(html.includes("Запустить занятие"));
});
test("all required navigation sections exist in Russian", () => {
  const html = render(<App data={empty()} />);
  for (const text of [
    "Главная",
    "Команда ИИ",
    "Школа II PRO",
    "Проекты",
    "История заданий",
    "Ресурсы",
    "Настройки",
  ])
    assert.ok(html.includes(text));
});
test("connection errors show an alert and stale-state warning", () => {
  const html = render(<App data={empty()} error="Нет связи с API" />);
  assert.ok(html.includes('role="alert"'));
  assert.ok(html.includes("Данные могли устареть"));
});
test("bootstrap without data does not render successful telemetry", () => {
  const html = render(<App data={null} loading />);
  assert.ok(html.includes("Подключаемся к локальному API"));
  assert.ok(!html.includes("ПОДТВЕРЖДЕНО АРТЕФАКТАМИ"));
});
test("failed cases are not drawn as successful green checks", () => {
  const data = empty();
  data.exams = [measured()];
  assert.ok(render(<SchoolView data={data} />).includes("case bad-case"));
});
