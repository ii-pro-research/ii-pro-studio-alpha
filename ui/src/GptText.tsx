import { useEffect, useState } from "react";
import type { TeamPhase, TeamState } from "./Team.js";

export function TransportDiagnostics({ phase }: { phase: TeamPhase }) {
  const entry = [...phase.events].reverse().find((e) =>
    e.agent_event.kind === "status" && e.agent_event.data.type === "transport.diagnostic");
  const facts = entry?.agent_event.data;
  if (!facts) return phase.status === "unknown" || phase.status === "failed"
    ? <p>Диагностика транспорта для этого исторического запуска не сохранена.</p> : null;
  const outcomes: Record<string, string> = {
    before_send: "HTTP-запрос не отправлен",
    confirmed_http_rejection: "Получен подтверждённый HTTP-отказ",
    uncertain_after_send: "После начала отправки результат неопределён",
    completed: "Ответ получен и проверен",
  };
  const rows: [string, string][] = [
    ["Этап", "stage"], ["Передача запроса", "request_transmission"],
    ["HTTP-статус", "http_status"], ["Content-Type", "content_type"],
    ["Request ID", "request_id"], ["Первичное исключение", "primary_exception_type"],
    ["Код ошибки ОС", "os_error_number"], ["Код ошибки Windows", "windows_error_number"],
    ["Безопасное описание", "safe_message"], ["Код ошибки API", "api_error_code"],
    ["Получено байтов", "received_bytes"], ["SSE-кадров", "sse_frames"],
    ["Распознано SSE-событий", "sse_events"], ["Последнее событие", "last_event_type"],
    ["Получен response.created", "response_created_received"],
    ["Получен response.completed", "response_completed_received"],
    ["Причина завершения потока", "termination_reason"], ["Время наблюдения, с", "elapsed_seconds"],
  ];
  return <details open aria-label="Диагностика транспорта">
    <summary>Диагностика транспорта</summary>
    <p>{outcomes[String(facts.outcome)] || "Нет данных"}</p>
    <dl>{rows.map(([label, key]) => <div key={key}>
      <dt>{label}</dt>
      <dd>{facts[key] === null || facts[key] === undefined ? "Нет данных"
        : typeof facts[key] === "boolean" ? (facts[key] ? "Да" : "Нет") : String(facts[key])}</dd>
    </div>)}</dl>
    <p>Диагностика не разрешает повтор. HTTP-отказ и закрытие соединения сами по себе не доказывают отсутствие расхода лимитов.</p>
  </details>;
}

export function TextPhaseResult({
  phase,
  model,
}: {
  phase: TeamPhase;
  model: string;
}) {
  const result = phase.outputs["result.json"];
  const complete = phase.status === "succeeded" && phase.execution_stopped &&
    typeof result?.text === "string";
  const partial = phase.events
    .filter((e) => e.agent_event.kind === "message" &&
      e.agent_event.data.provisional === true &&
      typeof e.agent_event.data.delta === "string")
    .map((e) => String(e.agent_event.data.delta)).join("");
  const shadow = phase.outputs["shadow-material.json"];
  return (
    <section aria-label="Результат текстовой генерации">
      <p>Модель: {String(result?.model || model || "Нет данных")}</p>
      <TransportDiagnostics phase={phase} />
      {complete ? (
        <>
          <p role="status">Ответ подтверждён событием response.completed</p>
          <h4>Оригинальный ответ</h4>
          <pre>{String(result.text)}</pre>
          <p>Длительность: {String(result.elapsed_seconds ?? "Нет данных")} с</p>
          <p>Статистика использования</p>
          <pre>{result.usage && Object.keys(result.usage as object).length
            ? JSON.stringify(result.usage, null, 2) : "Нет данных"}</pre>
          {shadow && <p>Shadow Learning: материал сохранён, ожидает проверки наставником. Qwen не запускалась.</p>}
        </>
      ) : (
        <>
          <p role="status">{phase.status === "running"
            ? "Генерация — ожидаем подтверждения завершения"
            : phase.status === "unknown"
              ? "Результат неизвестен — автоматический повтор заблокирован"
              : "Подтверждённого ответа пока нет"}</p>
          {partial && <>
            <p>Поступающий текст — незавершённый ответ</p>
            <pre aria-label="Поступающий текст">{partial}</pre>
          </>}
        </>
      )}
    </section>
  );
}

export function GptTextView({ data }: { data: TeamState }) {
  const info = data.text;
  return (
    <section className="panel" aria-label="Текстовый GPT Architect">
      <h2>GPT Text · Architect</h2>
      {data.engineering?.supported && <p>
        GPT Engineer · {data.engineering.auth_ready ? "Авторизация SIWC готова" : "Требуется действующая авторизация SIWC"}
        {data.cloud?.status === "waiting_for_cloud_quota" && " · ожидание облачного лимита"}
      </p>}
      <p>
        {info?.auth_ready
          ? "Вход Studio подтверждён; каждый запрос требует отдельного разрешения."
          : "Continue with ChatGPT для Studio ещё не завершён либо срок входа истёк."}
      </p>
      <p>
        Модель: {info?.model || "Нет данных"}. Using ChatGPT plan — после
        OAuth-согласия. Инструменты и доступ к проектам не предоставляются.
      </p>
      <a
        href="https://chatgpt.com/settings/usage"
        target="_blank"
        rel="noreferrer"
      >
        Manage usage — лимиты и credits
      </a>
      <p>
        Подготовка и подтверждение — в разделе «Задачи команды». Сам вход не
        запускает модель.
      </p>
      {data.jobs
        .filter((j) => j.text_mode)
        .map((j) => (
          <article key={j.job_id}>
            <h3>{j.text}</h3>
            <p>
              {j.backend === "siwc_text_mock"
                ? "Mock — не ответ GPT"
                : "Responses Text / ChatGPT plan"}
            </p>
            {j.phases.map((p) => (
              <div key={p.run_id}>
                <p>
                  Run: {p.run_id} ·{" "}
                  {p.status === "unknown"
                    ? "unknown — повтор заблокирован"
                    : p.status}
                </p>
                <TextPhaseResult phase={p} model={info?.model || ""} />
              </div>
            ))}
          </article>
        ))}
    </section>
  );
}

export function GptTextPanel() {
  const [data, setData] = useState<TeamState | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const response = await fetch("/api/team/state", {
          headers: {
            Authorization:
              "Bearer " + (sessionStorage.getItem("ii-pro-session") || ""),
          },
          credentials: "omit",
          cache: "no-store",
          signal: AbortSignal.any([
            controller.signal,
            AbortSignal.timeout(10000),
          ]),
        });
        if (!response.ok) throw new Error();
        const value = (await response.json()) as TeamState;
        if (!controller.signal.aborted) {
          setData(value);
          setError(false);
        }
      } catch {
        if (!controller.signal.aborted) setError(true);
      } finally {
        if (!controller.signal.aborted)
          timer = setTimeout(() => void poll(), 5000);
      }
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, []);
  return (
    <>
      {error && (
        <p role="alert">
          Состояние GPT Text не обновлено; данные могли устареть.
        </p>
      )}
      {data && <GptTextView data={data} />}
    </>
  );
}
