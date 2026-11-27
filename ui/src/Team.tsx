import { useEffect, useRef, useState } from "react";
import { TextPhaseResult } from "./GptText.js";
import { ProjectWork, type ProjectWorkState } from "./ProjectWork.js";

type Json = Record<string, unknown>;
export interface TeamPhase {
  phase: string;
  role: string;
  session_id: string;
  run_id: string;
  attempt: number;
  previous_review_run?: string | null;
  abandoned_unknown?: boolean;
  status: string;
  effect_status: string;
  execution_stopped: boolean;
  active: boolean;
  error_code: string | null;
  approval: {
    id: string;
    status: string;
    spec_digest: string;
    expires_at: string;
  } | null;
  approved_profile?: Json;
  text_operation?: { prompt: string; instructions: string; context: Json; operator_permit: boolean };
  outputs: Record<string, Json>;
  events: {
    session_id: string;
    role: string;
    sequence: number;
    agent_event: { kind: string; data: Json };
  }[];
  artifacts: { id: string; sha256: string; size: number }[];
}
export interface TeamJob {
  working_copy?: WorkingCopy | null;
  team_local_available?: boolean;
  team_local?: {
    project: string; job: string; task: string; requirements: string; status: string;
    can_approve: boolean; results: Json;
    can_close_attempt?: boolean; attempt_outcome?: string; completion_reason?: string;
    approval: { run: string; id: string; digest: string; status: string; operation: Json; expires_at: string } | null;
  } | null;
  team_report?: Json | null;
  review_followup_available?: boolean;
  text_mode?: boolean;
  engineering?: boolean;
  can_execute?: boolean;
  text_profile?: Record<string, unknown>;
  prompt_sha256?: string;
  job_id: string;
  project_id: string;
  text: string;
  backend: string;
  real_inference: boolean;
  contract: string;
  phases: TeamPhase[];
  workspace: {
    architect: string;
    engineer: string;
    qwen: string;
    host_project_access: boolean;
  };
}
export interface TeamState {
  project_work?: ProjectWorkState;
  cloud?: { status: string; run_id: string | null; source?: string; provider_verified?: boolean };
  engineering?: { supported: boolean; fixture_available: boolean; auth_ready: boolean; model: string | null; task: string; criteria: string };
  text?: {
    model: string | null;
    auth_ready: boolean;
    authorization_ref?: string | null;
    backend: string;
    billing: string;
    prompt: string;
    computer_access: false;
    generation_automatically_enabled: false;
  };
  version: number;
  backend: string;
  real_connected: boolean;
  auth_status: string;
  real_launch_enabled: boolean;
  blocked: boolean;
  abandoned_unknowns?: string[];
  projects: { id: string; title: string; user_project: boolean }[];
  jobs: TeamJob[];
}
type Command = (path: string, body: object) => void;
type Operation = {
  run: string; status: string;
  approval: { id: string; status: string; digest: string; expires_at: string } | null;
  artifacts: { name: string; sha256: string }[];
};
export type WorkingCopy = {
  revision: string; snapshot: string; source: Json;
  files: { path: string; before: string; after: string; before_sha256: string }[];
  diff: string; backup: string; rollback: string;
  test_contract: Json | null; test: { status: string; passed?: number; total?: number } | null;
  worker_ready: boolean; test_permitted: boolean; worker_profile: Json | null;
  phases: Partial<Record<"apply" | "test" | "rollback", Operation>>;
};
const labels: Record<string, string> = {
  plan: "План GPT Architect",
  implement: "Работа GPT Engineer",
  review: "Проверка GPT Architect",
  created: "Подготовлено — ожидает решения",
  running: "Выполняется",
  succeeded: "Завершено по журналу",
  unknown: "Результат неизвестен — повтор заблокирован",
  failed: "Ошибка",
  cancelled: "Отменено",
  pending: "Ожидает подтверждения",
  rejected: "Отказано",
  revoked: "Отозвано",
  consumed: "Использовано",
  expired: "Срок истёк",
  invalidated: "Условия изменились",
  approved: "Подтверждено",
};
function json(value: unknown) {
  return JSON.stringify(value, null, 2);
}

export function WorkingCopyView({ job, copy, blocked, command }: {
  job: string; copy: WorkingCopy; blocked: boolean; command: Command;
}) {
  const titles = { apply: "Применить правку", test: "Запустить функциональные тесты", rollback: "Восстановить исходную версию" };
  const steps = ["apply", "test", "rollback"] as const;
  const backup = { planned: "Будет сохранена при применении", sealed: "Сохранена и проверена", integrity_error: "Нарушена целостность" };
  const rollback = { not_needed: "Не применялся", baseline_selected: "Возвращена исходная версия", restored: "Исходная версия подтверждена", quarantined: "Версия заблокирована до проверки", integrity_error: "Целостность не подтверждена" };
  const testLabels: Record<string, string> = {
    pending: "Ожидает завершения", passed: "Пройдены", failed: "Есть проваленные проверки",
    mock_passed: "Mock: успешный результат сценария", mock_failed: "Mock: сценарий с провалом",
    infrastructure: "Ошибка инфраструктуры", unknown: "Исход неизвестен, повтор недоступен",
    integrity_error: "Целостность результата не подтверждена",
  };
  return <section className="panel" aria-label="Изолированная рабочая копия">
    <h3>Изолированная рабочая копия</h3>
    <p>Изменения сохраняются в Studio. Исходный проект остаётся в своей папке.</p>
    <ul>{copy.files.map(file => <li key={file.path}>{file.path}</li>)}</ul>
    <details><summary>Исходный код и предложенная замена</summary>
      {copy.files.map(file => <div key={file.path}><h4>{file.path}</h4>
        <pre>{file.before}</pre><pre>{file.after}</pre><p>SHA-256 оригинала <code>{file.before_sha256}</code></p>
      </div>)}
    </details>
    <details><summary>Разница между версиями</summary><pre>{copy.diff}</pre></details>
    <p>Резервная копия — {backup[copy.backup as keyof typeof backup] || copy.backup}</p>
    <p>Откат — {rollback[copy.rollback as keyof typeof rollback] || copy.rollback}</p>
    {!copy.worker_ready && <p>Для запуска тестов требуется согласованный образ Worker.</p>}
    {copy.test && <p>Функциональная проверка — {testLabels[copy.test.status] || copy.test.status}
      {copy.test.total !== undefined && ` · ${copy.test.passed}/${copy.test.total}`}</p>}
    {copy.test_contract && <details><summary>Контракт тестов и параметры Worker</summary>
      <pre>{json({ test: copy.test_contract, worker: copy.worker_profile })}</pre></details>}
    {copy.test && <details><summary>Результаты, вывод и доказательства</summary><pre>{json(copy.test)}</pre></details>}
    <details><summary>Происхождение предложения</summary><pre>{json({ revision: copy.revision, snapshot: copy.snapshot, source: copy.source })}</pre></details>
    {steps.map(phase => {
      const operation = copy.phases[phase];
      const approval = operation?.approval;
      const prepare = !operation && (phase === "apply" ||
        copy.backup === "sealed" && copy.phases.apply?.status === "succeeded" &&
        (phase === "test" ? copy.worker_ready && !!copy.test_contract && !copy.phases.rollback
          : !copy.phases.test || !["created", "running", "unknown"].includes(copy.phases.test.status)
            || ["rejected", "revoked", "expired", "invalidated"].includes(copy.phases.test.approval?.status || "")));
      const pending = operation?.status === "created" && approval?.status === "pending";
      const renew = operation?.status === "created" && (!approval || ["expired", "invalidated", "revoked"].includes(approval.status));
      const permitted = phase !== "test" || copy.test_permitted;
      const payload = approval && operation && { run_id: operation.run, approval_id: approval.id, spec_digest: approval.digest };
      return <div key={phase}>
        {operation && <p>{titles[phase]} · {labels[operation.status] || operation.status}</p>}
        {(prepare || renew) && <button type="button" disabled={blocked}
          onClick={() => command("advance", { job_id: job, phase })}>{renew ? "Обновить разрешение" : `Подготовить: ${titles[phase].toLowerCase()}`}</button>}
        {pending && payload && <>
          {!permitted && <p>Запуск требует допуска для этой попытки при открытии Studio.</p>}
          <button type="button" disabled={blocked || !permitted}
            onClick={() => command("decision", { ...payload, allow: true })}>{`Подтвердить: ${titles[phase].toLowerCase()}`}</button>
          <button type="button" disabled={blocked} onClick={() => command("decision", { ...payload, allow: false })}>Отклонить</button>
          <button type="button" disabled={blocked} onClick={() => command("revoke", payload)}>Отозвать разрешение</button>
        </>}
        {operation && <details><summary>Разрешение и история операции</summary><pre>{json(operation)}</pre></details>}
      </div>;
    })}
  </section>;
}

export function TeamJobView({
  job,
  busy,
  blocked,
  cloudWaiting = false,
  command,
}: {
  job: TeamJob;
  busy: boolean;
  blocked: boolean;
  cloudWaiting?: boolean;
  command: Command;
}) {
  const finished = new Set(
    job.phases
      .filter((p) => p.status === "succeeded" && p.execution_stopped)
      .map((p) => p.phase),
  );
  const unknown = job.phases.some((p) => p.status === "unknown" && !p.abandoned_unknown);
  const engineerNext = job.engineering && !job.phases.some(p => p.phase === "implement") &&
    job.phases.some(p => p.phase === "plan" && p.outputs["engineering.json"]?.state === "proposed");
  const local = job.team_local;
  const reviewPrepared = job.phases.some(p => p.phase === "review");
  const prepareLocal = job.team_local_available && (!local || local.status === "ready_for_next_step");
  const localApproval = local?.approval;
  const localChoice = local && localApproval ? {
    project: local.project, job: local.job, run: localApproval.run,
    approval: localApproval.id, digest: localApproval.digest,
  } : null;
  const next = job.text_mode
    ? null
    : !job.phases.some((p) => p.phase === "implement") && finished.has("plan")
      ? "implement"
      : !job.phases.some((p) => p.phase === "review") &&
          finished.has("implement")
        ? "review"
        : null;
  return (
    <article className="panel team-job" aria-label="Задача команды">
      <div className="panel-title">
        <h2>{job.text}</h2>
        <span className="tag">
          {job.text_mode
            ? job.backend === "siwc_text_mock"
              ? "GPT Text · Mock"
              : "GPT Text · план ChatGPT"
            : "Mock · без GPT-вызова"}
        </span>
      </div>
      <p className="mono">Задача: {job.job_id}</p>
      <p>
        {job.engineering
          ? "В облако передаётся только показанный текст учебного снимка. Инструменты и доступ к компьютеру отключены."
          : job.text_mode
          ? "Текстовая сессия Architect. Без файлов, инструментов и доступа к проектам. Разрешение генерации не даёт прав на компьютер."
          : "Проект: лаборатория Studio. Отдельные области Architect / Engineer / Qwen. Доступ к пользовательским проектам закрыт."}
      </p>
      <p>
        <strong>Контракт фиксированного примера:</strong> {job.contract}
      </p>
      {unknown && (
        <p role="alert" className="notice">
          Результат неизвестен. Необходимо отдельно проверить остановку и
          последствия через Core. Повтор и следующий этап заблокированы.
        </p>
      )}
      {job.phases.length === 0 && !job.engineering && (
        <aside className="notice">
          <p>
            Подготовка прервана до создания Run. Задача сохранена; исполнение не
            начиналось.
          </p>
          <button
            disabled={busy || blocked}
            onClick={() =>
              command("prepare", {
                request_id: job.job_id,
                project_id: job.project_id,
                text: job.text,
              })
            }
          >
            Восстановить подготовку
          </button>
        </aside>
      )}
      {job.phases.map((p) => {
        const result = p.outputs["result.json"];
        const tests = Array.isArray(result?.tests)
          ? (result.tests as { id: string; passed: boolean }[])
          : [];
        const decision = {
          run_id: p.run_id,
          approval_id: p.approval?.id,
          spec_digest: p.approval?.spec_digest,
        };
        return (
          <section
            className="team-phase"
            key={p.run_id}
            aria-label={labels[p.phase]}
          >
            <h3>
              {labels[p.phase]} · {job.text_mode ? "GPT Text" : "Mock"}
              {p.previous_review_run && " · дополнительная попытка"}
            </h3>
            <p role="status">
              {labels[p.status] || p.status}
              {p.active && " · ожидаем завершения"}
            </p>
            <p className="mono">
              Сессия: {p.session_id}
              <br />
              Run: {p.run_id} · попытка {p.attempt}
            </p>
            {p.abandoned_unknown && <p>Предыдущая операция закрыта с неопределённым результатом. Её повтор запрещён.</p>}
            {p.previous_review_run && <p className="notice">
              Предыдущий Run: {p.previous_review_run}. Новый запрос может дополнительно расходовать лимит ChatGPT.
            </p>}
            {p.error_code && (
              <p role="alert">
                Ошибка Core: {p.error_code}. Автоматического повтора нет.
              </p>
            )}
            {p.text_operation && <details open={p.status === "created"}>
              <summary>Что получит модель · контекст и точный запрос</summary>
              {typeof p.text_operation.context.source_job === "string" &&
                <p>Передача из истории Local · {p.text_operation.context.source_job}</p>}
              <pre>{p.text_operation.instructions}</pre>
              <pre>{p.text_operation.prompt}</pre>
            </details>}
            {p.approval && (
              <details open={p.status === "created"}>
                <summary>
                  Конкретное подтверждение ·{" "}
                  {labels[p.approval.status] || p.approval.status}
                </summary>
                <p className="mono">
                  Approval: {p.approval.id}
                  <br />
                  SHA-256 операции: {p.approval.spec_digest}
                </p>
                {job.text_mode && (
                  <>
                    {!p.text_operation && <p className="mono">SHA-256 запроса: {job.prompt_sha256}</p>}
                    <pre>{json(p.approved_profile || job.text_profile)}</pre>
                  </>
                )}
                <p>
                  Действует до {p.approval.expires_at}. Разрешение относится
                  {job.text_mode
                    ? "только к этой роли, запросу, аккаунту, модели и текстовому профилю."
                    : "только к этой стадии, роли, задаче и фиксированному Mock-профилю."}
                </p>
                {p.status === "created" && p.approval.status === "pending" && (
                  <div className="action-row">
                    <button
                      disabled={
                        busy || blocked || (job.text_mode && cloudWaiting) || unknown || (p.text_operation ? !p.text_operation.operator_permit : job.can_execute === false)
                      }
                      onClick={() =>
                        command("decision", { ...decision, allow: true })
                      }
                    >
                      {job.text_mode
                        ? "Подтвердить один текстовый запрос"
                        : "Подтвердить Mock-этап"}
                    </button>
                    <button
                      disabled={busy || blocked}
                      onClick={() =>
                        command("decision", { ...decision, allow: false })
                      }
                    >
                      Отказать
                    </button>
                  </div>
                )}
                {p.status === "created" &&
                  ["pending", "approved"].includes(p.approval.status) && (
                    <button
                      disabled={busy}
                      onClick={() => command("revoke", decision)}
                    >
                      Отозвать разрешение
                    </button>
                  )}
              </details>
            )}
            {job.engineering && p.status === "created" && (p.phase === "review" ||
              (p.phase === "implement" && job.phases.some(item => item.phase === "plan"))) &&
              ["expired", "invalidated"].includes(p.approval?.status || "") && (
              <button disabled={busy || blocked || cloudWaiting}
                onClick={() => command("advance", { job_id: job.job_id, phase: p.previous_review_run ? "review_followup" : p.phase })}>
                Запросить новое подтверждение этапа
              </button>
            )}
            {p.status === "created" && !unknown && !job.engineering && (
              <button
                disabled={busy || blocked}
                onClick={() =>
                  command(
                    p.phase === "plan" ? "prepare" : "advance",
                    p.phase === "plan"
                      ? {
                          request_id: job.job_id,
                          project_id: job.project_id,
                          text: job.text,
                        }
                      : { job_id: job.job_id, phase: p.phase },
                  )
                }
              >
                Обновить проект подтверждения
              </button>
            )}
            {p.status === "running" && !job.text_mode && (
              <button
                disabled={busy}
                onClick={() => command("stop", { run_id: p.run_id })}
              >
                Запросить остановку
              </button>
            )}
            {p.status === "running" && !job.text_mode && (
              <p>
                Кооперативный запрос не доказывает остановку и не отменяет
                выполненные изменения.
              </p>
            )}
            {p.status === "running" && job.text_mode && (
              <p>
                Ожидаем ответ в пределах deadline. Закрытие окна не доказывает
                остановку удалённой генерации; автоматического повтора нет.
              </p>
            )}
            {Array.isArray(result?.plan) && (
              <ol>
                {(result.plan as string[]).map((s, i) => (
                  <li key={i}>{s}</li>
                ))}
              </ol>
            )}
            {typeof result?.explanation === "string" && (
              <p>{result.explanation}</p>
            )}
            {job.text_mode && <TextPhaseResult phase={p} model={String(job.text_profile?.model || "")} />}
            {!job.text_mode && typeof result?.text === "string" && (
              <>
                <h4>Оригинальный ответ</h4>
                <pre>{result.text}</pre>
                <p>
                  Модель: {String(result.model)} · время:{" "}
                  {String(result.elapsed_seconds)} с
                </p>
                <pre>{json(result.usage)}</pre>
              </>
            )}
            {p.outputs["engineering.json"] && <details open>
              <summary>{p.role === "engineer" ? "Предложение исправления и результаты проверки" : p.phase === "review" ? "Оценка доказательств Architect" : "Структурированный план"}</summary>
              {p.role === "engineer" && <p>Исходники не изменялись. Функциональные тесты не проводились.</p>}
              <pre>{json(p.outputs["engineering.json"])}</pre>
            </details>}
            {job.text_mode && (p.text_operation ? !p.text_operation.operator_permit : job.can_execute === false) && p.status === "created" && (
              <p className="notice">
                Генерация выключена. Нужен отдельный операторский допуск для
                этого Run и свежее подтверждение Core.
              </p>
            )}
            {typeof result?.diff === "string" && (
              <details open>
                <summary>
                  Фактические изменения Git · лабораторная копия
                </summary>
                <pre>{result.diff}</pre>
                <p className="mono">
                  {String(result.git_before)} → {String(result.git_after)}
                </p>
              </details>
            )}
            {tests.length > 0 && (
              <>
                <h4>Проверки доверенного эталона и точных байтов файлов</h4>
                <p>
                  Код из рабочей области не импортировался и не исполнялся. Это
                  не тест произвольного решения ИИ.
                </p>
                <ul>
                  {tests.map((t) => (
                    <li key={t.id}>
                      {t.id}: {t.passed ? "Пройден" : "Провален"}
                    </li>
                  ))}
                </ul>
              </>
            )}
            <details>
              <summary>Журнал роли · {p.events.length} событий</summary>
              <pre>{json(p.events)}</pre>
            </details>
            {Object.keys(p.outputs).length > 0 && (
              <details>
                <summary>Открыть отчёт и доказательства</summary>
                <pre>{json(p.outputs)}</pre>
                <ul>
                  {p.artifacts.map((a) => (
                    <li className="mono" key={a.id}>
                      {a.id} · {a.size} байт · {a.sha256}
                    </li>
                  ))}
                </ul>
              </details>
            )}
            {p.outputs["shadow-task.json"] && (
              <aside className="notice">
                <strong>Опыт для Qwen подготовлен</strong>
                <p>
                  Источник: фиксированный Mock-пример. Это не проверенные знания
                  модели. Для ученика выделена отдельная область; пакет сохранён
                  как артефакт. Qwen не вызывалась.
                </p>
              </aside>
            )}
          </section>
        );
      })}
      {job.team_local_available && <section className="team-phase">
        <h3>Ограниченная подзадача Local</h3>
        {local && <>
          <p>{local.task}</p><pre>{local.requirements}</pre>
          <p role="status">{({
            awaiting_approval: "Ожидает подтверждения", ready_for_next_step: "Готов к следующему шагу",
            finished: "Отчёт сохранён", unknown: "Результат неизвестен", running: "Выполняется",
            stopped: "Остановлено", budget_exhausted: "Лимит шагов исчерпан",
            waiting_for_engineer: "Ожидает разбора",
          } as Record<string, string>)[local.status] || local.status}</p>
          {local.attempt_outcome === "incomplete" && <p className="notice">
            Попытка завершена без решения. {({
              operator_closed: "Закрыта оператором.",
              step_budget_exhausted: "Лимит шагов исчерпан.",
              candidate_budget_exhausted: "Лимит предложений исчерпан.",
            } as Record<string, string>)[local.completion_reason || ""]}
          </p>}
          {local.attempt_outcome === "model_reported" && <p>Заключение модели сохранено. Функциональная проверка не проводилась.</p>}
          <details><summary>Результаты инструментов Local</summary><pre>{json(local.results)}</pre></details>
          {localApproval && local.status === "awaiting_approval" && <details open>
            <summary>Отдельное разрешение для шага Local</summary>
            <p>Действует до {localApproval.expires_at}</p>
            <pre>{json(localApproval.operation)}</pre>
            {localChoice && localApproval.status === "pending" && <button disabled={busy || blocked || !local.can_approve}
              onClick={() => command("local-engine/decision", { ...localChoice, allow: true })}>Подтвердить шаг Local</button>}
            {localApproval.status !== "pending" && <button disabled={busy || blocked}
              onClick={() => command("advance", { job_id: job.job_id, phase: "local" })}>Обновить разрешение Local</button>}
          </details>}
        </>}
        {prepareLocal && <button disabled={busy || blocked}
          onClick={() => command("advance", { job_id: job.job_id, phase: "local" })}>
          {local ? "Подготовить следующий шаг Local" : "Подготовить подзадачу Local"}
        </button>}
        {local?.can_close_attempt && <button disabled={busy || blocked}
          onClick={() => command("advance", { job_id: job.job_id, phase: "close_local" })}>
          Завершить попытку и передать на разбор наставнику
        </button>}
        {(local?.status === "finished" || local?.attempt_outcome === "model_reported" || local?.attempt_outcome === "incomplete") && !reviewPrepared && <button disabled={busy || blocked || cloudWaiting}
          onClick={() => command("advance", { job_id: job.job_id, phase: "review" })}>
          Подготовить итоговую проверку Architect
        </button>}
        {job.review_followup_available && <div className="notice">
          Предыдущий запрос мог израсходовать лимит. Дополнительный разбор потребует нового Approval и может снова расходовать лимит ChatGPT.
          <button disabled={busy || blocked || cloudWaiting || unknown}
            onClick={() => command("advance", { job_id: job.job_id, phase: "review_followup" })}>
            Подготовить один дополнительный разбор Architect
          </button>
        </div>}
      </section>}
      {job.working_copy && <WorkingCopyView job={job.job_id} copy={job.working_copy}
        blocked={blocked || busy || unknown} command={command} />}
      {job.team_report && <details open>
        <summary>Итоговый отчёт команды</summary>
        {job.team_report.local_outcome === "incomplete" && <p>Локальная попытка завершена без решения.</p>}
        <p>Функциональные тесты не проводились. Материал сохранён со статусом unreviewed.</p>
        <pre>{json(job.team_report)}</pre>
      </details>}
      {engineerNext && <button disabled={busy || blocked || cloudWaiting || unknown}
        onClick={() => command("advance", { job_id: job.job_id, phase: "implement" })}>
        Использовать этот план и подготовить запрос Engineer
      </button>}
      {next && (
        <button
          disabled={busy || blocked || unknown}
          onClick={() =>
            command("advance", { job_id: job.job_id, phase: next })
          }
        >
          Подготовить: {labels[next]}
        </button>
      )}
    </article>
  );
}

export function TeamView({
  data,
  busy,
  command,
  prepare,
  prepareEngineering,
  sourceJob,
}: {
  data: TeamState;
  busy: boolean;
  command: Command;
  prepare: (text: string, project: string) => void;
  prepareEngineering?: (phase: "plan" | "implement") => void;
  sourceJob?: string;
}) {
  const [text, setText] = useState("");
  const [project, setProject] = useState("studio-lab");
  const cloudWaiting = data.cloud?.status === "waiting_for_cloud_quota";
  return (
    <>
      <div className="page-heading">
        <h1>Задачи команды</h1>
        <p>
          Инженерная работа, проверяемые результаты и опыт для локального II
          PRO.
        </p>
      </div>
      <section className="panel team-compose" aria-label="Новая задача команды">
        {cloudWaiting && <p role="status" className="notice">
          Ожидаем восстановления лимита ChatGPT. Облачные запросы поставлены на паузу.
        </p>}
        {data.cloud?.source === "user_reported_chatgpt_reset" && data.cloud.provider_verified === false &&
          <p className="notice">Сброс лимита ChatGPT отмечен со слов пользователя. Доступный лимит SIWC ещё не подтверждён.</p>}
        {!!data.abandoned_unknowns?.length && <p className="notice">
          Пользователь признал неопределённость и возможный расход лимитов прежних запросов:
          {" "}{data.abandoned_unknowns.join(", ")}. Их unknown и история сохранены;
          повтор запрещён. Новые независимые задачи требуют отдельного подтверждения.
        </p>}
        {data.text && (
          <section aria-label="GPT Text">
            <h2>GPT Text · Architect</h2>
            <p>
              Using ChatGPT plan · план ChatGPT после отдельного входа. Без
              API-ключа и автоматического перехода на платный API.
            </p>
            <p>
              Модель: {data.text.model || "Не определена"}.{" "}
              {data.text.auth_ready
                ? "Авторизация и каталог сохранены; генерация требует подтверждения."
                : "Нужен отдельный вход Continue with ChatGPT для II PRO Studio. Генерация отключена."}
            </p>
            <p>{data.text.prompt}</p>
            <a
              href="https://chatgpt.com/settings/usage"
              target="_blank"
              rel="noreferrer"
            >
              Manage usage — лимиты и credits ChatGPT
            </a>
            <p>
              Проверьте, разрешён ли расход дополнительных credits. При unknown
              автоматического повтора нет.
            </p>
            <button
              disabled={busy || data.blocked || cloudWaiting || !data.text.auth_ready}
              onClick={() => prepare(data.text!.prompt, "gpt-text")}
            >
              Подготовить один запрос Architect
            </button>
          </section>
        )}
        {data.engineering?.supported && <section aria-label="Текстовые инженеры">
          <h2>GPT Architect и GPT Engineer</h2>
          <p>Модель {data.engineering.model || "не определена"}. У каждой роли свой контекст и история.</p>
          <p>{data.engineering.auth_ready ? "Авторизация готова. Каждый запрос требует отдельного подтверждения." : "Авторизация недоступна или устарела."}</p>
          <p>Учебный проект pricing.py. {data.engineering.task} {data.engineering.criteria}</p>
          {sourceJob && <p>Выбран пакет передачи из Local. Точный состав появится перед подтверждением.</p>}
          {!data.engineering.fixture_available && <p>Учебный снимок не зарегистрирован.</p>}
          <div className="action-row">
            <button disabled={busy || data.blocked || cloudWaiting || !data.engineering.auth_ready || !data.engineering.fixture_available}
              onClick={() => prepareEngineering?.("plan")}>Начать командную задачу · план Architect</button>
            <button disabled={busy || data.blocked || cloudWaiting || !data.engineering.auth_ready || !data.engineering.fixture_available}
              onClick={() => prepareEngineering?.("implement")}>Подготовить запрос Engineer</button>
          </div>
        </section>}
        <h2>Mock-лаборатория команды</h2>
        <p className="notice">
          {data.text?.auth_ready ? (
            "Проектные агенты с доступом к компьютеру пока не подключены. Ниже — фиксированная Mock-лаборатория."
          ) : (
            <>
              Сейчас работает Mock Adapter — фиксированный пример разработчика.
              Настоящие GPT Architect и GPT Engineer не подключены. Две роли
              могут использовать одну модель; это разные контексты, а не
              независимые технологии.
            </>
          )}
        </p>
        <label>
          Рабочий проект
          <select
            value={project}
            onChange={(e) => setProject(e.target.value)}
            disabled={busy}
          >
            {data.projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.title}
              </option>
            ))}
          </select>
        </label>
        <label>
          Задача обычным текстом
          <textarea
            maxLength={1800}
            rows={4}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Например: исправить slug_words в лабораторном проекте"
            disabled={busy}
          />
        </label>
        <p>
          Mock всегда демонстрирует контракт slug_words и не обещает решить
          произвольное задание. Подготовка сохраняет задачу; каждый этап требует
          отдельного подтверждения.
        </p>
        <button
          disabled={busy || data.blocked || !text.trim()}
          onClick={() => prepare(text, project)}
        >
          Подготовить задачу Mock-команды
        </button>
        <p>
          Codex App Server:{" "}
          {data.auth_status === "confirmed"
            ? "вход подтверждён последней диагностикой; активных GPT-сессий нет"
            : "авторизация ещё не подтверждена"}
          ; реальный запуск закрыт до отдельного согласования и проверки среды.
        </p>
      </section>
      {data.blocked && (
        <p role="alert" className="notice">
          Есть активное либо неопределённое выполнение. Новые запуски
          заблокированы; журнал доступен.
        </p>
      )}
      {data.jobs.length === 0 ? (
        <p>
          Задач команды пока нет. Результаты не подставляются автоматически.
        </p>
      ) : (
        data.jobs.map((job) => (
          <TeamJobView
            key={job.job_id}
            job={job}
            busy={busy}
            blocked={data.blocked}
            cloudWaiting={cloudWaiting}
            command={command}
          />
        ))
      )}
    </>
  );
}

export function TeamPanel({ sourceJob }: { sourceJob?: string } = {}) {
  const [data, setData] = useState<TeamState | null>(null),
    [error, setError] = useState<string | null>(null),
    [busy, setBusy] = useState(false);
  const gate = useRef(false);
  function headers() {
    return {
      Authorization:
        "Bearer " + (sessionStorage.getItem("ii-pro-session") || ""),
    };
  }
  async function load() {
    const response = await fetch("/api/team/state", {
      headers: headers(),
      credentials: "omit",
      cache: "no-store",
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw new Error("Журнал команды недоступен");
    const value = (await response.json()) as TeamState;
    if (!value.projects) throw new Error("GPT Team отключена сервером");
    setData(value);
  }
  useEffect(() => {
    let disposed = false;
    const poll = async () => {
      try {
        if (!gate.current) {
          await load();
          if (!disposed) setError(null);
        }
      } catch (e) {
        const fault = e as Error;
        if (!disposed && !gate.current) setError(fault.name === "TimeoutError"
          ? "Studio не ответила вовремя. Повторяем чтение журнала." : fault.message);
      } finally {
        if (!disposed) timer = setTimeout(poll, 3000);
      }
    };
    let timer: ReturnType<typeof setTimeout>;
    void poll();
    return () => {
      disposed = true;
      clearTimeout(timer);
    };
  }, []);
  async function command(path: string, body: object) {
    if (gate.current) return;
    gate.current = true;
    setBusy(true);
    setError(null);
    try {
      const session = await fetch("/api/session", {
        headers: headers(),
        credentials: "omit",
        cache: "no-store",
        signal: AbortSignal.timeout(10000),
      });
      if (!session.ok) throw new Error("Требуется текущая локальная сессия");
      const { csrf } = (await session.json()) as { csrf: string };
      const endpoint = path === "local-engine/decision" ? "/api/local-engine/decision" : "/api/team/" + path;
      const response = await fetch(endpoint, {
        method: "POST",
        headers: {
          ...headers(),
          "Content-Type": "application/json",
          "X-CSRF-Token": csrf,
        },
        body: JSON.stringify(body),
        credentials: "omit",
        cache: "no-store",
        signal: AbortSignal.timeout(15000),
      });
      const value = (await response.json()) as { error?: string };
      if (!response.ok) throw new Error(value.error || "Команда отклонена");
      await load();
    } catch (e) {
      setError((e as Error).message + " Автоматического повтора нет.");
    } finally {
      gate.current = false;
      setBusy(false);
    }
  }
  function prepare(text: string, project: string) {
    const scope = JSON.stringify([
      project,
      text,
      project === "gpt-text" ? data?.text?.authorization_ref : null,
    ]);
    const old = sessionStorage.getItem("ii-pro-team-draft");
    const saved = old
      ? (JSON.parse(old) as { scope: string; id: string })
      : null;
    const existing = data?.jobs.find(
      (j) =>
        j.project_id === project &&
        j.text === text &&
        (project !== "gpt-text" ||
          j.text_profile?.authorization_ref === data?.text?.authorization_ref),
    );
    const id =
      existing?.job_id ||
      (saved?.scope === scope
        ? saved.id
        : crypto.randomUUID().replaceAll("-", ""));
    sessionStorage.setItem("ii-pro-team-draft", JSON.stringify({ scope, id }));
    void command("prepare", { request_id: id, project_id: project, text });
  }
  function prepareEngineering(phase: "plan" | "implement") {
    const existing = data?.jobs.find(job => job.engineering && job.phases.some(item =>
      item.phase === phase && item.text_operation?.context.source_job === (sourceJob ?? null) &&
      item.text_operation?.context.architect_plan === null));
    const key = JSON.stringify([sourceJob ?? null, phase]);
    const saved = sessionStorage.getItem("ii-pro-engineering-" + key);
    const id = existing?.job_id || saved || crypto.randomUUID().replaceAll("-", "");
    sessionStorage.setItem("ii-pro-engineering-" + key, id);
    void command("prepare", { project_id: "pricing", request_id: id, phase, source_job: sourceJob ?? null });
  }
  return (
    <>
      {error && (
        <p role="alert" className="notice">
          {error}
        </p>
      )}
      {data?.project_work && <ProjectWork data={data.project_work} busy={busy} command={(p, b) => void command(p, b)} />}
      {data ? (
        <TeamView
          data={data}
          busy={busy}
          command={(p, b) => void command(p, b)}
          prepare={prepare}
          prepareEngineering={prepareEngineering}
          sourceJob={sourceJob}
        />
      ) : (
        <p role="status">{error || "Загрузка журнала GPT Team…"}</p>
      )}
    </>
  );
}
