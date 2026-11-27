import { useState } from "react";

type Consent = { id: string; status: string; spec_digest: string; expires_at: string };
type Phase = { id: string; status: string; error?: string; approval?: Consent; result?: { state?: string }; diff?: string };
type LocalStep = { id: string; run: string; digest: string; status: string };
type Scope = { project: string; files: string[]; task: string; acceptance: string; models: Record<string, string>;
  cloud_calls: number; local_steps: number; duration_seconds: number; attempts_per_phase: number; operations: string[];
  local_review?: string; resume_from?: string; edit_protocol?: string; restart_of?: string; engineer_deadline_seconds?: number;
  autonomous?: { budget: Record<string, number>; tools: string[] } };
type Session = { job: string; title: string; scope: Scope; sha256: string; consent?: { expires_at?: string }; expired: boolean;
  stopped: boolean; blocked: boolean; phases: Record<string, Phase>; functional_blocker: string;
  plan_recovery_available?: boolean; renewal_available?: boolean; filter_continuation_available?: boolean;
  learning_status?: string; local?: { job: string; project: string; status: string; approval?: LocalStep; results: object };
  autonomy?: {status: string; current?: string; remaining: Record<string, number>; journal: unknown[]}};
export type ProjectWorkState = { projects: { id: string; title: string; files: string[] }[]; sessions: Session[] };

const labels: Record<string, string> = {
  plan: "GPT Architect · План", plan_recovery: "GPT Architect · Новый план", implement: "GPT Engineer · Правки",
  apply: "Изолированная копия", review: "GPT Architect · Разбор",
  test: "Docker Worker · Приёмка", repair: "GPT Engineer · Исправление", apply_fix: "Изолированная копия · Исправление",
  test_fix: "Docker Worker · Проверка исправления",
  rollback: "Engineering Executor · Откат", rollback_fix: "Engineering Executor · Откат исправления",
  local: "II PRO Local · Наблюдение",
};
const states: Record<string, string> = {
  created: "Ожидает решения", running: "Выполняется", succeeded: "Завершено", failed: "Ошибка", unknown: "Результат не подтверждён",
  cancelled: "Отменено", awaiting_approval: "Ожидает решения", ready: "Наблюдение сохранено", finished: "Наблюдение сохранено",
  draft: "Готовится согласование", active: "Разрешение действует", paused: "Приостановлено", revoked: "Разрешение отозвано",
  stopped: "Остановлено", expired: "Срок истёк", completed: "Сеанс завершён",
  budget: "Бюджет исправлений исчерпан, копия восстановлена", constraint: "Операция заблокирована ограничениями",
};
const budgets: Record<string, string> = {cloud: "облачные запросы", local: "шаги Local", copies: "применения",
  worker: "проверки Worker", rollbacks: "откаты"};

export function ProjectWork({ data, busy, command }: { data: ProjectWorkState; busy: boolean; command: (path: string, value: object) => void }) {
  const [projectId, setProject] = useState("");
  const [task, setTask] = useState("");
  const [criteria, setCriteria] = useState("");
  const [selected, setSelected] = useState<string[] | null>(null);
  const [job, setJob] = useState("");
  const [automatic, setAutomatic] = useState(false);
  const [minutes, setMinutes] = useState(30);
  const [corrections, setCorrections] = useState(1);
  const project = data.projects.find(p => p.id === projectId) || data.projects[0];
  const files = selected ?? project?.files ?? [];
  const session = data.sessions.find(s => s.job === job) || data.sessions[0];
  const progress = session && Boolean(session.consent) && !session.expired && !session.stopped;
  const local = session?.local;
  const auto = session?.autonomy;
  const hasAuto = project?.id === "task-manager" || project?.id === "task-manager-filter";
  const waitingLocal = local?.status === "awaiting_approval" && local.approval?.status === "pending";
  const waitingPhase = Object.entries(session?.phases || {}).find(([, phase]) =>
    phase.status === "created" && phase.approval?.status === "pending");
  return <section className="panel" aria-label="Инженерный сеанс">
    <h2>Инженерный сеанс</h2>
    <p>Выберите зарегистрированный проект и файлы, которые смогут прочитать инженеры.</p>
    <form onSubmit={e => {
      e.preventDefault();
      if (!project || !files.length) return;
      const id = crypto.randomUUID().replaceAll("-", "");
      setJob(id);
      command("project-draft", { job: id, project: project.id, task, acceptance: criteria, files,
        ...(automatic && hasAuto ? {autonomous: {duration_seconds: minutes * 60, corrections}} : {}) });
    }}>
      <label>Проект <select value={project?.id || ""} onChange={e => { setProject(e.target.value); setSelected(null); }}>
        {data.projects.map(p => <option key={p.id} value={p.id}>{p.title}</option>)}
      </select></label>
      <label>Задача<textarea required maxLength={600} value={task} onChange={e => setTask(e.target.value)} /></label>
      <label>Критерии приёмки<textarea required maxLength={800} value={criteria} onChange={e => setCriteria(e.target.value)} /></label>
      <fieldset><legend>Файлы в области сеанса</legend>
        {project?.files.map(file => <label key={file}><input type="checkbox" checked={files.includes(file)}
          onChange={e => setSelected(e.target.checked ? [...files, file] : files.filter(p => p !== file))} /> {file}</label>)}
      </fieldset>
      {hasAuto && <fieldset><legend>Режим</legend>
        <label><input type="checkbox" checked={automatic} onChange={e => setAutomatic(e.target.checked)} /> Автономный Balanced</label>
        {automatic && <>
          <label>Минуты работы <input type="number" min={1} max={30} required value={minutes}
            onChange={e => setMinutes(Number(e.target.value))} /></label>
          <label>Попытки исправления <select value={corrections} onChange={e => setCorrections(Number(e.target.value))}>
            <option value={0}>0</option><option value={1}>1</option></select></label>
        </>}
      </fieldset>}
      <button disabled={busy || !project || !files.length}>Подготовить сеанс</button>
    </form>
    {session && <>
      <label>История сеансов <select value={session.job} onChange={e => setJob(e.target.value)}>
        {data.sessions.map(s => <option key={s.job} value={s.job}>{s.title} · {s.scope.task.slice(0, 60)}</option>)}
      </select></label>
      <h3>{session.title}</h3><p>{session.scope.task}</p>
      <details open={auto && !session.consent ? true : undefined}><summary>Область, файлы, модели и бюджет</summary><pre>{JSON.stringify(session.scope, null, 2)}</pre></details>
      <p>До {session.scope.cloud_calls} облачных запросов. Локальных шагов: {session.scope.local_steps}. Сеанс длится {session.scope.duration_seconds / 60} минут.
        {auto ? " Core выводит отдельные разрешения из согласованной области." : " У каждой операции своё согласование."}</p>
      {auto && <>
        <p role="status">{states[auto.status] || auto.status}{auto.current && " · " + labels[auto.current]}</p>
        <ul aria-label="Оставшийся бюджет">{Object.entries(auto.remaining).map(([key, value]) =>
          <li key={key}>{budgets[key] || key} · {value}</li>)}</ul>
        <details><summary>Журнал производных Approval и управления</summary><pre>{JSON.stringify(auto.journal, null, 2)}</pre></details>
        {progress && ["active", "paused"].includes(auto.status) && <>
          <button disabled={busy} onClick={() => command("project-control", {job: session.job,
            control: auto.status === "paused" ? "resume" : "pause"})}>{auto.status === "paused" ? "Продолжить" : "Приостановить"}</button>
          <button disabled={busy} onClick={() => command("project-control", {job: session.job, control: "stop"})}>Остановить сеанс</button>
          <button disabled={busy} onClick={() => command("project-control", {job: session.job, control: "revoke"})}>Отозвать разрешение</button>
          <p>Пауза и отзыв блокируют новые операции. Запрос, уже переданный исполнителю, может завершиться.</p>
        </>}
      </>}
      {session.scope.engineer_deadline_seconds && <p>Дедлайн одного запроса Engineer — {session.scope.engineer_deadline_seconds} с.</p>}
      {progress && <p>{session.consent?.expires_at && "Сеанс активен до " + new Date(session.consent.expires_at).toLocaleTimeString("ru-RU") + "."}
        {waitingLocal ? " Следующее решение · локальное наблюдение."
          : waitingPhase ? " Следующее решение · " + labels[waitingPhase[0]] + "." : ""}</p>}
      {session.scope.resume_from
        ? <p>Продолжение с точечными правками. План и наблюдение Local взяты из предыдущего сеанса.</p>
        : session.scope.local_review === "filter" && <p>Local сначала изучает исходный маршрут. Engineer получит результат этой попытки.</p>}
      {(session.scope.restart_of || (session.filter_continuation_available && session.scope.resume_from)) &&
        <p>Отдельный сеанс после unknown. Исходный результат сохранён. Новый запрос может дополнительно израсходовать лимит.</p>}
      {session.filter_continuation_available &&
        <button disabled={busy || session.blocked} onClick={() => {
          const id = crypto.randomUUID().replaceAll("-", "");
          setJob(id);
          command("project-draft", {job: id, resume_from: session.job});
        }}>Подготовить продолжение с точечными правками</button>}
      <p>{session.functional_blocker}</p>
      {session.expired && <p role="status">Время сеанса истекло.</p>}
      {session.stopped && <p role="status">Дальнейшие операции сеанса остановлены.</p>}
      {(session.plan_recovery_available || session.phases.plan_recovery) &&
        <p>Новый план использует облачный запрос из резерва на исправление Engineer. Общий лимит сохраняется.</p>}
      {session.plan_recovery_available &&
        <button disabled={busy} onClick={() => command("project-recover", {job: session.job, sha256: session.sha256,
          previous_run: session.phases.plan.id})}>Подготовить новый запрос Architect</button>}
      {session.renewal_available &&
        <button disabled={busy} onClick={() => command("project-start", {job: session.job, sha256: session.sha256})}>
          Согласовать возобновление сеанса с прежним бюджетом</button>}
      {!session.consent && !session.stopped &&
        <button disabled={busy || session.blocked} onClick={() => command("project-start", {job: session.job, sha256: session.sha256})}>
          {auto ? "Согласовать и запустить автономный сеанс" : "Согласовать область сеанса"}</button>}
      {progress && !auto && <>
        <button disabled={busy || session.blocked} onClick={() => command("project-progress", {job: session.job})}>Подготовить следующий шаг</button>
        <button disabled={busy} onClick={() => command("project-stop", {job: session.job})}>Остановить сеанс</button>
      </>}
      {Object.entries(session.phases).map(([key, phase]) => <article key={key}>
        <h4>{labels[key] || key}</h4>
        <p>{phase.status === "succeeded" && phase.result?.state === "rejected"
          ? "Ответ получен · результат отклонён проверкой" : states[phase.status] || phase.status}
          {phase.error ? " · " + phase.error : ""}</p>
        {phase.result && <details><summary>Результат</summary><pre>{JSON.stringify(phase.result, null, 2)}</pre></details>}
        {phase.diff && <details><summary>Изменения файлов</summary><pre>{phase.diff}</pre></details>}
        {phase.approval && <details><summary>Параметры согласования</summary><pre>{JSON.stringify({run: phase.id, ...phase.approval}, null, 2)}</pre></details>}
        {progress && !auto && phase.status === "created" && phase.approval?.status === "pending" &&
          <button disabled={busy || session.blocked} onClick={() => command("decision", {run_id: phase.id, approval_id: phase.approval!.id,
            spec_digest: phase.approval!.spec_digest, allow: true})}>Подтвердить {key.startsWith("apply") ? "применение в копии"
              : key.startsWith("test") ? "функциональную проверку" : "текстовый запрос"}</button>}
      </article>)}
      {local && <article><h4>II PRO Local · Самостоятельное наблюдение</h4>
        <p>{local.status === "ready_for_next_step" ? "Один шаг завершён. Бюджет сеанса использован." : states[local.status] || local.status}</p>
        <details><summary>Действие и результат</summary><pre>{JSON.stringify(local.results, null, 2)}</pre></details>
        {progress && !auto && local.approval?.status === "pending" && local.status === "awaiting_approval" &&
          <button disabled={busy || session.blocked} onClick={() => command("local-engine/decision", {job: local.job, project: local.project,
            run: local.approval!.run, approval: local.approval!.id, digest: local.approval!.digest, allow: true})}>Подтвердить один локальный шаг</button>}
      </article>}
      {session.learning_status === "unreviewed" && <p>Материал наставника сохранён для инженерной памяти · ожидает проверки.</p>}
    </>}
  </section>;
}
