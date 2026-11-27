import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup as render } from "react-dom/server";
import { GptTextView, TextPhaseResult, TransportDiagnostics } from "../src/GptText.js";
import {
  TeamJobView,
  TeamView,
  type TeamState,
  type TeamJob,
} from "../src/Team.js";

const job: TeamJob = {
  job_id: "a".repeat(32),
  project_id: "gpt-text",
  text: "План приложения",
  backend: "siwc_responses",
  real_inference: false,
  text_mode: true,
  can_execute: false,
  prompt_sha256: "e".repeat(64),
  text_profile: { tools: [], file_inputs: false },
  contract: "Только текст",
  workspace: {
    architect: "none",
    engineer: "none",
    qwen: "none",
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
        expires_at: "future",
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
  auth_status: "not_checked",
  real_launch_enabled: false,
  blocked: false,
  projects: [],
  jobs: [job],
  text: {
    model: "gpt-6.2-sol",
    auth_ready: false,
    backend: "siwc_responses",
    billing: "chatgpt_plan",
    prompt: "Фиксированный запрос",
    computer_access: false,
    generation_automatically_enabled: false,
  },
};
const command = () => {};

test("Official DevKit approval explicitly exposes SDK default effort", () => {
  const data=structuredClone(job);
  data.backend="siwc_devkit";
  data.text_profile={backend:"siwc_devkit", effort:"sdk_default", effort_description:"Стандарт SDK; reasoning effort не задаётся"};
  const html=render(<TeamJobView job={data} busy={false} blocked={false} command={command} />);
  assert.match(html,/sdk_default/);
  assert.match(html,/reasoning effort не задаётся/);
  assert.match(html,/siwc_devkit/);
  assert.match(html,/<button disabled="">Подтвердить один текстовый запрос/);
});

test("Official SDK does not fabricate unavailable usage or wire counts", () => {
  const phase=structuredClone(job.phases[0]);
  phase.status="succeeded";
  phase.outputs={"result.json":{text:"Developer fixture", usage:null}};
  phase.events=[{session_id:phase.session_id,role:"architect",sequence:1,
    agent_event:{kind:"status",data:{type:"transport.diagnostic",outcome:"completed",sse_events:null,received_bytes:null,response_created_received:null,response_completed_received:true}}}];
  const html=render(<TextPhaseResult phase={phase} model="gpt-6.2-sol" />);
  assert.match(html,/response.completed/);
  assert.match(html,/Нет данных/);
  assert.doesNotMatch(html,/total_tokens|input_tokens|output_tokens/);
});

test("Text preview exposes exact operation but no host permit cannot launch", () => {
  const html = render(
    <TeamJobView job={job} busy={false} blocked={false} command={command} />,
  );
  assert.match(html, /GPT Text · план ChatGPT/);
  assert.match(html, /SHA-256 запроса/);
  assert.match(html, /операторский допуск/);
  assert.match(html, /<button disabled="">Подтвердить один текстовый запрос/);
  assert.doesNotMatch(html, /Подтвердить Mock-этап|Следующий этап/);
});
test("Missing SIWC login never enables preparation or claims active GPT", () => {
  const html = render(
    <TeamView data={state} busy={false} command={command} prepare={command} />,
  );
  assert.match(html, /Continue with ChatGPT/);
  assert.match(html, /<button disabled="">Подготовить один запрос Architect/);
  assert.match(html, /Manage usage/);
  assert.match(html, /credits/);
});
test("Real Text reply is inert text and does not become a Mock answer", () => {
  const data = structuredClone(state);
  const phase = data.jobs[0].phases[0];
  phase.status = "succeeded";
  phase.outputs = { "result.json": { text: "<script>no()</script> План" } };
  const html = render(<GptTextView data={data} />);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /Responses Text/);
  assert.doesNotMatch(html, /<script>|Mock — не ответ GPT/);
});
test("Unknown suppresses stale completion and forbids repeat", () => {
  const data = structuredClone(state);
  data.jobs[0].phases[0].status = "unknown";
  data.jobs[0].phases[0].outputs = { "result.json": { text: "STALE_REPLY" } };
  const html = render(<GptTextView data={data} />);
  assert.match(html, /unknown — повтор заблокирован/);
  assert.doesNotMatch(html, /STALE_REPLY/);
});
test("Mock Text is explicitly identified in team panel", () => {
  const data = structuredClone(state);
  data.jobs[0].backend = "siwc_text_mock";
  assert.match(render(<GptTextView data={data} />), /Mock — не ответ GPT/);
});

test("Provisional streamed text is visible without claiming completion", () => {
  const phase = structuredClone(job.phases[0]);
  phase.status = "running";
  phase.events = [{session_id: phase.session_id, role: "architect", sequence: 1,
    agent_event: {kind: "message", data: {delta: "<script>partial</script>", provisional: true}}}];
  const html = render(<TextPhaseResult phase={phase} model="gpt-6.2-sol" />);
  assert.match(html, /gpt-6.2-sol/);
  assert.match(html, /Поступающий текст/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /Ответ подтверждён|Оригинальный ответ|Shadow Learning: материал сохранён/);
  phase.status = "unknown";
  assert.match(render(<TextPhaseResult phase={phase} model="gpt-6.2-sol" />), /автоматический повтор заблокирован/);
});

test("Completed Text shows actual model duration usage and unreviewed shadow", () => {
  const phase = structuredClone(job.phases[0]);
  phase.status = "succeeded";
  phase.outputs = {"result.json": {text: "План", model: "actual-model", elapsed_seconds: 1.25, usage: {total_tokens: 30}},
    "shadow-material.json": {status: "unreviewed", verified_knowledge: false}};
  const html = render(<TextPhaseResult phase={phase} model="requested-model" />);
  assert.match(html, /actual-model/);
  assert.match(html, /1.25/);
  assert.match(html, /total_tokens/);
  assert.match(html, /response.completed/);
  assert.match(html, /ожидает проверки наставником/);
  phase.execution_stopped = false;
  assert.doesNotMatch(render(<TextPhaseResult phase={phase} model="requested-model" />), /Ответ подтверждён|Оригинальный ответ/);
});

test("Studio shows all persisted transport facts and primary failure", () => {
  const phase = structuredClone(job.phases[0]);
  phase.status = "unknown";
  phase.events = [{session_id: phase.session_id, role: "architect", sequence: 1,
    agent_event: {kind: "status", data: {
      type: "transport.diagnostic", stage: "read_sse", outcome: "uncertain_after_send",
      request_transmission: "sent", http_status: 200, content_type: "text/event-stream",
      request_id: "req_fixture", primary_exception_type: "ConnectionResetError",
      safe_message: "Соединение прервано", api_error_code: null,
      received_bytes: 123, sse_frames: 2, sse_events: 2, last_event_type: "response.output_text.delta",
      response_created_received: true, response_completed_received: false,
      termination_reason: "stream_read_failed", elapsed_seconds: 1.5,
    }}}];
  const html = render(<TextPhaseResult phase={phase} model="gpt-6.2-sol" />);
  for (const value of ["Диагностика транспорта", "ConnectionResetError", "req_fixture", "stream_read_failed", "123", "response.completed", "Нет данных"]) assert.ok(html.includes(value));
  assert.match(html, /автоматический повтор заблокирован/);
  assert.doesNotMatch(html, /Ответ подтверждён|Shadow Learning: материал сохранён/);
});

test("HTTP rejection and before-send diagnostics remain distinct in Studio", () => {
  const phase = structuredClone(job.phases[0]);
  phase.events = [{session_id: phase.session_id, role: "architect", sequence: 1,
    agent_event: {kind: "status", data: {type: "transport.diagnostic", outcome: "before_send", stage: "connect", primary_exception_type: "ConnectionRefusedError"}}}];
  assert.match(render(<TransportDiagnostics phase={phase} />), /HTTP-запрос не отправлен/);
  phase.events[0].agent_event.data.outcome = "confirmed_http_rejection";
  phase.events[0].agent_event.data.http_status = 429;
  phase.events[0].agent_event.data.api_error_code = "subscription_sharing_usage_limit_exceeded";
  const html = render(<TransportDiagnostics phase={phase} />);
  assert.match(html, /подтверждённый HTTP-отказ/);
  assert.match(html, /429/);
  assert.match(html, /subscription_sharing_usage_limit_exceeded/);
});

test("Historical unknown does not invent missing diagnostic facts", () => {
  const phase = structuredClone(job.phases[0]);
  phase.status = "unknown";
  const html = render(<TransportDiagnostics phase={phase} />);
  assert.match(html, /исторического запуска не сохранена/);
  assert.doesNotMatch(html, /HTTP-статус|Получено байтов|response.completed/);
});

test("Explicit abandonment preserves unknown and never becomes a retry permit", () => {
  const data=structuredClone(state);
  data.abandoned_unknowns=["c".repeat(32)];
  data.jobs[0].phases[0].status="unknown";
  data.jobs[0].phases[0].approval!.status="consumed";
  const html=render(<TeamView data={data} busy={false} command={command} prepare={command} />);
  assert.match(html,/признал неопределённость/);
  assert.match(html,/повтор запрещён/);
  assert.match(html,/Результат неизвестен/);
  assert.doesNotMatch(html,/<button[^>]*>Подтвердить один текстовый запрос/);
});
