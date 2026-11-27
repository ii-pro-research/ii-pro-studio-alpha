import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup as render } from "react-dom/server";
import {
  TeamJobView,
  TeamView,
  type TeamJob,
  type TeamState,
} from "../src/Team.js";

const job: TeamJob = {
  job_id: "a".repeat(32),
  project_id: "studio-lab",
  text: "Исправить пример",
  backend: "mock",
  real_inference: false,
  contract: "Фиксированный контракт",
  workspace: {
    architect: "architect",
    engineer: "engineer",
    qwen: "qwen",
    host_project_access: false,
  },
  phases: [
    {
      phase: "plan",
      role: "architect",
      session_id: "b".repeat(32),
      run_id: "c".repeat(32),
      attempt: 1,
      status: "created",
      effect_status: "not_started",
      execution_stopped: true,
      active: false,
      error_code: null,
      approval: {
        id: "d".repeat(32),
        status: "pending",
        spec_digest: "e".repeat(64),
        expires_at: "2026-11-22T00:00:00Z",
      },
      outputs: {},
      events: [],
      artifacts: [],
    },
  ],
};
const state: TeamState = {
  version: 1,
  backend: "mock",
  real_connected: false,
  auth_status: "not_probed",
  real_launch_enabled: false,
  blocked: false,
  projects: [{ id: "studio-lab", title: "Лаборатория", user_project: false }],
  jobs: [],
};
const command = () => {};
test("An interrupted preparation restores the same saved task without dispatch", () => {
  const changed = structuredClone(job);
  changed.phases = [];
  const html = render(
    <TeamJobView
      job={changed}
      busy={false}
      blocked={false}
      command={command}
    />,
  );
  assert.match(html, /Восстановить подготовку/);
  assert.match(html, /исполнение не начиналось/);
  assert.doesNotMatch(html, /Подтвердить Mock-этап/);
});
test("Team empty state never manufactures agent results", () => {
  const html = render(
    <TeamView data={state} busy={false} command={command} prepare={command} />,
  );
  assert.match(html, /Задач команды пока нет/);
  assert.match(html, /не подключены/);
  assert.doesNotMatch(html, /100\/100/);
});
test("Mock and shared-model context separation are explicit", () => {
  const html = render(
    <TeamView data={state} busy={false} command={command} prepare={command} />,
  );
  assert.match(html, /Mock Adapter/);
  assert.match(html, /разные контексты/);
  assert.match(html, /не независимые технологии/);
  assert.match(html, /реальный запуск закрыт/);
});
test("Pending Team operation exposes exact Run, session, attempt and approval", () => {
  const html = render(
    <TeamJobView job={job} busy={false} blocked={false} command={command} />,
  );
  for (const token of [
    job.job_id,
    job.phases[0].run_id,
    job.phases[0].session_id,
    job.phases[0].approval!.spec_digest,
  ])
    assert.ok(html.includes(token));
  assert.match(html, /Подтвердить Mock-этап/);
  assert.match(html, /Отозвать разрешение/);
});
test("Unknown cannot show an execute or advance action", () => {
  const changed = structuredClone(job);
  changed.phases[0].status = "unknown";
  const html = render(
    <TeamJobView job={changed} busy={false} blocked={true} command={command} />,
  );
  assert.match(html, /повтор заблокирован/);
  assert.doesNotMatch(html, /Подтвердить Mock-этап/);
  assert.doesNotMatch(html, /Подготовить: Работа/);
});
test("Real Git diff and tests remain attributed to developer reference", () => {
  const changed = structuredClone(job);
  changed.phases[0].status = "succeeded";
  changed.phases[0].outputs = {
    "result.json": {
      diff: "+ lower()",
      git_before: "abc",
      git_after: "def",
      tests: [{ id: "spaces", passed: false }],
    },
  };
  const html = render(
    <TeamJobView
      job={changed}
      busy={false}
      blocked={false}
      command={command}
    />,
  );
  assert.match(html, /lower\(\)/);
  assert.match(html, /spaces: Провален/);
  assert.match(html, /не импортировался и не исполнялся/);
});
test("Tool output and user task are escaped", () => {
  const changed = structuredClone(job);
  changed.text = "<script>alert(1)</script>";
  changed.phases[0].outputs = {
    "result.json": { diff: "<img src=x onerror=alert(1)>" },
  };
  const html = render(
    <TeamJobView
      job={changed}
      busy={false}
      blocked={false}
      command={command}
    />,
  );
  assert.ok(!html.includes("<script>"));
  assert.ok(!html.includes("<img src=x"));
  assert.ok(html.includes("&lt;script&gt;"));
});
test("Shadow fixture does not claim verified model knowledge or Qwen execution", () => {
  const changed = structuredClone(job);
  changed.phases[0].outputs = {
    "shadow-task.json": { status: "prepared_not_run" },
  };
  const html = render(
    <TeamJobView
      job={changed}
      busy={false}
      blocked={false}
      command={command}
    />,
  );
  assert.match(html, /не проверенные знания модели/);
  assert.match(html, /Qwen не вызывалась/);
});
test("Stop UI describes cooperative signal without inventing stopped status", () => {
  const changed = structuredClone(job);
  changed.phases[0].status = "running";
  const html = render(
    <TeamJobView job={changed} busy={false} blocked={true} command={command} />,
  );
  assert.match(html, /Запросить остановку/);
  assert.match(html, /не доказывает остановку/);
  assert.doesNotMatch(html, /Безопасно остановлено/);
});
test("Busy blocks duplicate confirmation", () => {
  const html = render(
    <TeamJobView job={job} busy={true} blocked={false} command={command} />,
  );
  assert.match(html, /<button disabled="">Подтвердить Mock-этап/);
});

test("Engineer shows frozen context, handoff and its own operator permit", () => {
  const draft: TeamJob = { ...structuredClone(job), text_mode: true, engineering: true, can_execute: true, backend: "siwc_devkit" };
  const phase = draft.phases[0];
  phase.phase = "implement";
  phase.role = "engineer";
  phase.text_operation = { prompt: "pricing.py: <code>", instructions: "GPT Engineer", context: { source_job: "local-1" }, operator_permit: false };
  const html = render(<TeamJobView job={draft} busy={false} blocked={false} command={command} />);
  assert.match(html, /Передача из истории Local/);
  assert.match(html, /pricing.py: &lt;code&gt;/);
  assert.match(html, /disabled="">Подтвердить один текстовый запрос/);
  assert.doesNotMatch(html, /Подтвердить Mock-этап/);
});

test("Engineer result distinguishes checked proposal from changes or passing tests", () => {
  const outcome: TeamJob = { ...structuredClone(job), text_mode: true, engineering: true, backend: "siwc_devkit" };
  const phase = outcome.phases[0];
  phase.phase = "implement";
  phase.role = "engineer";
  phase.status = "succeeded";
  phase.outputs["engineering.json"] = {state: "proposed", candidate: {diff: "safe diff"}, functional_tests: "not_run"};
  const html = render(<TeamJobView job={outcome} busy={false} blocked={false} command={command} />);
  assert.match(html, /Предложение исправления и результаты проверки/);
  assert.match(html, /Исходники не изменялись. Функциональные тесты не проводились/);
  assert.match(html, /safe diff/);
  assert.doesNotMatch(html, /Фактические изменения Git/);
});

test("Cloud quota wait is visible and blocks text preparation", () => {
  const current = structuredClone(state);
  current.cloud = { status: "waiting_for_cloud_quota", run_id: "limited" };
  current.engineering = { supported: true, fixture_available: true, auth_ready: true, model: "gpt-6.2-sol", task: "pricing", criteria: "10%" };
  const html = render(<TeamView data={current} busy={false} command={command} prepare={command} />);
  assert.match(html, /Ожидаем восстановления лимита ChatGPT/);
  assert.match(html, /disabled="">Начать командную задачу/);
  assert.match(html, /disabled="">Подготовить запрос Engineer/);
});

test("The team view exposes the Local approval and a separate Architect review", () => {
  const joined = {...structuredClone(job), text_mode: true, engineering: true, team_local_available: true};
  const local = {
    project: "pricing", job: "local-job", task: "Inspect the proposed function", requirements: "Read only",
    status: "awaiting_approval", can_approve: true,
    results: {result: "actual read"},
    approval: {run: "run", id: "approval", digest: "digest", status: "pending", operation: {role: "local"}, expires_at: "future"},
  };
  const html = render(<TeamJobView job={{...joined, team_local: local}} busy={false} blocked={false} command={command} />);
  assert.match(html, /Подтвердить шаг Local/);
  assert.match(html, /actual read/);
  assert.doesNotMatch(html, /Подготовить итоговую проверку Architect/);
  const finished = render(<TeamJobView job={{...joined, team_local: {...local, status: "finished"}}} busy={false} blocked={false} command={command} />);
  assert.match(finished, /Подготовить итоговую проверку Architect/);
  assert.doesNotMatch(finished, /Подтвердить шаг Local/);
});

test("An incomplete Local attempt opens mentoring without marking a solution", () => {
  const current = {...structuredClone(job), text_mode: true, engineering: true, team_local_available: true};
  const local = {
    project: "pricing", job: "local-job", task: "Review", requirements: "Read only",
    status: "ready_for_next_step", can_approve: false, can_close_attempt: true,
    results: {}, approval: null,
  };
  const open = render(<TeamJobView job={{...current, team_local: local}} busy={false} blocked={false} command={command} />);
  assert.match(open, /Завершить попытку и передать на разбор наставнику/);
  const closed = render(<TeamJobView job={{...current, team_local: {...local, status: "stopped",
    can_close_attempt: false, attempt_outcome: "incomplete", completion_reason: "operator_closed"}}}
    busy={false} blocked={false} command={command} />);
  assert.match(closed, /Попытка завершена без решения/);
  assert.match(closed, /Подготовить итоговую проверку Architect/);
  assert.doesNotMatch(closed, /Подготовить следующий шаг Local/);
});

test("One separate Architect review after closed unknown presents the cost warning", () => {
  const current = {...structuredClone(job), text_mode: true, engineering: true, team_local_available: true,
    review_followup_available: true};
  const phase = {...current.phases[0], phase: "review", status: "unknown", abandoned_unknown: true};
  const first = render(<TeamJobView job={{...current, phases: [phase]}} busy={false} blocked={false} command={command} />);
  assert.match(first, /Подготовить один дополнительный разбор Architect/);
  assert.match(first, /может снова расходовать лимит ChatGPT/);
  const next = {...structuredClone(phase), run_id: "new-run", status: "created", abandoned_unknown: false,
    previous_review_run: phase.run_id, text_operation: {prompt: "evidence", instructions: "review", context: {}, operator_permit: true}};
  const after = render(<TeamJobView job={{...current, phases:[phase,next], review_followup_available:false}}
    busy={false} blocked={false} command={command} />);
  assert.match(after, /дополнительная попытка/);
  assert.match(after, /может дополнительно расходовать лимит/);
  assert.match(after, /<button>Подтвердить один текстовый запрос/);
  assert.doesNotMatch(after, /Подготовить один дополнительный разбор Architect/);
});
