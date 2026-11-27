import { useEffect, useState } from "react";

export interface CodexDiagnostic {
  cli_installed?: boolean;
  cli_version?: string | null;
  app_server_version?: string;
  connection: string;
  auth?: string;
  auth_mode?: string;
  observed_at?: string;
  models?: {
    model: string;
    is_default: boolean;
    reasoning_efforts: string[];
    access_verified: false;
  }[];
  last_error?: string | null;
  stop_confirmed?: boolean;
  run_id?: string;
  run_status?: string;
  agent_sessions?: number;
  turns?: number;
  restrictions?: Record<string, boolean>;
  trace?: { direction: string; method: string }[];
  historical?: boolean;
  elapsed_seconds?: number;
}
const connection: Record<string, string> = {
  not_checked: "Диагностика ещё не запускалась",
  starting: "Запускается диагностическое соединение",
  connected: "Диагностическое соединение открыто",
  closed: "Диагностический сеанс штатно завершён",
  error: "Ошибка диагностики",
  unconfirmed: "Завершение не подтверждено",
  disabled: "Диагностика отключена",
};
const auth: Record<string, string> = {
  not_checked: "Не проверена",
  checking: "Проверяется",
  confirmed: "Подтверждена App Server",
  login_required: "Требуется вход через ChatGPT",
  error: "Ошибка проверки авторизации",
};
export function CodexDiagnosticsView({ data }: { data: CodexDiagnostic }) {
  return (
    <section
      className="panel codex-diagnostics"
      aria-label="Диагностика Codex App Server"
    >
      <div className="panel-title">
        <h2>Codex App Server</h2>
        <span className="tag">Только диагностика</span>
      </div>
      <dl>
        <dt>Codex CLI</dt>
        <dd>
          {data.cli_installed === true
            ? "Установлен"
            : data.cli_installed === false
              ? "Не установлен"
              : "Нет данных"}
          {data.cli_version && ` · ${data.cli_version}`}
        </dd>
        <dt>App Server</dt>
        <dd>
          {data.app_server_version
            ? `Доступен по последней проверке · ${data.app_server_version}`
            : "Доступность не подтверждена"}
        </dd>
        <dt>Авторизация</dt>
        <dd>
          {auth[data.auth || "not_checked"] || "Не подтверждена"}
          {data.auth_mode && ` · ${data.auth_mode}`}
        </dd>
        <dt>Соединение</dt>
        <dd>{connection[data.connection] || "Нет данных"}</dd>
        <dt>Последняя проверка</dt>
        <dd>
          {data.observed_at
            ? new Intl.DateTimeFormat("ru-RU", {
                dateStyle: "short",
                timeStyle: "medium",
                timeZone: "Europe/Moscow",
              }).format(new Date(data.observed_at)) + " · МСК"
            : "Нет данных"}
        </dd>
      </dl>
      <p>
        GPT Architect и GPT Engineer не работают: агентские сессии не
        запускались. Проверка учётной записи и каталога не является запросом к
        GPT.
      </p>
      {data.last_error && (
        <p role="alert">
          Последняя ошибка: {data.last_error}. Автоматического переподключения
          нет.
        </p>
      )}
      {data.run_status === "unknown" && (
        <p role="alert" className="notice">
          Run unknown. Новое соединение заблокировано до отдельной проверки
          завершения и последствий.
        </p>
      )}
      {data.run_id && (
        <p className="mono">Диагностический Run: {data.run_id}</p>
      )}
      {data.stop_confirmed === false && (
        <p className="notice">
          Остановка собственного процесса пока не подтверждена.
        </p>
      )}
      <h3>Каталог моделей</h3>
      <p>
        Использование ещё не проверено. Наличие модели в model/list не
        доказывает доступ к генерации.
      </p>
      {data.models?.length ? (
        <ul className="codex-models">
          {data.models.map((m) => (
            <li key={m.model}>
              <strong>{m.model}</strong>
              {m.is_default && " · выбор каталога по умолчанию"}
              <small>
                Доступ к генерации не проверен · усилие:{" "}
                {m.reasoning_efforts.join(", ") || "Нет данных"}
              </small>
            </li>
          ))}
        </ul>
      ) : (
        <p>Каталог не получен.</p>
      )}
      {data.restrictions && (
        <details>
          <summary>Фактически проверенные ограничения</summary>
          <ul>
            {Object.entries(data.restrictions).map(([k, v]) => (
              <li key={k}>
                {k}: {v ? "Подтверждено" : "Не подтверждено"}
              </li>
            ))}
          </ul>
          <p>
            Это профиль metadata-only соединения. Реальное исполнение
            инструментов и OS-sandbox агентской сессии не испытывались.
          </p>
        </details>
      )}
      {data.trace && (
        <details>
          <summary>Разрешённые диагностические RPC</summary>
          <pre>{data.trace.map((r) => r.method).join("\n")}</pre>
        </details>
      )}
      <p className="muted">
        Браузер читает сохранённый результат. Запуск и завершение Codex через
        интерфейс недоступны. Новый диагностический Run запускает оператор;
        настоящий GPT-запрос требует отдельного разрешения.
      </p>
    </section>
  );
}
export function CodexDiagnosticsPanel() {
  const [data, setData] = useState<CodexDiagnostic | null>(null),
    [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const r = await fetch("/api/team/codex", {
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
        if (!r.ok)
          throw new Error(
            "Диагностика Codex недоступна. Данные могли устареть.",
          );
        const value = (await r.json()) as CodexDiagnostic;
        if (!controller.signal.aborted) {
          setData(value);
          setError(null);
        }
      } catch (e) {
        if (!controller.signal.aborted) setError((e as Error).message);
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
      {error && <p role="alert">{error}</p>}
      {data ? (
        <CodexDiagnosticsView data={data} />
      ) : (
        <p role="status">{error || "Читаем сохранённую диагностику Codex…"}</p>
      )}
    </>
  );
}
