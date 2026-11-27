import { strict as assert } from "node:assert";
import { test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ProjectWork, type ProjectWorkState } from "../src/ProjectWork.js";

const initial: ProjectWorkState = {
  projects: [{ id: "task-manager", title: "Локальный менеджер", files: ["app.py"] }],
  sessions: [{
    job: "sample", title: "Локальный менеджер",
    sha256: "digest", expired: false, stopped: false, blocked: false, phases: {},
    functional_blocker: "Для испытания требуется отдельно согласованный образ Worker.",
    scope: {
      project: "task-manager", files: ["app.py"], task: "Выполнить <script> как текст задачи", acceptance: "Критерии",
      models: { cloud: "GPT", local: "Qwen" }, cloud_calls: 4, local_steps: 1,
      duration_seconds: 1800, attempts_per_phase: 1, operations: [],
    },
  }],
};

function render(data: ProjectWorkState) {
  return renderToStaticMarkup(createElement(ProjectWork, { data, busy: false, command: () => {} }));
}

test("draft exposes scope consent and the real budget without an execution control", () => {
  const html = render(initial);
  assert.match(html, /Согласовать область сеанса/);
  assert.match(html, /4 облачных запросов/);
  assert.match(html, /30 минут/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /Подтвердить текстовый запрос/);
});

test("unknown blocks advancement and still offers a stop control", () => {
  const [draft] = initial.sessions;
  const html = render({ ...initial, sessions: [{
    ...draft, consent: {}, blocked: true, phases: { plan: { id: "run", status: "unknown" } },
  }] });
  assert.match(html, /Результат не подтверждён/);
  assert.match(html, /<button disabled="">Подготовить следующий шаг/);
  assert.match(html, /<button>Остановить сеанс/);
  assert.doesNotMatch(html, /Подтвердить текстовый запрос/);
});

test("a rejected completed plan is visible and an expired recovery requires renewed consent", () => {
  const [draft] = initial.sessions;
  const plan = { id: "original", status: "succeeded", result: { state: "rejected" } };
  const html = render({ ...initial, sessions: [{ ...draft, consent: {}, expired: true,
    plan_recovery_available: true, phases: { plan },
  }] });
  assert.match(html, /Ответ получен · результат отклонён проверкой/);
  assert.match(html, /Подготовить новый запрос Architect/);
  assert.doesNotMatch(html, /Подтвердить текстовый запрос/);
  const recovery = render({ ...initial, sessions: [{ ...draft, consent: {}, expired: true, renewal_available: true,
    phases: { plan, plan_recovery: { id: "new", status: "created",
      approval: { id: "fresh", status: "pending", spec_digest: "spec", expires_at: "later" },
    } },
  }] });
  assert.match(recovery, /Согласовать возобновление сеанса с прежним бюджетом/);
  assert.doesNotMatch(recovery, /Подтвердить текстовый запрос/);
});

test("a filter continuation shows the preserved Local evidence and requires new consent", () => {
  const [draft] = initial.sessions;
  const previous = { ...draft, consent: {}, filter_continuation_available: true,
    phases: { implement: { id: "rejected", status: "succeeded", result: { state: "rejected" } } },
  };
  assert.match(render({ ...initial, sessions: [previous] }), /Подготовить продолжение с точечными правками/);
  const continuation = render({ ...initial, sessions: [{ ...draft, scope: { ...draft.scope,
    cloud_calls: 2, local_steps: 0, resume_from: previous.job, edit_protocol: "fragments-v1",
  } }] });
  assert.match(continuation, /2 облачных запросов/);
  assert.match(continuation, /Локальных шагов: 0/);
  assert.match(continuation, /взяты из предыдущего сеанса/);
  assert.match(continuation, /Согласовать область сеанса/);
  assert.doesNotMatch(continuation, /Подтвердить текстовый запрос/);
});

test("autonomous draft exposes the visible grant and keeps manual operation controls hidden", () => {
  const [draft] = initial.sessions;
  const html = render({ ...initial, sessions: [{ ...draft, scope: {...draft.scope,
    autonomous: {budget: {cloud: 4, worker: 2}, tools: ["finish"]}},
    autonomy: {status: "draft", remaining: {cloud: 4, worker: 2}, journal: []},
  }] });
  assert.match(html, /Согласовать и запустить автономный сеанс/);
  assert.match(html, /облачные запросы · 4/);
  assert.match(html, /Журнал производных Approval/);
  assert.doesNotMatch(html, /Подтвердить текстовый запрос/);
});
