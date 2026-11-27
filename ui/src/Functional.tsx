import { useEffect, useRef, useState } from "react";

export interface FunctionalCheck {
  source_run_id: string;
  run_id: string;
  task_id: string;
  attempt: number;
  status: string;
  phase: string;
  error_code: string | null;
  code: string;
  code_sha256: string;
  answer_sha256: string;
  origin: string;
  suite_id: string;
  suite_digest: string;
  package_id: string;
  package_digest: string;
  cases: { id: string; input: number[]; expected: number }[];
  worker_configuration: {
    backend: { profile: Record<string, unknown>; review_digest?: string };
    limits: Record<string, number>;
  };
  approval: {
    id: string;
    status: string;
    spec_digest: string;
    expires_at: string;
  } | null;
  dispatch_allowed: boolean;
  static: { awarded: number; available: number; eligible: boolean };
  functional: {
    passed: number;
    total: number;
    functional_awarded: number;
    functional_available: number;
    cases: { id: string; status: string; diagnostic: string }[];
    resources: Record<string, number>;
  } | null;
  stdout: string | null;
  stderr: string | null;
  exit_code: number | null;
  stop_confirmed: boolean;
  worker_reason: string | null;
  evidence_sealed: boolean;
  observations: unknown[];
  bootstrap_stderr?: string | null;
  artifacts: { id: string; sha256: string; size: number }[];
}
const states: Record<string, string> = {
  created: "Проверка подготовлена",
  dispatching: "Ожидание Worker",
  running: "Выполняются фиксированные тесты",
  succeeded: "Выполнение подтверждено",
  failed: "Ошибка до начала выполнения",
  unknown: "Результат неизвестен — повтор запрещён",
  cancelled: "Отменено",
};
export function FunctionalView({
  check,
  busy = false,
  error = null,
  onPrepare,
  onDecision,
  onRevoke,
}: {
  check?: FunctionalCheck;
  busy?: boolean;
  error?: string | null;
  onPrepare?: () => void;
  onDecision?: (allow: boolean) => void;
  onRevoke?: () => void;
}) {
  const pending =
    check?.status === "created" &&
    ["pending", "approved"].includes(check.approval?.status || "");
  const active = check && ["running", "unknown"].includes(check.status);
  const measured =
    check?.status === "succeeded" &&
    check.stop_confirmed &&
    check.evidence_sealed
      ? check.functional
      : null;
  return (
    <section
      className="functional-panel"
      aria-label="Функциональная проверка сохранённого ответа"
    >
      <h3>Функциональная проверка · Docker Worker</h3>
      <p>
        Отдельная операция с сохранённым ответом. Модель повторно не вызывается.
        Подготовка не исполняет код. Доступен только ограниченный контракт
        sum_positive.
      </p>
      {error && (
        <p role="alert" className="training-error">
          {error}
        </p>
      )}
      <button
        className="training-primary"
        disabled={!!busy || !!active || check?.status === "succeeded"}
        onClick={onPrepare}
      >
        Подготовить функциональную проверку
      </button>
      {check && (
        <>
          <p role="status">
            {states[check.phase] || check.phase} · попытка {check.attempt}
            <br />
            Worker Run: <code>{check.run_id}</code>
            <br />
            Исходный Run: <code>{check.source_run_id}</code>
          </p>
          <details open={check.status === "created"}>
            <summary>Неизменённый код и SHA-256</summary>
            <pre>{check.code}</pre>
            <p className="training-digest">
              Код: {check.code_sha256}
              <br />
              Исходный ответ: {check.answer_sha256}
              <br />
              Пакет: {check.package_id}
              <br />
              {check.package_digest}
            </p>
          </details>
          <details>
            <summary>
              Фиксированный контракт · {check.cases.length} тестов
            </summary>
            <p>
              Сумма строго положительных целых чисел; вход не изменяется.
              Проверяется только эта конечная suite. Нового экзамена не
              создаётся.
            </p>
            <p>{check.suite_id}</p>
            <p className="training-digest">{check.suite_digest}</p>
            <ul>
              {check.cases.map((c) => (
                <li key={c.id}>
                  {c.id}: {JSON.stringify(c.input)} → {c.expected}
                </li>
              ))}
            </ul>
          </details>
          <details>
            <summary>Ограничения и образ Worker</summary>
            <p>
              UID/GID 65532 · сеть none · root read-only · CPU 0.5 · RAM/swap
              128 MiB · PIDs 16 · tmpfs 1 MiB. Без host mounts, socket, GPU и
              секретов. Capabilities: drop ALL, no-new-privileges, стандартный
              seccomp.
            </p>
            <pre>{JSON.stringify(check.worker_configuration, null, 2)}</pre>
          </details>
          <p>
            Статический допуск к Worker: {check.static.awarded}/
            {check.static.available}. Это отдельная рубрика от статической
            оценки занятия по шкале 45 баллов.
          </p>
          {pending && (
            <div className="approval-review">
              <h3>Отдельное подтверждение исполнения кода</h3>
              <p>
                Разрешить один запуск именно этого пакета в ограниченном Worker?
                Запрос Qwen не выполняется.
              </p>
              <p className="training-digest">
                Approval: {check.approval!.id}
                <br />
                SHA-256 операции: {check.approval!.spec_digest}
                <br />
                Действует до: {check.approval!.expires_at}
              </p>
              {!check.dispatch_allowed && (
                <p role="alert">
                  Исполнение ещё не разрешено пользователем в Codex. Браузер не
                  может включить права Worker. После отдельного разрешения
                  потребуется свежий Approval доверенной сессии и конфигурации.
                </p>
              )}
              <div className="training-row">
                <button
                  disabled={busy || !check.dispatch_allowed}
                  onClick={() => onDecision?.(true)}
                >
                  Подтвердить исполнение в Worker
                </button>
                <button disabled={busy} onClick={() => onDecision?.(false)}>
                  Отказать в исполнении
                </button>
                <button disabled={busy} onClick={onRevoke}>
                  Отозвать разрешение Worker
                </button>
              </div>
            </div>
          )}
          {check.approval && !pending && (
            <p>
              Approval:{" "}
              {(
                {
                  consumed: "использовано",
                  rejected: "отклонено",
                  revoked: "отозвано",
                  expired: "истекло",
                  invalidated: "область устарела",
                } as Record<string, string>
              )[check.approval.status] || check.approval.status}
            </p>
          )}
          {check.status === "unknown" && (
            <p role="alert">
              Не повторять. Нужна отдельная процедура подтверждения остановки и
              reconciliation результата. Этот интерфейс не разрешает повтор.
            </p>
          )}
          {(check.error_code || check.worker_reason) && (
            <p role="alert">
              Диагностика: {check.error_code || ""} {check.worker_reason || ""}
            </p>
          )}
          {measured ? (
            <>
              <h3>
                Функциональные тесты: {measured.passed}/{measured.total}
              </h3>
              <p>
                Функциональная оценка: {measured.functional_awarded}/
                {measured.functional_available}. Проверена только
                зафиксированная suite.
              </p>
              <ul>
                {measured.cases.map((c) => (
                  <li key={c.id}>
                    {c.id}:{" "}
                    {(
                      {
                        passed: "пройден",
                        failed: "провален",
                        error: "ошибка",
                      } as Record<string, string>
                    )[c.status] || c.status}{" "}
                    · {c.diagnostic}
                  </li>
                ))}
              </ul>
              <details>
                <summary>Измеренные ресурсы Worker</summary>
                <pre>{JSON.stringify(measured.resources, null, 2)}</pre>
              </details>
            </>
          ) : (
            <p>
              Функциональные баллы не начислены. Результат тестов пока не
              подтверждён.
            </p>
          )}
          {(check.stdout !== null ||
            check.stderr !== null ||
            check.exit_code !== null) && (
            <details>
              <summary>stdout / stderr / exit code</summary>
              <p>
                Код завершения: {check.exit_code ?? "Нет данных"}. Остановка
                подтверждена: {check.stop_confirmed ? "да" : "нет"}.
              </p>
              <h4>stdout</h4>
              <pre>{check.stdout ?? "Нет данных"}</pre>
              <h4>stderr</h4>
              <pre>{check.stderr ?? "Нет данных"}</pre>
              <h4>Диагностика запуска</h4>
              <pre>{check.bootstrap_stderr ?? "Нет данных"}</pre>
            </details>
          )}
          {!!check.artifacts.length && (
            <details>
              <summary>Доказательства и контрольные суммы</summary>
              <p>Manifest проверен: {check.evidence_sealed ? "да" : "нет"}</p>
              {check.artifacts.map((a) => (
                <p className="training-digest" key={a.id}>
                  {a.id} · {a.size} байт
                  <br />
                  {a.sha256}
                </p>
              ))}
              <pre>{JSON.stringify(check.observations, null, 2)}</pre>
            </details>
          )}
        </>
      )}
    </section>
  );
}

export function FunctionalPanel({
  sourceRun,
  check,
}: {
  sourceRun: string;
  check?: FunctionalCheck;
}) {
  const [current, setCurrent] = useState(check);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const gate = useRef(false);
  useEffect(() => setCurrent(check), [check]);
  async function command(kind: string, body: object) {
    if (gate.current) return;
    gate.current = true;
    setBusy(true);
    setError(null);
    try {
      const auth = {
        Authorization: `Bearer ${sessionStorage.getItem("ii-pro-session") || ""}`,
      };
      const s = await fetch("/api/session", {
        headers: auth,
        credentials: "omit",
        cache: "no-store",
        signal: AbortSignal.timeout(10000),
      });
      if (!s.ok) throw new Error("Нужна текущая локальная сессия");
      const { csrf } = (await s.json()) as { csrf: string };
      const r = await fetch("/api/school/functional/" + kind, {
        method: "POST",
        headers: {
          ...auth,
          "Content-Type": "application/json",
          "X-CSRF-Token": csrf,
        },
        body: JSON.stringify(body),
        credentials: "omit",
        cache: "no-store",
        signal: AbortSignal.timeout(15000),
      });
      const value = (await r.json()) as FunctionalCheck & { error?: string };
      if (!r.ok)
        throw new Error(value.error || "Функциональная команда отклонена");
      setCurrent(value);
    } catch (e) {
      setError((e as Error).message + " Автоматического повтора нет.");
    } finally {
      gate.current = false;
      setBusy(false);
    }
  }
  const scope = current?.approval
    ? {
        run_id: current.run_id,
        approval_id: current.approval.id,
        spec_digest: current.approval.spec_digest,
      }
    : null;
  return (
    <FunctionalView
      check={current}
      busy={busy}
      error={error}
      onPrepare={() => void command("prepare", { source_run_id: sourceRun })}
      onDecision={(allow) =>
        scope && void command("decision", { ...scope, allow })
      }
      onRevoke={() => scope && void command("revoke", scope)}
    />
  );
}
