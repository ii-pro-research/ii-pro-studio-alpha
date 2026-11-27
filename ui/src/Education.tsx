import { useEffect, useState } from "react";
import { JobView, type Job } from "./LocalEngine.js";
import { AttemptView, type TrainingAttempt } from "./Training.js";
import { FunctionalView, type FunctionalCheck } from "./Functional.js";

type Json = Record<string, unknown>;
type Consent = { id: string; status: string; spec_digest: string; expires_at: string };
type Suite = { id: string; title: string; category: string; task_sha256: string; suite_sha256: string; cases: number };
type Exam = { attempt: TrainingAttempt & { tokens_per_second?: number | null; execution_blocked?: boolean }; static: { eligible: boolean } | null;
  functional_run: string | null; functional: { passed: number; total: number } | null };
type Arm = { attempts: Record<string, Exam>; completed: number; solved: number; passed: number; total: number; hints: number;
  retries: number; tool_errors: number | null };
type StudyJob = Job & { mode: string; feedback: { author: string; text: string; next_step: string }[] };
export type StudyState = {
  lesson: { title: string; facts: string[]; observed_errors: string[]; mentor: string[]; assumptions: string[];
    evidence: { name: string; record: string; sha256: string }[] };
  lessons: StudyJob[]; catalog: Suite[]; model: string; experiment: string | null; arms: Record<string, Arm>; execution_blocked: boolean;
  pilot_order: [string, string][]; pilot_card_verified: boolean;
  knowledge: { run: string; status: string; entry: string; sha256: string; body: Json; history: { status: string }[]; approval: Consent | null } | null;
};
const arms = { a: "A · Без памяти", b: "B · С проверенной памятью", c: "C · С наставником" } as const;
const lessonModes = { without_memory: "Без памяти", verified_memory: "С проверенной памятью", mentored: "С наставником" } as const;

function details(title: string, value: unknown) {
  return <details><summary>{title}</summary><pre>{JSON.stringify(value, null, 2)}</pre></details>;
}

export function StudyProgress({ groups }: { groups: StudyState["arms"] }) {
  return <section aria-label="Фактическое сравнение">
    <h3>Результаты по режимам</h3>
    {(Object.keys(arms) as (keyof typeof arms)[]).map(key => {
      const data = groups[key];
      return <div key={key} className="study-progress">
        <strong>{arms[key]}</strong>
        {data?.completed ? <p>Решено {data.solved} из {data.completed} оценённых задач. {key === "c" && `Подсказок: ${data.hints}.`}</p>
          : <p>Оценённых задач пока нет.</p>}
        {data?.total ? <label>Пройдено {data.passed} из {data.total} выполненных проверок
          <meter min={0} max={data.total} value={data.passed} /></label> : null}
        {data && Object.keys(data.attempts).length > 0 && <p>Повторных попыток: {data.retries}. Ошибки выбора инструментов: {data.tool_errors ?? "не измеряются"}.</p>}
      </div>;
    })}
  </section>;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "omit", cache: "no-store", signal: AbortSignal.timeout(10000), ...init,
    headers: { Authorization: "Bearer " + (sessionStorage.getItem("ii-pro-session") || ""), ...init?.headers },
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Не удалось загрузить School");
  return result;
}

export function EducationPanel() {
  const [data, setData] = useState<StudyState | null>(null);
  const [checks, setChecks] = useState<FunctionalCheck[]>([]);
  const [mode, setMode] = useState("without_memory");
  const [arm, setArm] = useState<keyof typeof arms>("a");
  const [suite, setSuite] = useState("");
  const [job, setJob] = useState("");
  const [author, setAuthor] = useState("");
  const [note, setNote] = useState("");
  const [next, setNext] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function refresh() {
    const [state, training] = await Promise.all([
      api<StudyState | null>("/api/school/education"),
      api<{ functional_checks?: FunctionalCheck[] }>("/api/school/training"),
    ]);
    setData(state);
    setChecks(training.functional_checks || []);
  }
  useEffect(() => {
    if (!sessionStorage.getItem("ii-pro-session")) return;
    void refresh().catch(failure => setError(failure.message));
    const timer = setInterval(() => void refresh().catch(() => {}), 3500);
    return () => clearInterval(timer);
  }, []);
  async function send(path: string, value: object) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const session = await api<{ csrf: string }>("/api/session");
      const result = await api<{ job?: string }>(path, { method: "POST", body: JSON.stringify(value),
        headers: { "Content-Type": "application/json", "X-CSRF-Token": session.csrf } });
      if (result.job) setJob(result.job);
      await refresh();
    } catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  }
  if (!data) return error ? <p role="alert">{error}</p> : null;
  const study = (command: string, value: object) => void send("/api/school/education/" + command, value);
  const selected = suite || data.catalog[0]?.id || "";
  const experiment = data.arms[arm]?.attempts[selected];
  const attempt = experiment?.attempt;
  const executionBlocked = attempt?.execution_blocked ?? data.execution_blocked;
  const check = checks.find(f => f.run_id === experiment?.functional_run);
  const lesson = data.lessons.find(l => l.job === job) || data.lessons[0];
  const knowledge = data.knowledge;
  const confirmed = knowledge?.status === "succeeded" && knowledge.history.at(-1)?.status === "verified";
  return <section className="panel" aria-label="Инженерная школа">
    <h2>Инженерная школа</h2>
    <p>Ученик · II PRO · {data.model}</p>
    {error && <p role="alert">{error}</p>}
    <h3>{data.lesson.title}</h3>
    {([
      ["Функциональные доказательства H.1 и H.2", data.lesson.facts],
      ["Ошибки ученика", data.lesson.observed_errors],
      ["Рекомендации наставника", data.lesson.mentor],
      ["Границы выводов", data.lesson.assumptions],
    ] as const).map(([title, values]) => <details key={title}><summary>{title}</summary>
      <ul>{values.map(value => <li key={value}>{value}</li>)}</ul></details>)}
    {details("Источники урока и контрольные суммы", data.lesson.evidence)}
    <label>Режим урока <select value={mode} onChange={e => setMode(e.target.value)}>
      {Object.entries(lessonModes).map(([id,label]) => <option key={id} value={id}>{label}</option>)}
    </select></label>
    <button disabled={busy} onClick={() => {
      const id = crypto.randomUUID().replaceAll("-", "");
      setJob(id);
      study("lesson", { job: id, mode });
    }}>Подготовить урок</button>
    {lesson && <>
      <label>История уроков <select value={lesson.job} onChange={e => setJob(e.target.value)}>
        {data.lessons.map(item => <option key={item.job} value={item.job}>{lessonModes[item.mode as keyof typeof lessonModes]} · {item.job.slice(0,8)}</option>)}
      </select></label>
      <JobView job={lesson} busy={busy} command={(path, value) => void send("/api/local-engine/" + path, value)} />
      {lesson.feedback.length ? details("Разбор наставника и следующий шаг", lesson.feedback) : null}
      {lesson.feedback.length > 0 && <button disabled={busy} onClick={() => study("lesson", {
        job: crypto.randomUUID().replaceAll("-", ""), mode: "mentored", after: lesson.job,
      })}>Подготовить подзадачу наставника</button>}
      {lesson.runs.at(-1)?.status === "succeeded" && lesson.feedback.length === 0 &&
        <form onSubmit={e => { e.preventDefault(); study("feedback", { job: lesson.job, author, text: note, next_step: next }); }}>
          <label>Наставник<input maxLength={80} required value={author} onChange={e => setAuthor(e.target.value)} /></label>
          <label>Разбор<textarea maxLength={800} required value={note} onChange={e => setNote(e.target.value)} /></label>
          <label>Следующая подзадача<textarea maxLength={150} required value={next} onChange={e => setNext(e.target.value)} /></label>
          <button disabled={busy}>Сохранить разбор наставника</button>
        </form>}
    </>}
    <h3>Проверенная инженерная память</h3>
    {knowledge ? <><p>Карточка · {knowledge.history.at(-1)?.status}</p>{details("Доказательства, границы и Approval", knowledge)}</> : null}
    {!knowledge && data.pilot_card_verified && <p>Подтверждённая карточка доступна из исходной базы.</p>}
    {!confirmed && !data.pilot_card_verified && (!knowledge?.approval || ["expired", "invalidated"].includes(knowledge.approval.status)) &&
      <button disabled={busy} onClick={() => study("knowledge", {})}>Подготовить согласование знания H.1</button>}
    {knowledge?.status === "created" && knowledge.approval?.status === "pending" &&
      <button disabled={busy} onClick={() => study("decision", {
        run: knowledge.run, approval: knowledge.approval!.id, digest: knowledge.approval!.spec_digest,
      })}>Подтвердить только проверенную карточку</button>}
    <StudyProgress groups={data.arms} />
    <h3>Независимый экзамен</h3>
    <p>{data.catalog.length} задач. Один ответ на задачу. Выполнение проверок требует отдельного разрешения.</p>
    {executionBlocked && <p role="status">Выполнение выбранной попытки заблокировано, пока есть незавершённая или неопределённая операция.</p>}
    <button disabled={busy || !data.pilot_card_verified} onClick={() => study("pilot", {})}>Подготовить четыре попытки A/B</button>
    {details("Порядок пилота", data.pilot_order)}
    {!data.experiment && <button disabled={busy || !data.pilot_card_verified} onClick={() => study("plan", {id: crypto.randomUUID().replaceAll("-", "")})}>
      Подготовить сравнение A/B/C</button>}
    <label>Режим <select value={arm} onChange={e => setArm(e.target.value as keyof typeof arms)}>
      {Object.entries(arms).map(([id,label]) => <option key={id} value={id}>{label}</option>)}
    </select></label>
    <label>Задание <select value={selected} onChange={e => setSuite(e.target.value)}>
      {data.catalog.map(item => <option key={item.id} value={item.id}>{item.category} · {item.title}</option>)}
    </select></label>
    {details("Контрольные суммы экзаменационного набора", data.catalog)}
    {!attempt || attempt.status === "created" ?
      <button disabled={busy || !data.experiment} onClick={() => study("exam", {arm, suite: selected})}>
        {attempt ? "Обновить согласование ответа" : "Подготовить ответ на экзамен"}</button> : null}
    {attempt && <>
      <AttemptView attempt={attempt} />
      {attempt.tokens_per_second != null && <p>Скорость · {attempt.tokens_per_second.toFixed(2)} токена/с</p>}
      {attempt.status === "created" && attempt.approval?.status === "pending" &&
        <button disabled={busy || executionBlocked || !attempt.dispatch_allowed} onClick={() => void send("/api/school/decision", {
          run_id: attempt.run_id, approval_id: attempt.approval!.id, spec_digest: attempt.approval!.spec_digest, allow: true,
        })}>Подтвердить один ответ Qwen</button>}
      {attempt.status === "succeeded" && experiment.static?.eligible !== false && (!check || check.status === "created") &&
        <button disabled={busy} onClick={() => study("functional", {arm, suite: selected})}>
          {check ? "Обновить согласование проверки" : "Подготовить функциональную проверку"}</button>}
      {experiment.static?.eligible === false && <p>Профиль Worker отклонил кандидат статически. Функционального результата нет.</p>}
      {check && <FunctionalView check={check} busy={busy || executionBlocked} onDecision={allow => void send("/api/school/functional/decision",
        { run_id: check.run_id, approval_id: check.approval?.id, spec_digest: check.approval?.spec_digest, allow })} />}
    </>}
  </section>;
}
