import { useEffect, useRef, useState } from "react";

type Plan = { id: string; sha256: string; baseline: string; candidate: string; order: string[] };
type Slot = { job: string; case: string; arm: string; status: string; correct: boolean | null; seconds?: number; run?: string };
type Choice = { id: string; sha256: string; operation: string };
type State = {
  plan?: Plan; frozen: boolean; blocked: boolean; local_enabled: boolean;
  active?: string; rollback_available: boolean; pending_decision?: Choice; decisions: Choice[];
  anchor: string; slots: Slot[]; evaluation: { verdict: string; reason?: string; a?: number; b?: number; pairs?: number };
  observations: object[]; problems: { category: string; issue: string; count: number }[];
  hypotheses: { id: string; title: string; evidence: string; status: string }[];
};
type Job = { job: string; project: string; can_approve: boolean; approval?: { id: string; run: string; digest: string; expires_at: string; operation: object } };

const decisions: Record<string, string> = { accept: "Внедрить", reject: "Отклонить", rollback: "Откатить" };
const verdicts: Record<string, string> = { inconclusive: "Данных недостаточно", supported: "Критерий выполнен", not_supported: "Критерий не выполнен" };

function detail(label: string, data: unknown) {
  return <details><summary>{label}</summary><pre>{JSON.stringify(data, null, 2)}</pre></details>;
}

export function EvolutionPanel({ state, busy, job, command }: {
  state: State; busy: boolean; job?: Job; command: (path: string, value: object) => void;
}) {
  const plan = state.plan;
  const preview = state.pending_decision;
  const next = state.slots.find(row => row.status !== "succeeded");
  const blocked = busy || state.blocked;
  return <section className="panel" aria-label="Evolution Engine">
    <h1>Evolution Engine</h1>
    <p>Результаты экспериментов с инженерной обвязкой.</p>
    {state.blocked && <p role="status">Запуск заблокирован до разрешения незавершённой операции.</p>}
    <h2>Наблюдения и проблемы</h2>
    <ul>{state.problems.map(p => <li key={p.category + p.issue}>{p.issue} · {p.category} · {p.count}</li>)}</ul>
    {detail("Наблюдения, метрики и ссылки на Run", state.observations)}
    <h2>Гипотезы</h2>
    <ul>{state.hypotheses.map(h => <li key={h.id}>{h.title} · <code>{h.evidence}</code></li>)}</ul>
    <h2>Сравнение A/B</h2>
    {plan && <>
      <p>База <code>{plan.baseline}</code> · Кандидат <code>{plan.candidate}</code></p>
      {detail("Версии, бюджет, критерии и хеши", plan)}
      {state.frozen ? <p>План закреплён в журнале.</p> : <button disabled={busy}
        onClick={() => command("evolution-freeze", {sha256: plan.sha256})}>Закрепить план эксперимента</button>}
    </>}
    <p>{verdicts[state.evaluation.verdict] || state.evaluation.verdict}
      {state.evaluation.pairs != null && ` · A ${state.evaluation.a}/${state.evaluation.pairs}, B ${state.evaluation.b}/${state.evaluation.pairs}`}</p>
    <ul>{state.slots.map(row => <li key={row.case + row.arm}>{row.case} · {row.arm.toUpperCase()} · {row.status}
      {row.correct != null && (row.correct ? " · верный прогноз" : " · неверный прогноз")}
      {row.seconds != null && ` · ${row.seconds.toFixed(2)} с`}</li>)}</ul>
    {detail("Доказательства, измерения и контрольные суммы", state.slots)}
    {state.frozen && next && !state.decisions.length && ["planned", "created"].includes(next.status) &&
      <button disabled={blocked} onClick={() => command("evolution-prepare", {})}>Подготовить очередной Run</button>}
    {job?.approval && job.can_approve && <>{detail("Операция Local и срок Approval", job.approval)}
      <button disabled={blocked || !state.local_enabled} onClick={() => command("local-decision", {
        project: job.project, job: job.job, run: job.approval!.run, approval: job.approval!.id,
        digest: job.approval!.digest, allow: true,
      })}>Подтвердить один шаг Local</button></>}
    <h2>Внедрение и откат</h2>
    <p>{state.active ? "Рабочая версия " + state.active : "Рабочая версия сравнения — базовая"}</p>
    {preview ? <>{detail("Точное решение для согласования", preview)}
      <button disabled={blocked} onClick={() => command("evolution-decision", {
        id: preview.id, sha256: preview.sha256, confirm: true,
      })}>{decisions[preview.operation]} · подтвердить</button></> : state.frozen && <>
        {!state.decisions.length &&
          <button disabled={blocked} onClick={() => command("evolution-decision", {operation: "reject"})}>Подготовить отклонение</button>}
        {!state.decisions.length && state.evaluation.verdict === "supported" &&
          <button disabled={blocked} onClick={() => command("evolution-decision", {operation: "accept"})}>Подготовить внедрение</button>}
        {state.rollback_available &&
          <button disabled={blocked} onClick={() => command("evolution-decision", {operation: "rollback"})}>Подготовить откат</button>}
      </>}
    {detail("История решений и откатов", state.decisions)}
  </section>;
}

async function api(path: string, options?: RequestInit) {
  const response = await fetch(path, {
    ...options, credentials: "omit", cache: "no-store", signal: AbortSignal.timeout(10000),
    headers: {Authorization: "Bearer " + (sessionStorage.getItem("ii-pro-session") || ""), ...options?.headers},
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "Раздел Evolution Engine недоступен");
  return value;
}

export function EvolutionView() {
  const [state, setState] = useState<State>();
  const [job, setJob] = useState<Job>();
  const [error, setError] = useState("");
  const gate = useRef(false);
  const [busy, setBusy] = useState(false);
  async function refresh() {
    const next = await api("/api/team/evolution") as State;
    setState(next);
    const waiting = next.slots.find(row => row.status === "created");
    setJob(waiting ? await api(`/api/local-engine/jobs/${next.anchor}/${waiting.job}`) : undefined);
  }
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function read() {
      if (!gate.current) {
        try { await refresh(); }
        catch (fault) { if (!cancelled) setError((fault as Error).message); }
      }
      if (!cancelled) timer = setTimeout(read, 4000);
    }
    void read();
    return () => { cancelled = true; clearTimeout(timer); };
  }, []);
  async function command(path: string, value: object) {
    if (gate.current) return;
    gate.current = true;
    setBusy(true);
    setError("");
    try {
      const {csrf} = await api("/api/session");
      await api(path === "local-decision" ? "/api/local-engine/decision" : "/api/team/" + path,
        {method: "POST", body: JSON.stringify(value), headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf}});
      await refresh();
    } catch (fault) { setError((fault as Error).message); }
    finally { gate.current = false; setBusy(false); }
  }
  return <>{error && <p role="alert">{error}</p>}
    {state && <EvolutionPanel state={state} job={job} busy={busy} command={(path, value) => void command(path, value)} />}</>;
}
