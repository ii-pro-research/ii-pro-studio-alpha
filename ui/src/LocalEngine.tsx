import { useEffect, useRef, useState } from "react";
import { TeamPanel } from "./Team.js";

type Json = Record<string, unknown>;
type Selection = { project: string; job: string };
export type Project = { id: string; title: string };
type Approval = { id: string; run: string; digest: string; status: string; expires_at: string; operation: Json };
type Observation = { tool: string | null; status: string; plan?: string; result: Json };
type Recovery = { target: string; original_status: string; confirmed_stop: boolean; error_code: string | null;
  eligible: boolean; closed: boolean; evidence: Json | null; reason?: string;
  fence?: { status: string; epoch_run?: string; evidence?: Json; approval?: Pick<Approval, "id" | "run" | "digest" | "expires_at"> };
  approval: Pick<Approval, "id" | "run" | "digest" | "expires_at"> | null };
type Memory = {
  id: string;
  body: { title: string; author: string; observation: string; limitations: string[]; evidence: Json; source: Json; revision: string };
  history: { status: string; reviewer: string; reason: string; created_at: string }[];
};
type Unreviewed = { id: string; run: string; sha256: string; author: string; limitation: string; material: Json };
type Skill = { id: string; title: string; version: number; status: string; sha256: string; applies_to: string;
  inputs: string[]; steps: string[]; tools: string[]; criterion: string; limitation: string;
  evidence: [string, string][]; history: string[]; trust_history: Json[] };
type SkillArm = { job: string; case: string; arm: string; status: string; pass: boolean | null;
  run: string | null; seconds: number | null; error: string | null; tokens: Record<string, number> | null };
export type EngineState = {
  projects: Project[]; jobs: (Selection & { task: string })[]; live: boolean; max_steps: number;
  roles: { local: string; architect: string; engineer: string }; memory: Memory[];
  unreviewed_materials?: Unreviewed[];
  skills?: Skill[]; skill_uses?: Json[]; skill_pilot?: SkillArm[];
};
export type Job = Selection & {
  task: string; requirements: string; status: string; can_approve: boolean;
  scope_changed: boolean; dispatch_pending: boolean; approval: Approval | null;
  results: Record<string, Observation>;
  runs: { id: string; status: string; error_code: string | null; proposal: string | null; memory: Json[]; approvals: Json[]; artifacts: Json[];
    statistics?: Record<string, number>; elapsed_seconds?: number | null; tokens_per_second?: number | null; recovery?: Recovery }[];
  handoff?: Json;
};

const statuses: Record<string, string> = {
  awaiting_approval: "Ожидает разрешения", ready_for_next_step: "Можно подготовить следующий шаг",
  running: "Выполняется", stopping_after_current_step: "Остановка после текущего шага",
  stopped: "Дальнейшие шаги остановлены", finished: "Отчёт готов",
  budget_exhausted: "Лимит шагов исчерпан", unknown: "Результат неизвестен. Повтор заблокирован.",
  waiting_for_engineer: "Ожидает GPT Engineer", succeeded: "Ответ сохранён",
  failed: "Ошибка", created: "Подготовлен", pending: "Ожидает решения", approved: "Разрешено",
  consumed: "Использовано", rejected: "Отклонено", revoked: "Отозвано", expired: "Срок истёк",
  invalidated: "Недействительно", stale: "Требуется проверка в новой сессии",
  tested: "Проверено тестами", imported: "Импортировано", verified: "Подтверждено", cancelled: "Отменено",
  unreviewed: "Ожидает проверки", planned: "Запланирован",
};
const tools: Record<string, string> = {
  list_files: "Список файлов", read_file: "Чтение файла", search_code: "Поиск по коду",
  git_status: "Состояние снимка Git", git_diff: "Сравнение снимков Git", analyze_python: "Анализ синтаксиса",
  propose_patch: "Предложение правки", finish: "Итоговый отчёт",
};
const reasons: Record<string, string> = {
  invalid_proposal: "Ответ не прошёл проверку формата", repeated_action: "Повтор инструмента заблокирован",
  operator_stop: "Действие отменено остановкой задачи", outside_tool_scope: "Действие выходит за разрешённую область",
  tool_budget_exhausted: "Лимит инструментов исчерпан", anchor_not_unique: "Исходный фрагмент не найден или встречается несколько раз",
};
const label = (status: string) => statuses[status] || status;
const pretty = (value: unknown) => JSON.stringify(value, null, 2);
type Command = (path: string, body: object) => void;

export function JobView({ job, busy, command }: { job: Job; busy: boolean; command: Command }) {
  const selected = { project: job.project, job: job.job };
  const approval = job.approval;
  const pending = job.status === "awaiting_approval" && !job.dispatch_pending && !job.scope_changed;
  const refreshable = pending && (!approval || !["pending", "approved"].includes(approval.status));
  const next = !job.scope_changed && (job.status === "ready_for_next_step" || refreshable);
  const canStop = ["awaiting_approval", "ready_for_next_step", "running"].includes(job.status);
  function decide(allow: boolean) {
    if (approval) command("decision", { ...selected, run: approval.run, approval: approval.id, digest: approval.digest, allow });
  }
  return <section className="panel team-job" aria-label="Текущая задача">
    <h2>{job.task}</h2>
    <p>{job.requirements}</p>
    <p role="status">{job.dispatch_pending ? "Шаг передан на запуск" : label(job.status)} · Шагов: {job.runs.length}</p>
    {job.scope_changed && <p role="alert">Снимок проекта изменился. История доступна, для работы с новым снимком создайте задачу.</p>}
    {approval && <details open={pending}>
      <summary>Разрешение на шаг · {label(approval.status)}</summary>
      <p>Разрешение охватывает одну генерацию и проверку одного предложенного действия на снимке проекта.</p>
      <p>Действует до {new Date(approval.expires_at).toLocaleString("ru-RU")}</p>
      <details><summary>Точный запрос, схема и параметры</summary><pre>{pretty(approval.operation)}</pre></details>
      {pending && approval.status === "pending" && <>
        <button type="button" disabled={busy || !job.can_approve} onClick={() => decide(true)}>Разрешить один шаг</button>{" "}
        <button type="button" disabled={busy} onClick={() => decide(false)}>Отклонить</button>
      </>}
    </details>}
    {next && <button type="button" disabled={busy} onClick={() => command("prepare", { ...selected, task: job.task, requirements: job.requirements })}>
      {refreshable ? "Подготовить новое разрешение" : "Подготовить следующий шаг"}
    </button>}{" "}
    {canStop && <button type="button" disabled={busy} onClick={() => command("stop", selected)}>Остановить дальнейшие шаги</button>}
    {job.runs.map((run, i) => {
      const obs = job.results[String(i + 1)];
      const facts = obs?.result;
      const recovery = run.recovery;
      return <details className="team-phase" key={run.id}>
        <summary>Шаг {i + 1} · {obs?.tool ? tools[obs.tool] || obs.tool : label(run.status)}{obs?.status === "rejected" ? " · Отклонён" : ""}</summary>
        {run.error_code && <p role="alert">Ошибка шага: {run.error_code}</p>}
        {obs?.plan && <p>План: {obs.plan}</p>}
        {obs?.status === "rejected" && <p role="alert">{reasons[String(facts.reason)] || "Запрос инструмента отклонён"}</p>}
        {facts?.reason === "anchor_not_unique" && <p>{reasons.anchor_not_unique}</p>}
        {typeof facts?.model_report === "string" && <p>Вывод модели: {facts.model_report}</p>}
        {facts?.syntax !== undefined && <p>Синтаксис: {facts.syntax === "valid" ? "корректен" : "ошибка"}</p>}
        {facts?.functional_tests === "not_run" && <p>Функциональные тесты не выполнялись.</p>}
        {typeof facts?.diff === "string" && <details><summary>{obs.tool === "propose_patch" ? "Предложенный diff" : "Различия снимков"}</summary><pre>{facts.diff || "Изменений нет"}</pre></details>}
        {obs && <details><summary>Фактический результат инструмента</summary><pre>{pretty(facts)}</pre></details>}
        {run.proposal && <details><summary>Запрос инструмента от модели</summary><pre>{run.proposal}</pre></details>}
        {run.statistics && <details><summary>Статистика генерации</summary><pre>{JSON.stringify({
          ...run.statistics, elapsed_seconds: run.elapsed_seconds, tokens_per_second: run.tokens_per_second,
        }, null, 2)}</pre></details>}
        {recovery && <section aria-label="Сверка Local Run">
          <h3>{recovery.closed ? "Административно закрыт" : "Сверка неизвестного шага"}</h3>
          <p>Run <code>{recovery.target}</code>. Исходный статус {recovery.original_status}. Результат модели неизвестен. Повтор запрещён.</p>
          {recovery.evidence
            ? <p>Конец ответа Ollama подтверждён запечатанной диагностикой. Действие инструмента не запускалось. Закрытие снимет блокировку для независимых задач.</p>
            : <p role="alert">В Core нет достаточных доказательств завершения именно этого запроса. Блокировка сохранена.</p>}
          {recovery.reason === "active_execution" && <p role="alert">Сверка недоступна, пока выполняется другая операция.</p>}
          <details><summary>Доказательства сверки</summary><pre>{pretty(recovery.evidence ?? { run: run.id, error: recovery.error_code, artifacts: run.artifacts })}</pre></details>
          {recovery.eligible && !recovery.closed && (recovery.approval ? <>
            <p>Отдельное разрешение действует до {new Date(recovery.approval.expires_at).toLocaleString("ru-RU")}.</p>
            <button disabled={busy} onClick={() => recovery.approval && command("recovery-decision", {
              ...selected, target: recovery.target, run: recovery.approval.run, approval: recovery.approval.id, digest: recovery.approval.digest,
            })}>Подтвердить административное закрытие</button>
          </> : <button disabled={busy} onClick={() => command("recovery", {
            ...selected, target: recovery.target,
          })}>Подготовить разрешение на сверку</button>)}
          {recovery.fence && <section aria-label="Исполнительная среда Local">
            <h4>Исполнительная среда Local</h4>
            {recovery.fence.status === "needs_witness" && <button disabled={busy} onClick={() => command("recovery", {
              ...selected, target: recovery.target, mode: "witness",
            })}>Зафиксировать текущее поколение Windows</button>}
            {recovery.fence.status === "restart_unproven" && <p>Исходное поколение зафиксировано. До подтверждённого полного перезапуска Windows допуск закрыт.</p>}
            {recovery.fence.status === "fenced" && <p>Старая среда изолирована. Исторический результат неизвестен. Поколение <code>{recovery.fence.epoch_run}</code>.</p>}
            {recovery.fence.status === "ready" && <>
              <p>Подтверждена новая загрузка Windows и новый владелец Core. Отдельное согласование откроет допуск для независимых задач. Старый Run останется unknown.</p>
              {recovery.fence.evidence && <details><summary>Доказательства смены поколения</summary><pre>{pretty(recovery.fence.evidence)}</pre></details>}
              {recovery.fence.approval
                ? <button disabled={busy} onClick={() => {
                  const permit = recovery.fence?.approval;
                  if (permit) command("recovery-decision", { ...selected, target: recovery.target, mode: "fence",
                    run: permit.run, approval: permit.id, digest: permit.digest });
                }}>Подтвердить новое поколение Local</button>
                : <button disabled={busy} onClick={() => command("recovery", {
                  ...selected, target: recovery.target, mode: "fence",
                })}>Подготовить разрешение на новое поколение</button>}
            </>}
          </section>}
        </section>}
        <details><summary>Записи памяти в запросе ({run.memory.length})</summary>{run.memory.length ? <pre>{pretty(run.memory)}</pre> : <p>Записей нет.</p>}</details>
        <details><summary>История разрешений и доказательства</summary><pre>{pretty({ run: run.id, approvals: run.approvals, artifacts: run.artifacts })}</pre></details>
      </details>;
    })}
    {job.handoff && <details open><summary>Пакет передачи GPT Engineer</summary><pre>{pretty(job.handoff)}</pre></details>}
  </section>;
}

export function MemoryView({ entries, unreviewed = [] }: { entries: Memory[]; unreviewed?: Unreviewed[] }) {
  return <section className="panel" aria-label="Инженерная память">
    <h2>Инженерная память</h2>
    {!entries.length && <p>Сохранённых знаний пока нет.</p>}
    {entries.map(entry => {
      const status = entry.history.at(-1)?.status || "imported";
      return <details key={entry.id}>
        <summary>{entry.body.title} · {label(status)}</summary>
        <p>Автор: {entry.body.author}</p><p>{entry.body.observation}</p>
        {status !== "verified" && <p>Запись исключена из доверенного контекста.</p>}
        <ul>{entry.body.limitations.map(item => <li key={item}>{item}</li>)}</ul>
        <details><summary>Источник и доказательства</summary><pre>{pretty({ revision: entry.body.revision, source: entry.body.source, evidence: entry.body.evidence })}</pre></details>
        <details><summary>История статусов</summary>
          <ol>{entry.history.map((item, i) => <li key={i}>{label(item.status)} · {item.reviewer} · {item.reason}</li>)}</ol>
        </details>
      </details>;
    })}
    {unreviewed.length > 0 && <div aria-label="Непроверенные материалы">
      <h3>Непроверенные материалы</h3>
      <p>Эти материалы доступны для просмотра и исключены из доверенного контекста.</p>
      {unreviewed.map(entry => <details key={entry.id}>
        <summary>{entry.author} · Ожидает проверки</summary>
        <p>{entry.limitation}</p>
        <pre>{pretty({ run: entry.run, sha256: entry.sha256, material: entry.material })}</pre>
      </details>)}
    </div>}
  </section>;
}

export function SkillView({ skills = [], uses = [], pilot = [], prepare, busy = false }: {
  skills?: Skill[]; uses?: Json[]; pilot?: SkillArm[]; prepare?: () => void; busy?: boolean;
}) {
  if (!skills.length) return null;
  const completed = pilot.filter(slot => slot.pass !== null);
  const blocked = pilot.some(slot => ["unknown", "failed", "cancelled", "running"].includes(slot.status));
  return <section className="panel" aria-label="Инженерные навыки">
    <h2>Инженерные навыки</h2>
    {skills.map(skill => <details key={skill.id + skill.version}>
      <summary>{skill.title} · v{skill.version} · {label(skill.status)}</summary>
      <p>{skill.applies_to}. {skill.criterion}</p>
      <p>Входные данные · {skill.inputs.join(", ")}</p>
      <ol>{skill.steps.map(step => <li key={step}>{step}</li>)}</ol>
      <p>Инструменты · {skill.tools.map(tool => tools[tool] || tool).join(", ")}</p>
      <p>{skill.limitation}</p>
      <details><summary>История и доказательства</summary>
        <pre>{pretty({ sha256: skill.sha256, evidence: skill.evidence, versions: skill.history, trust: skill.trust_history })}</pre>
      </details>
    </details>)}
    <details><summary>История применения ({uses.length})</summary>
      {uses.length ? <pre>{pretty(uses)}</pre> : <p>Применений пока нет.</p>}
    </details>
    {pilot.length > 0 && <><h3>Сравнение навыка A/B</h3>
      {!completed.length && <p>Фактических результатов пока нет.</p>}
      {!!completed.length && <p>{["a", "b"].map(arm => {
        const measured = completed.filter(slot => slot.arm === arm);
        return measured.length ? arm.toUpperCase() + " · " + measured.filter(slot => slot.pass).length + "/" + measured.length
          : arm.toUpperCase() + " · нет результатов";
      }).join(" · ")}</p>}
      <ul>{pilot.map(slot => <li key={slot.job}>{slot.case} · {slot.arm.toUpperCase()} · {label(slot.status)}
        {slot.pass !== null && (slot.pass ? " · прогноз совпал" : " · прогноз не принят")}
        {slot.seconds !== null && " · " + slot.seconds.toFixed(1) + " с"}
        {slot.tokens?.eval_count !== undefined && " · " + slot.tokens.eval_count + " выходных токенов"}
        {slot.error && <small> · {slot.error}</small>}
      </li>)}</ul>
      {prepare && completed.length < pilot.length && <button type="button" disabled={busy || blocked}
        onClick={prepare}>Подготовить следующий слот A/B</button>}
    </>}
  </section>;
}

export function WorkspaceModes({ mode, select }: { mode: string; select: (mode: "Local" | "GPT" | "Team") => void }) {
  return <nav className="panel" aria-label="Режим работы">
    {(["Local", "GPT", "Team"] as const).map(name =>
      <button key={name} type="button" aria-pressed={mode === name} onClick={() => select(name)}>{name}</button>)}
  </nav>;
}

async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    credentials: "omit", cache: "no-store", signal: AbortSignal.timeout(10000), ...init,
    headers: { Authorization: "Bearer " + (sessionStorage.getItem("ii-pro-session") || ""), ...init?.headers },
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Не удалось загрузить данные Studio");
  return data as T;
}

export function EngineeringWorkspace() {
  const [mode, setMode] = useState<"Local" | "GPT" | "Team">("Local");
  const [handoffJob, setHandoffJob] = useState<string>();
  const [state, setState] = useState<EngineState | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [project, setProject] = useState("");
  const [task, setTask] = useState("");
  const [requirements, setRequirements] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const gate = useRef(false);
  const active = useRef<Selection | null>(null);
  const draft = useRef<{ key: string; id: string } | null>(null);
  const readEpoch = useRef(0);

  async function refresh() {
    const generation = ++readEpoch.current;
    const selected = active.current;
    const data = await api<EngineState>("/api/local-engine/state");
    const detail = selected ? await api<Job>(`/api/local-engine/jobs/${selected.project}/${selected.job}`) : null;
    if (generation === readEpoch.current) {
      setState(data);
      setError("");
      if (selected === active.current) setJob(detail);
    }
  }
  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        if (!gate.current) await refresh();
      } catch (e) {
        if (!disposed && !gate.current) {
          const fault = e as Error;
          setError(fault.name === "TimeoutError" ? "Studio не ответила вовремя. Повторяем чтение истории." : fault.message);
        }
      } finally {
        if (!disposed) timer = setTimeout(poll, 3000);
      }
    }
    void poll();
    return () => { disposed = true; clearTimeout(timer); readEpoch.current++; };
  }, []);

  async function command(path: string, body: object) {
    if (gate.current) return;
    gate.current = true;
    readEpoch.current++;
    setBusy(true);
    setError("");
    try {
      const { csrf } = await api<{ csrf: string }>("/api/session");
      const detail = await api<Job>("/api/local-engine/" + path, {
        method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      });
      active.current = { project: detail.project, job: detail.job };
      setJob(detail);
      await refresh();
    } catch (e) { setError((e as Error).message); }
    finally { gate.current = false; setBusy(false); }
  }

  const selectedProject = project || state?.projects[0]?.id || "";
  function prepare() {
    const key = pretty([selectedProject, task, requirements]);
    const saved = draft.current;
    const id = saved?.key === key ? saved.id : crypto.randomUUID().replaceAll("-", "");
    draft.current = { key, id };
    void command("prepare", { project: selectedProject, job: id, task, requirements });
  }
  return <>
    <WorkspaceModes mode={mode} select={next => { setHandoffJob(undefined); setMode(next); }} />
    {mode !== "Local" && <>
      {mode === "Team" && job?.handoff && job.project === "pricing" && <section className="panel">
        <button type="button" onClick={() => setHandoffJob(job.job)}>Выбрать пакет передачи Local</button>
      </section>}
      <TeamPanel key={mode} sourceJob={handoffJob} />
      {mode === "Team" && state && <>
        <SkillView skills={state.skills} uses={state.skill_uses} pilot={state.skill_pilot} />
        <MemoryView entries={state.memory} unreviewed={state.unreviewed_materials} />
      </>}
    </>}
    {mode === "Local" && <>
      {error && <p role="alert" className="notice">{error}</p>}
      <section className="panel"><h1>Рабочее пространство инженеров</h1>
        <ul>
          <li>Локальный II PRO · {state ? (state.roles.local === "enabled" ? "Разрешён запуск с подтверждением" : "Просмотр и подготовка") : "Состояние загружается"}</li>
          <li>GPT Architect · {state?.roles.architect === "text_ready" ? "Доступен текстовый режим с подтверждением" : "Текстовый режим не готов"}</li>
          <li>GPT Engineer · {state?.roles.engineer === "text_ready" ? "Доступен текстовый режим с подтверждением" : "Текстовый режим не готов"}</li>
        </ul>
      </section>

        <section className="panel"><h2>Новая задача</h2>
          <form className="team-compose" onSubmit={e => { e.preventDefault(); prepare(); }}>
            <label>Исполнитель<select defaultValue="local"><option value="local">Локальный II PRO</option></select></label>
            <label>Проект<select value={selectedProject} onChange={e => setProject(e.target.value)} required>
              {!state?.projects.length && <option value="">Нет зарегистрированных проектов</option>}
              {state?.projects.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}
            </select></label>
            <label>Задача<textarea value={task} onChange={e => setTask(e.target.value)} maxLength={240} required /></label>
            <label>Критерии проверки<textarea value={requirements} onChange={e => setRequirements(e.target.value)} maxLength={160} required /></label>
            <button disabled={busy || !selectedProject || !task.trim() || !requirements.trim()}>Подготовить шаг</button>
          </form>
        </section>
        <section className="panel"><h2>История задач</h2>
          {!state?.jobs.length && <p>{state ? "Задач пока нет." : "Загрузка истории…"}</p>}
          <ul>{state?.jobs.map(item => <li key={item.project + item.job}>
            <button type="button" disabled={busy} onClick={() => {
              active.current = { project: item.project, job: item.job }; setJob(null);
              setError(""); void refresh().catch(e => setError((e as Error).message));
            }}>{item.task}</button>
          </li>)}</ul>
        </section>
        {job && <JobView job={job} busy={busy} command={(path, body) => void command(path, body)} />}
        {state && <>
          <SkillView skills={state.skills} uses={state.skill_uses} pilot={state.skill_pilot} busy={busy}
            prepare={() => void command("skill-pilot", {})} />
          <MemoryView entries={state.memory} unreviewed={state.unreviewed_materials} />
        </>}
    </>}
  </>;
}
