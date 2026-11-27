import { useEffect, useRef, useState } from "react";
import type { State } from "./types.js";
import { FunctionalPanel, type FunctionalCheck } from "./Functional.js";

export interface Lesson {
  id: string;
  revision: number;
  title_ru: string;
  requirements: string;
  source: string;
  restrictions: string[];
  reference_criteria: string[];
  public_examples: string[];
  rubric: Record<string, number>;
}
export interface TrainingAttempt {
  run_id: string;
  task_id: string;
  session_id: string;
  lesson_id: string;
  attempt: number;
  status: string;
  phase: string;
  effect_status: string;
  error_code: string | null;
  backend: string;
  real_inference: boolean;
  created_at: string;
  prompt: string;
  approval: {
    id: string;
    status: string;
    spec_digest: string;
    expires_at: string;
  } | null;
  response: string | null;
  response_sha256: string | null;
  response_redacted?: boolean;
  assessment: {
    score_awarded: number;
    score_available: number;
    scores: Record<string, number>;
    findings: string[];
    functional_status: string;
  } | null;
  artifacts: {
    id: string;
    relative_path: string;
    sha256: string;
    size: number;
  }[];
  diagnostics?: string[];
  model_id?: string;
  executor?: string;
  dispatch_allowed?: boolean;
  profile?: {
    model: string;
    expected_digest: string;
    options_json: string;
    deadline_seconds: number;
  };
  executor_revision?: string;
  policy_revision?: string;
  prompt_sha256?: string;
  elapsed_seconds?: number | null;
  statistics?: Record<string, number>;
}
export interface TrainingState {
  functional_preparation_enabled?: boolean;
  functional_checks?: FunctionalCheck[];
  mode: string;
  real_inference_enabled: boolean;
  authorized_real_run?: string | null;
  code_execution_enabled: boolean;
  stopping: boolean;
  models: {
    id: string;
    label: string;
    target: string;
    actual_model: string;
    options: Record<string, number>;
    deadline_seconds: number;
    expected_digest?: string;
  }[];
  lessons: Lesson[];
  attempts: TrainingAttempt[];
}
const phaseText: Record<string, string> = {
  created: "Подготовлено · ожидает решения",
  dispatching: "Запуск MockExecutor",
  running: "Получение тестового ответа MockExecutor",
  succeeded: "Ответ сохранён",
  failed: "Ошибка до начала выполнения",
  unknown: "Результат не определён · повтор заблокирован",
  cancelled: "Отменено",
};
const criteriaText: Record<string, string> = {
  syntax: "Синтаксис",
  signature: "Сигнатура функции",
  single_function_scope: "Одна функция без явного ввода-вывода",
  static_evidence: "Статические признаки решения",
  functional_pending: "Функциональная проверка · не выполняется",
};
export function attemptPhase(attempt: TrainingAttempt) {
  if (attempt.real_inference && attempt.phase === "running")
    return "Генерация Qwen7B через Ollama";
  if (attempt.real_inference && attempt.phase === "dispatching")
    return "Ожидание запуска Ollama";
  return phaseText[attempt.phase] || attempt.phase;
}
export function AttemptView({ attempt }: { attempt: TrainingAttempt }) {
  return (
    <article className="training-attempt">
      <div className="training-row">
        <strong>{attemptPhase(attempt)}</strong>
        <span className="mock-label">
          {attempt.real_inference
            ? "REAL · Ollama Qwen7B"
            : `${attempt.backend} · не Qwen`}
        </span>
      </div>
      <p>
        Попытка {attempt.attempt} · <code>{attempt.run_id}</code>
        <br />
        {attempt.created_at}
      </p>
      {!!attempt.diagnostics?.length && (
        <p role="alert">Диагностика: {attempt.diagnostics.join(", ")}</p>
      )}
      {attempt.error_code && (
        <p role="alert">
          Ошибка Core: {attempt.error_code}. Автоматического повтора нет.
        </p>
      )}
      {attempt.status === "unknown" && (
        <p role="alert">
          Нужна отдельная проверка результата и reconciliation в Core. Новый
          запуск через панель запрещён.
        </p>
      )}
      <details>
        <summary>Исходный запрос</summary>
        <pre>{attempt.prompt}</pre>
      </details>
      {attempt.response !== null && (
        <>
          <h3>
            Исходный ответ ·{" "}
            {attempt.real_inference ? "Ollama Qwen7B" : "MockExecutor"}
          </h3>
          <p>
            {attempt.real_inference
              ? "Оригинальный ответ локальной модели без исправлений. Код не исполнялся."
              : "Тестовая фикстура разработчика. Модель не вызывалась; код не исполнялся."}
          </p>
          <pre>{attempt.response}</pre>
          {attempt.response_redacted && (
            <p>
              Часть отображения скрыта фильтром приватности. Оригинальные байты
              сохранены в School; SHA-256 относится к оригиналу.
            </p>
          )}
          <div className="training-digest">
            SHA-256 оригинала: {attempt.response_sha256}
          </div>
        </>
      )}
      {attempt.real_inference && attempt.response !== null && (
        <div className="training-budget">
          <p>
            Время ответа:{" "}
            {typeof attempt.elapsed_seconds === "number"
              ? `${attempt.elapsed_seconds.toFixed(2)} с`
              : "Нет данных"}{" "}
            · Выходных токенов: {attempt.statistics?.eval_count ?? "Нет данных"}{" "}
            · Входных токенов:{" "}
            {attempt.statistics?.prompt_eval_count ?? "Нет данных"}
          </p>
          <p>
            Скорость:{" "}
            {typeof attempt.statistics?.eval_count === "number" &&
            (attempt.statistics?.eval_duration ?? 0) > 0
              ? `${((attempt.statistics!.eval_count * 1e9) / attempt.statistics!.eval_duration).toFixed(2)} ток/с`
              : "Нет данных"}
          </p>
          <details>
            <summary>Статистика Ollama и параметры сохранённого ответа</summary>
            <pre>
              {JSON.stringify(
                {
                  model: attempt.profile?.model,
                  digest: attempt.profile?.expected_digest,
                  options: attempt.profile?.options_json,
                  elapsed_seconds: attempt.elapsed_seconds,
                  statistics: attempt.statistics,
                },
                null,
                2,
              )}
            </pre>
          </details>
        </div>
      )}
      {attempt.assessment ? (
        <>
          <h3>
            Статический анализ: {attempt.assessment.score_awarded} из{" "}
            {attempt.assessment.score_available}
          </h3>
          <p>
            Только AST-эвристики. Функциональная оценка отсутствует; 55 баллов
            не проверялись. Экспертиза GPT не проводилась.
          </p>
          <ul>
            {Object.entries(attempt.assessment.scores).map(([key, value]) => (
              <li key={key}>
                {criteriaText[key] || key}: {value}
              </li>
            ))}
          </ul>
          <details>
            <summary>Диагностика анализатора</summary>
            <pre>{attempt.assessment.findings.join("\n")}</pre>
          </details>
        </>
      ) : (
        attempt.status === "succeeded" && (
          <p>
            Статическая оценка пока не сохранена. Повтор генерации не
            выполняется.
          </p>
        )
      )}
      {!!attempt.artifacts.length && (
        <details>
          <summary>Артефакты и контрольные суммы</summary>
          {attempt.artifacts.map((a) => (
            <p className="training-digest" key={a.id}>
              {a.relative_path} · {a.size} байт
              <br />
              {a.sha256}
            </p>
          ))}
        </details>
      )}
    </article>
  );
}

export function TrainingView({
  data,
  busy = false,
  error = null,
  selected = null,
  onPrepare,
  onDecision,
}: {
  data: TrainingState;
  busy?: boolean;
  error?: string | null;
  selected?: string | null;
  onPrepare?: (lesson: string, model: string) => void;
  onDecision?: (attempt: TrainingAttempt, allow: boolean) => void;
}) {
  const prepared = data.attempts.find((a) => a.status === "created");
  const [lessonId, setLessonId] = useState(
    prepared?.lesson_id || data.lessons[0]?.id || "",
  );
  const [modelId, setModelId] = useState(
    prepared?.model_id || data.models[0]?.id || "",
  );
  const lesson = data.lessons.find((l) => l.id === lessonId);
  const model = data.models.find((m) => m.id === modelId);
  const attempt =
    data.attempts.find((a) => a.run_id === selected) || data.attempts[0];
  const unresolved = data.attempts.some(
    (a) =>
      a.status === "unknown" ||
      a.status === "running" ||
      a.phase === "dispatching",
  );
  const pending =
    attempt?.status === "created" &&
    ["pending", "approved"].includes(attempt.approval?.status || "");
  return (
    <section className="training-panel" aria-label="Управление занятием">
      <div className="training-row">
        <div>
          <div className="eyebrow">SCHOOL v0.5</div>
          <h2>Первый урок — под вашим контролем</h2>
        </div>
        <span className="mock-label">
          {model?.id === "ollama-qwen7b"
            ? "REAL · Ollama Qwen7B"
            : "Тестовый режим · MockExecutor"}
        </span>
      </div>
      <p>
        Подготовка сохраняет задание и попытку в Core. MockExecutor возвращает
        тестовую фикстуру; Ollama Qwen7B получает настоящий запрос только после
        отдельного разрешения на Run и Approval. Реальный вызов Ollama и
        исполнение кода заблокированы по умолчанию. Для сохранённого ответа
        функциональная проверка требует отдельного Worker Run и Approval.
      </p>
      <div className="training-selects">
        <label>
          Урок
          <select
            value={lessonId}
            disabled={busy || pending || unresolved}
            onChange={(e) => setLessonId(e.target.value)}
          >
            {data.lessons.map((l) => (
              <option key={l.id} value={l.id}>
                {l.title_ru}
              </option>
            ))}
          </select>
        </label>
        <label>
          Локальная модель / режим
          <select
            value={modelId}
            disabled={busy || pending || unresolved}
            onChange={(e) => setModelId(e.target.value)}
          >
            {data.models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      {lesson && (
        <details open>
          <summary>Задание и критерии · ревизия {lesson.revision}</summary>
          <p>{lesson.requirements}</p>
          <pre>{lesson.source}</pre>
          <h3>Критерии</h3>
          <ul>
            {lesson.reference_criteria.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
          <p>
            {Object.entries(lesson.rubric)
              .map(([k, v]) => `${criteriaText[k] || k}: ${v}`)
              .join(" · ")}
          </p>
          <details>
            <summary>Ограничения и публичные примеры</summary>
            <ul>
              {[...lesson.restrictions, ...lesson.public_examples].map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          </details>
        </details>
      )}
      {model && (
        <p className="training-budget">
          Целевая модель: {model.target}. Фактический исполнитель:{" "}
          {model.id === "ollama-qwen7b" ? "Ollama Qwen7B" : "MockExecutor"}(
          {model.actual_model}). Параметры для будущего вызова:{" "}
          {JSON.stringify(model.options)}. Лимит {model.deadline_seconds} с.
          {model.expected_digest && (
            <>
              <br />
              Закреплённый digest: {model.expected_digest}
            </>
          )}
        </p>
      )}
      {error && (
        <p role="alert" className="training-error">
          {error}
        </p>
      )}
      <button
        className="training-primary"
        disabled={busy || unresolved || data.stopping || !lesson || !model}
        onClick={() => onPrepare?.(lessonId, modelId)}
      >
        {busy ? "Обработка команды…" : "Подготовить занятие"}
      </button>
      {pending && (
        <div
          className="approval-review"
          role="region"
          aria-label="Подтверждение операции"
        >
          <h3>
            {attempt.real_inference
              ? "Подтвердите реальное занятие Qwen7B"
              : "Подтвердите тестовое занятие"}
          </h3>
          <p>
            Разрешить ровно одну попытку <b>school.propose</b> через{" "}
            <b>{attempt.real_inference ? "Ollama Qwen7B" : "MockExecutor"}</b>?{" "}
            {attempt.real_inference
              ? "Будет отправлен один настоящий запрос локальной модели. Docker и выполнение кода не используются."
              : "Qwen, Docker и выполнение кода не используются."}
          </p>
          <p>
            Задача: <code>{attempt.task_id}</code>
            <br />
            Run: <code>{attempt.run_id}</code> · попытка {attempt.attempt}
            <br />
            Урок:{" "}
            {data.lessons.find((l) => l.id === attempt.lesson_id)?.title_ru}
          </p>
          <p className="training-digest">
            Approval: {attempt.approval!.id}
            <br />
            SHA-256 операции: {attempt.approval!.spec_digest}
            <br />
            Действует до: {attempt.approval!.expires_at}
            <br />
            Исполнитель: {attempt.executor || attempt.backend} ·{" "}
            {attempt.executor_revision || "Нет данных"}
            <br />
            Политика: {attempt.policy_revision || "Нет данных"}
            {attempt.profile && (
              <>
                <br />
                Модель: {attempt.profile.model}
                <br />
                Digest: {attempt.profile.expected_digest}
                <br />
                Параметры: {attempt.profile.options_json}
              </>
            )}
          </p>
          <details>
            <summary>Точный запрос этой операции</summary>
            <pre>{attempt.prompt}</pre>
          </details>
          <div className="training-row">
            <button
              className="training-primary"
              disabled={busy || attempt.dispatch_allowed === false}
              onClick={() => onDecision?.(attempt, true)}
            >
              {attempt.real_inference
                ? "Подтвердить запрос Qwen7B"
                : "Подтвердить MockExecutor"}
            </button>
            <button
              disabled={busy}
              onClick={() => onDecision?.(attempt, false)}
            >
              Отклонить
            </button>
          </div>
          {attempt.real_inference && attempt.dispatch_allowed === false && (
            <p role="alert">
              Реальный запрос ещё не разрешён. Требуется отдельное подтверждение
              пользователя в Codex для этого Run. Браузер не может включить
              разрешение.
            </p>
          )}
        </div>
      )}
      {attempt?.approval && !pending && (
        <p>
          Решение Approval:{" "}
          {(
            {
              approved: "одобрено",
              consumed: "использовано",
              rejected: "отклонено",
              revoked: "отозвано",
              expired: "истекло",
              invalidated: "устарело",
              pending: "ожидает",
            } as Record<string, string>
          )[attempt.approval.status] || attempt.approval.status}
          . Для нового решения снова подготовьте операцию.
        </p>
      )}
      <h3>История занятий интерфейса · {data.attempts.length}</h3>
      {!data.attempts.length && (
        <p>Занятий ещё нет. Подготовка не обращается к модели.</p>
      )}
      {data.attempts.map((a) => (
        <details
          className="training-history"
          key={a.run_id}
          open={a.run_id === attempt?.run_id}
        >
          <summary>
            {data.lessons.find((l) => l.id === a.lesson_id)?.title_ru} ·{" "}
            {attemptPhase(a)} · {a.real_inference ? "REAL / Ollama" : "MOCK"}
          </summary>
          <AttemptView attempt={a} />
          {data.functional_preparation_enabled &&
            a.status === "succeeded" &&
            a.lesson_id === "bug-positive-sum" && (
              <FunctionalPanel
                sourceRun={a.run_id}
                check={data.functional_checks?.find(
                  (c) => c.source_run_id === a.run_id,
                )}
              />
            )}
        </details>
      ))}
    </section>
  );
}

function headers() {
  return {
    Authorization: `Bearer ${sessionStorage.getItem("ii-pro-session") || ""}`,
  };
}
export function resumableRequest(
  attempts: TrainingAttempt[],
  lesson: string,
  model: string,
) {
  return attempts.find(
    (a) =>
      a.status === "created" && a.lesson_id === lesson && a.model_id === model,
  )?.session_id;
}
export function TrainingPanel({ ollama }: { ollama?: State["ollama"] }) {
  const [data, setData] = useState<TrainingState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const gate = useRef(false);
  const mounted = useRef(true);
  async function load(signal?: AbortSignal) {
    const r = await fetch("/api/school/training", {
      headers: headers(),
      cache: "no-store",
      credentials: "omit",
      signal: signal
        ? AbortSignal.any([signal, AbortSignal.timeout(10000)])
        : AbortSignal.timeout(10000),
    });
    if (r.status === 404 || r.status === 405)
      throw new Error(
        "Учебный API ещё не запущен. Перезапустите панель через Start-Studio.cmd.",
      );
    if (!r.ok)
      throw new Error(
        "История занятий недоступна. Не повторяйте неопределённую команду; обновите состояние.",
      );
    const value = (await r.json()) as TrainingState;
    if (mounted.current) setData(value);
  }
  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        await load(controller.signal);
      } catch (e) {
        if (!controller.signal.aborted && mounted.current)
          setError((e as Error).message);
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(poll, 1000);
      }
    }
    void poll();
    return () => {
      mounted.current = false;
      controller.abort();
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
      if (!session.ok)
        throw new Error(
          "Нужна локальная сессия. Откройте панель через Start-Studio.cmd.",
        );
      const { csrf } = (await session.json()) as { csrf: string };
      const response = await fetch(path, {
        method: "POST",
        headers: {
          ...headers(),
          "Content-Type": "application/json",
          "X-CSRF-Token": csrf,
        },
        credentials: "omit",
        cache: "no-store",
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(15000),
      });
      const value = (await response.json()) as TrainingAttempt & {
        error?: string;
      };
      if (!response.ok) throw new Error(value.error || "Команда отклонена");
      if (mounted.current) {
        setSelected(value.run_id);
        await load();
      }
    } catch (e) {
      if (mounted.current)
        setError((e as Error).message + " Автоматического повтора нет.");
    } finally {
      gate.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  function prepare(lesson: string, model: string) {
    // Retained across reloads and ambiguous HTTP outcomes: another click on
    // the same lesson observes the same Run. A new lesson has a different key.
    const scope = `${lesson}:${model}`;
    const stored = sessionStorage.getItem("ii-pro-lesson:" + scope);
    const id =
      resumableRequest(data?.attempts || [], lesson, model) ||
      stored ||
      crypto.randomUUID().replaceAll("-", "");
    sessionStorage.setItem("ii-pro-lesson:" + scope, id);
    void command("/api/school/prepare", {
      request_id: id,
      lesson_id: lesson,
      model_id: model,
    });
  }
  if (!data)
    return (
      <section className="training-panel">
        <h2>Управление занятием</h2>
        <p role={error ? "alert" : "status"}>
          {error || "Загрузка реального состояния учебного API…"}
        </p>
      </section>
    );
  return (
    <>
      <p className="training-budget" role="status">
        Ollama:{" "}
        {ollama?.status === "available"
          ? ollama.model_available
            ? "доступен · целевая Qwen установлена"
            : "доступен · целевая Qwen не найдена"
          : "недоступен или нет данных"}
        . Это наблюдение API; MockExecutor не обращается к Ollama.
      </p>
      <TrainingView
        data={data}
        busy={busy}
        error={error}
        selected={selected}
        onPrepare={prepare}
        onDecision={(a, allow) =>
          void command("/api/school/decision", {
            run_id: a.run_id,
            approval_id: a.approval!.id,
            spec_digest: a.approval!.spec_digest,
            allow,
          })
        }
      />
    </>
  );
}
