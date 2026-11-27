import { useEffect, useState, type ReactNode } from "react";
import type { Exam, State } from "./types.js";
import { TrainingPanel } from "./Training.js";
import { EducationPanel } from "./Education.js";
import { EngineeringWorkspace } from "./LocalEngine.js";
import { GptTextPanel } from "./GptText.js";
import { CodexDiagnosticsPanel } from "./Codex.js";
import { EvolutionView } from "./Evolution.js";
import { Continuity } from "./Continuity.js";

type Page =
  | "home"
  | "team"
  | "teamtasks"
  | "evolution"
  | "school"
  | "projects"
  | "history"
  | "resources"
  | "settings";
const pages: { id: Page; title: string; icon: string }[] = [
  { id: "home", title: "Главная", icon: "grid" },
  { id: "team", title: "Команда ИИ", icon: "team" },
  { id: "teamtasks", title: "Задачи команды", icon: "code" },
  { id: "evolution", title: "Evolution Engine", icon: "pulse" },
  { id: "school", title: "Школа II PRO", icon: "school" },
  { id: "projects", title: "Проекты", icon: "folder" },
  { id: "history", title: "История заданий", icon: "clock" },
  { id: "resources", title: "Ресурсы", icon: "pulse" },
  { id: "settings", title: "Настройки", icon: "settings" },
];
const names: Record<string, string> = {
  succeeded: "Завершено",
  failed: "Ошибка",
  unknown: "Результат неизвестен",
  created: "Подготовлено",
  running: "Выполняется по журналу",
  cancelled: "Отменено",
  not_started: "Не запускалось",
  closed: "Штатно завершено",
  unconfirmed: "Закрытие не подтверждено",
  available: "Доступно",
  unavailable: "Нет связи",
  not_connected: "Не подключён",
  text_ready: "Авторизация SIWC готова",
  passed: "Пройден",
  error: "Ошибка",
  draft: "Черновик",
  planned: "Запланировано",
  verifying: "Ожидает проверки",
  awaiting_approval: "Ожидает подтверждения",
  needs_revision: "Нужна доработка",
  verified: "Проверено",
  saved: "Сохранено",
};
export function Badge({ status }: { status: string }) {
  return (
    <span
      className={`badge ${["succeeded", "available", "passed", "verified", "saved"].includes(status) ? "good" : ["unknown", "unconfirmed", "awaiting_approval"].includes(status) ? "warn" : ["failed", "error", "unavailable"].includes(status) ? "bad" : "neutral"}`}
    >
      <i />
      {names[status] || "Нет данных"}
    </span>
  );
}
export function metric(value: number | null | undefined, suffix = "") {
  return value == null
    ? "Нет данных"
    : `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(value)}${suffix}`;
}
function date(value: string | null) {
  return value
    ? new Intl.DateTimeFormat("ru-RU", {
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
        timeZone: "Europe/Moscow",
      }).format(new Date(value))
    : "Нет данных";
}
function titleText(value: string) {
  return (
    (
      {
        "GPT Team · plan": "План команды",
        "GPT Team · implement": "Реализация команды",
        "GPT Team · review": "Проверка команды",
        "Codex App Server diagnostics": "Диагностика Codex App Server",
        "School bounded functional evaluation":
          "Функциональная проверка sum_positive",
        "Bounded baseline evaluation": "Исходный экзамен sum_positive",
        "Find and fix the accumulator bug": "Поиск ошибки накопления суммы",
        "Write an integer clamp": "Функция ограничения целого числа",
        "Improve stable deduplication":
          "Удаление повторов с сохранением порядка",
        "Independent control exercise": "Независимое контрольное задание",
      } as Record<string, string>
    )[value] || value
  );
}
function Icon({ name, size = 20 }: { name: string; size?: number }) {
  const paths: Record<string, ReactNode> = {
    grid: (
      <>
        <rect x="3" y="3" width="7" height="7" rx="1.5" />
        <rect x="14" y="3" width="7" height="7" rx="1.5" />
        <rect x="3" y="14" width="7" height="7" rx="1.5" />
        <rect x="14" y="14" width="7" height="7" rx="1.5" />
      </>
    ),
    team: (
      <>
        <circle cx="9" cy="8" r="3" />
        <path d="M3 21v-3a6 6 0 0 1 12 0v3M17 5a3 3 0 0 1 0 6M18 15a5 5 0 0 1 3 5" />
      </>
    ),
    school: (
      <>
        <path d="m2 9 10-5 10 5-10 5L2 9Zm4 2v7c4 3 8 3 12 0v-7M22 9v7" />
      </>
    ),
    folder: (
      <path d="M3 6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6Z" />
    ),
    clock: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7v5l4 2" />
      </>
    ),
    pulse: <path d="M2 12h4l3-8 6 16 3-8h4" />,
    settings: (
      <>
        <circle cx="12" cy="12" r="3" />
        <path d="m9 3-1 3-3 1 1 4-2 3 3 2 1 4h4l2 1 2-3 4-1v-4l1-3-3-2-1-4-4 1-4-1Z" />
      </>
    ),
    arrow: <path d="m8 5 7 7-7 7" />,
    check: <path d="m5 12 4 4L19 6" />,
    lock: (
      <>
        <rect x="5" y="10" width="14" height="11" rx="3" />
        <path d="M8 10V7a4 4 0 0 1 8 0v3" />
      </>
    ),
    code: (
      <>
        <path d="m8 6-6 6 6 6m8-12 6 6-6 6m-3-15-2 18" />
      </>
    ),
  };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name] || paths.grid}
    </svg>
  );
}
function Panel({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return <section className={`panel ${className}`}>{children}</section>;
}
function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="empty">
      <Icon name="folder" size={30} />
      <p>{children}</p>
    </div>
  );
}
function Stat({
  label,
  value,
  sub,
  icon = "pulse",
}: {
  label: string;
  value: ReactNode;
  sub: ReactNode;
  icon?: string;
}) {
  return (
    <Panel className="stat">
      <div className="stat-top">
        <span>{label}</span>
        <Icon name={icon} />
      </div>
      <strong>{value}</strong>
      <small>{sub}</small>
    </Panel>
  );
}
function FutureActions() {
  return (
    <div className="future-actions">
      <span className="muted">Управление · следующий этап</span>
      <div>
        <button disabled title="Безопасная серверная логика ещё не подключена">
          Запустить занятие
        </button>
        <button disabled title="Approval недоступен в read-only Alpha">
          Подтвердить
        </button>
        <button disabled title="Остановка не реализована в read-only Alpha">
          STOP ALL — остановить всё
        </button>
      </div>
    </div>
  );
}

export function ExamCard({
  exam,
  onOpen,
}: {
  exam: Exam | null;
  onOpen: () => void;
}) {
  if (!exam)
    return (
      <Panel>
        <div className="panel-title">
          <h2>Функциональный экзамен</h2>
        </div>
        <Empty>Подтверждённых функциональных результатов пока нет</Empty>
      </Panel>
    );
  const f = exam.functional;
  return (
    <Panel className="exam-hero">
      <div className="hero-copy">
        <div className="eyebrow">
          <span className="live-dot" /> ПОДТВЕРЖДЕНО АРТЕФАКТАМИ
        </div>
        <h2>Первый экзамен локального ИИ</h2>
        <p>
          Qwen7B <span className="dim">/</span> Python{" "}
          <span className="dim">/</span> <code>sum_positive</code>
        </p>
        <div className="hero-checks">
          {f?.cases.map((c) => (
            <span
              className={c.status === "passed" ? "" : "bad-case"}
              key={c.id}
              title={c.id + ": " + (names[c.status] || c.status)}
            >
              <Icon
                name={c.status === "passed" ? "check" : "pulse"}
                size={16}
              />
            </span>
          ))}
          <b>
            {f?.passed}/{f?.total} тестов пройдено
          </b>
        </div>
        <button className="text-button" onClick={onOpen}>
          Открыть результат <Icon name="arrow" size={16} />
        </button>
      </div>
      <div className="score">
        <div className="score-orbit">
          <strong>{exam.total_awarded ?? "—"}</strong>
          <span>из 100 баллов</span>
        </div>
        <small>Только проверенный контракт</small>
      </div>
    </Panel>
  );
}

function History({
  data,
  compact = false,
}: {
  data: State;
  compact?: boolean;
}) {
  const values = compact ? data.runs.slice(0, 4) : data.runs;
  return (
    <Panel>
      <div className="panel-title">
        <h2>{compact ? "Последние попытки" : "История выполнений"}</h2>
        <span className="muted">{data.runs.length} в источниках</span>
      </div>
      {values.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Задание</th>
                <th>Исполнитель</th>
                <th>Попытка</th>
                <th>Статус</th>
                <th>Время · МСК</th>
              </tr>
            </thead>
            <tbody>
              {values.map((r) => (
                <tr key={r.source + r.id}>
                  <td>
                    <strong>
                      {titleText(
                        data.tasks.find(
                          (t) => t.source === r.source && t.id === r.task_id,
                        )?.title || "Задание",
                      )}
                    </strong>
                    <small className="mono">{r.id.slice(0, 12)}</small>
                    {r.error_code && (
                      <small className="error-code">{r.error_code}</small>
                    )}
                  </td>
                  <td>
                    {r.diagnostic
                      ? "Codex App Server · диагностика"
                      : r.text_generation
                        ? `${r.backend} · ${r.real_inference ? "REAL" : r.backend?.includes("Mock") ? "MOCK" : "генерация не подтверждена"}`
                        : r.backend
                          ? `${r.backend} · ${r.real_inference ? "REAL" : "MOCK"}`
                          : r.executor === "worker"
                            ? "Docker Worker"
                            : r.executor === "school_student"
                              ? "Qwen7B"
                              : r.executor}
                  </td>
                  <td>№ {r.attempt}</td>
                  <td>
                    <Badge status={r.status} />
                    {r.status === "unknown" && (
                      <small>Повтор заблокирован</small>
                    )}
                  </td>
                  <td className="muted">
                    {date(r.finished_at || r.created_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty>
          История пока пуста. Здесь появятся настоящие попытки Core.
        </Empty>
      )}
    </Panel>
  );
}
function Home({
  data,
  navigate,
}: {
  data: State;
  navigate: (page: Page) => void;
}) {
  const f = data.latest_exam?.functional;
  const last = data.attempts.find(
    (a) => a.real_inference === true && a.elapsed_seconds !== null,
  );
  return (
    <>
      <div className="intro">
        <div>
          <div className="eyebrow">ВАША ЛОКАЛЬНАЯ ИИ-СТУДИЯ</div>
          <h1>
            Всё под наблюдением<span className="accent">.</span>
          </h1>
          <p>Состояние системы, учебная история и проверенные результаты.</p>
        </div>
        <div className="local-pill">
          <span className="live-dot" /> На этом компьютере
        </div>
      </div>
      <div className="stats">
        <Stat
          label="II PRO Core"
          value={data.core.data_available ? "Снимок доступен" : "Нет данных"}
          sub={
            !data.core.data_available
              ? "Источник недоступен"
              : data.core.owner_state === "closed"
                ? "Ядро штатно завершено"
                : "Закрытие владельца не подтверждено"
          }
          icon="grid"
        />
        <Stat
          label="Ollama"
          value={data.ollama.status === "available" ? "На связи" : "Нет связи"}
          sub={
            data.ollama.model_available
              ? "qwen7b-fast:latest доступна"
              : "Доступность модели не подтверждена"
          }
          icon="pulse"
        />
        <Stat
          label="Docker Worker"
          value={
            data.worker.jobs[0]?.stop_confirmed
              ? "Завершён"
              : data.worker.jobs[0]
                ? "Не подтверждён"
                : "Нет запусков"
          }
          sub={
            data.worker.daemon === "available"
              ? "Docker доступен · режим просмотра"
              : "Связь с Docker не подтверждена"
          }
          icon="code"
        />
        <Stat
          label="Последний экзамен"
          value={f ? `${f.passed} / ${f.total}` : "Нет данных"}
          sub={
            f ? "Тестов в исходном экзамене" : "Функциональных оценок пока нет"
          }
          icon="school"
        />
      </div>
      <div className="home-grid">
        <ExamCard exam={data.latest_exam} onOpen={() => navigate("school")} />
        <Panel className="model-panel">
          <div className="panel-title">
            <h2>Локальный ученик</h2>
            <span className="model-logo">Q</span>
          </div>
          <h3>
            Qwen2.5-Coder <span>7B</span>
          </h3>
          <p className="muted">qwen7b-fast:latest</p>
          <Badge
            status={data.ollama.model_available ? "available" : "unavailable"}
          />
          <div className="model-metrics">
            <div>
              <small>Последний ответ</small>
              <strong>{metric(last?.elapsed_seconds, " с")}</strong>
            </div>
            <div>
              <small>Скорость генерации</small>
              <strong>{metric(last?.tokens_per_second, " ток/с")}</strong>
            </div>
          </div>
          <small className="muted">
            Метрики сохранённого ответа. Генерация не запускается.
          </small>
        </Panel>
      </div>
      <History data={data} compact />
      <div className="bottom-grid">
        <Panel>
          <div className="panel-title">
            <h2>Команда студии</h2>
            <button className="text-button" onClick={() => navigate("team")}>
              Все роли <Icon name="arrow" size={14} />
            </button>
          </div>
          <div className="team-strip">
            {data.team.map((r, i) => (
              <div key={r.role}>
                <span className={`role-avatar role-${i}`}>
                  {["A", "E", "Q"][i]}
                </span>
                <div>
                  <strong>{r.role}</strong>
                  <small>
                    {r.role === "Qwen7B" && r.status === "available"
                      ? "Локальный Ollama"
                      : names[r.status] || "Нет данных"}
                  </small>
                </div>
                <span
                  className={`status-dot ${["available", "text_ready"].includes(r.status) ? "on" : ""}`}
                />
              </div>
            ))}
          </div>
        </Panel>
        <Panel>
          <div className="panel-title">
            <h2>Состояние наблюдения</h2>
            <Icon name="lock" />
          </div>
          {data.warnings.length ? (
            data.warnings.map((w, i) => (
              <p className="notice" key={i}>
                {w.message}
              </p>
            ))
          ) : (
            <p className="notice good-notice">
              <Icon name="check" /> Источники прочитаны. Активных ошибок
              наблюдения нет.
            </p>
          )}
          <p className="muted small">
            Код исполняется только через отдельный Worker Approval. Занятия
            доступны в Школе.
          </p>
        </Panel>
      </div>
    </>
  );
}
export function SchoolView({ data }: { data: State }) {
  const [chosen, setChosen] = useState<string | null>(null);
  const exam =
    data.exams.find((e) => e.id === chosen) ||
    data.latest_exam ||
    data.exams[0];
  return (
    <>
      <div className="page-heading">
        <div className="eyebrow">УЧЁБА С ПРОВЕРЯЕМЫМ РЕЗУЛЬТАТОМ</div>
        <h1>Школа II PRO</h1>
        <p>
          Оригинальные решения, независимые критерии и сохранённые
          доказательства.
        </p>
      </div>
      <TrainingPanel ollama={data.ollama} />
      <EducationPanel />
      <div className="school-tabs">
        <span className="selected-tab">
          Экзамены <b>{data.exams.length}</b>
        </span>
        <span>
          Ученики <b>{new Set(data.students.map((s) => s.id)).size}</b>
        </span>
        <span>
          Попытки <b>{data.attempts.length}</b>
        </span>
      </div>
      <div className="school-layout">
        <Panel className="exam-list">
          <div className="panel-title">
            <h2>Учебные эксперименты</h2>
          </div>
          {data.exams.map((e) => (
            <button
              className={`exam-select ${e.id === exam?.id ? "selected" : ""}`}
              key={e.id}
              onClick={() => setChosen(e.id)}
            >
              <span className="exam-icon">
                <Icon name="code" />
              </span>
              <span>
                <strong>sum_positive</strong>
                <small>Исходный экзамен · Python</small>
                <small>{date(e.created_at)}</small>
              </span>
              <b>{e.total_awarded ?? "—"}</b>
            </button>
          ))}
          {!data.exams.length && <Empty>Сохранённых экзаменов нет</Empty>}
          <div className="list-note">
            <Icon name="lock" size={16} />
            <span>
              Практика и контроль отделены. Скрытые тесты не отправляются
              ученику.
            </span>
          </div>
        </Panel>
        <div className="exam-detail">
          {exam ? (
            <>
              <Panel>
                <div className="panel-title">
                  <div>
                    <div className="eyebrow">
                      ИСХОДНЫЙ ЭКЗАМЕН · ОДНА ПОПЫТКА
                    </div>
                    <h2 className="large-title">sum_positive</h2>
                  </div>
                  <Badge status={exam.status} />
                </div>
                <p className="muted">
                  Сумма строго положительных целых чисел без изменения входного
                  списка.
                </p>
                <div className="grade-grid">
                  <div>
                    <small>Статическая проверка</small>
                    <strong>
                      {exam.static_awarded}
                      <em> / {exam.static_available}</em>
                    </strong>
                    <span>Синтаксис, сигнатура, AST-профиль</span>
                  </div>
                  <div>
                    <small>Функциональные тесты</small>
                    <strong>
                      {exam.functional
                        ? exam.functional.functional_awarded
                        : "—"}
                      <em> / 70</em>
                    </strong>
                    <span>
                      {exam.functional
                        ? `${exam.functional.passed} из ${exam.functional.total} тестов`
                        : "Не выполнялись"}
                    </span>
                  </div>
                  <div className="grade-total">
                    <small>Подтверждённая оценка</small>
                    <strong>
                      {exam.total_awarded ?? "—"}
                      <em> / 100</em>
                    </strong>
                    <span>В границах этой конечной suite</span>
                  </div>
                </div>
              </Panel>
              <Panel className="code-panel">
                <div className="panel-title">
                  <h2>
                    <Icon name="code" size={18} /> Оригинальное решение Qwen
                  </h2>
                  <span className="tag">Python · только просмотр</span>
                </div>
                <pre>
                  <code>{exam.code || "Нет извлечённого кода"}</code>
                </pre>
                <div className="code-footer">
                  <Icon name="lock" size={14} />
                  {exam.code_redacted
                    ? "Чувствительные фрагменты скрыты; хэш относится к оригиналу"
                    : "Исходный код сохранён без исправлений"}
                </div>
              </Panel>
              <Panel>
                <div className="panel-title">
                  <h2>Результаты тестов</h2>
                  <span className="muted">
                    {exam.functional
                      ? `${exam.functional.passed}/${exam.functional.total}`
                      : "Нет функциональной оценки"}
                  </span>
                </div>
                {exam.functional ? (
                  <div className="case-grid">
                    {exam.functional.cases.map((c, i) => (
                      <div
                        className={`case ${c.status === "passed" ? "" : "bad-case"}`}
                        key={c.id}
                      >
                        <span>
                          <Icon
                            name={c.status === "passed" ? "check" : "pulse"}
                            size={16}
                          />{" "}
                          Тест {i + 1}
                        </span>
                        <Badge status={c.status} />
                      </div>
                    ))}
                  </div>
                ) : (
                  <Empty>
                    Код не проверен функционально. Баллы не начислены.
                  </Empty>
                )}
              </Panel>
              {exam.evidence && (
                <Panel>
                  <div className="panel-title">
                    <h2>Доказательства выполнения</h2>
                    <span className="tag mint">Целостность проверена</span>
                  </div>
                  <div className="evidence-summary">
                    <span>SQLite BLOB + зафиксированный manifest</span>
                    <span>{exam.evidence.artifacts.length} артефакта</span>
                  </div>
                  <details>
                    <summary>Контрольные суммы и идентификаторы</summary>
                    <div className="hash-list">
                      <label>
                        SHA-256 исходного кода<code>{exam.code_sha256}</code>
                      </label>
                      <label>
                        SHA-256 пакета
                        <code>{exam.evidence.package_sha256}</code>
                      </label>
                      <label>
                        Manifest<code>{exam.evidence.manifest_sha256}</code>
                      </label>
                      <label>
                        Run<code>{exam.run_id}</code>
                      </label>
                      {exam.evidence.artifacts.map((a) => (
                        <label key={a.id}>
                          {a.name} · {a.bytes} байт<code>{a.sha256}</code>
                        </label>
                      ))}
                    </div>
                  </details>
                </Panel>
              )}
            </>
          ) : (
            <Panel>
              <Empty>
                Подтверждённых данных экзамена нет. Значения не подставляются
                автоматически.
              </Empty>
            </Panel>
          )}
        </div>
      </div>
      <Panel>
        <div className="panel-title">
          <h2>Ученики и модели</h2>
        </div>
        <div className="student-grid">
          {data.students.map((s) => (
            <div key={s.source + s.id}>
              <span className="role-avatar role-2">Q</span>
              <div>
                <strong>{s.name}</strong>
                <small>
                  {data.models.find((m) => m.id === s.model_id)?.name ||
                    "Модель не найдена"}
                </small>
                <small>
                  Источник:{" "}
                  {data.sources.find((x) => x.key === s.source)?.title}
                </small>
              </div>
            </div>
          ))}
        </div>
      </Panel>
      <Panel>
        <div className="panel-title">
          <h2>Задания и история попыток</h2>
        </div>
        <div className="lesson-grid">
          {data.lessons
            .filter((l) => !l.id.startsWith("v2-"))
            .map((l) => (
              <div key={l.source + l.id}>
                <strong>{titleText(l.title)}</strong>
                <small>
                  {["exam", "baseline", "control"].includes(l.kind)
                    ? "Экзаменационный набор"
                    : "Практика"}
                </small>
              </div>
            ))}
        </div>
        <div className="model-profiles">
          {data.models.map((m) => (
            <details key={m.source + m.id}>
              <summary>Профиль модели · {m.name}</summary>
              <p className="mono">Digest: {m.digest}</p>
              <p className="mono">
                {Object.entries(m.options)
                  .map(([k, v]) => `${k}=${v}`)
                  .join(" · ")}
              </p>
            </details>
          ))}
        </div>
        <div className="attempt-list">
          {data.attempts.map((a) => (
            <details key={a.source + a.run_id}>
              <summary>
                Попытка №{a.sequence} · {a.lesson_id} · {date(a.created_at)} ·{" "}
                {a.backend || "School"}{" "}
                {a.backend ? (a.real_inference ? "REAL" : "MOCK") : ""}
              </summary>
              <Badge status={a.status} />
              {a.static_legacy && (
                <p>
                  Сохранённая статическая оценка:{" "}
                  {String(a.static_legacy.score_awarded)} /{" "}
                  {String(a.static_legacy.score_available)}. Функциональная
                  оценка этого занятия не выполнялась.
                </p>
              )}
              <p>
                {metric(a.elapsed_seconds, " с")} ·{" "}
                {metric(a.tokens, " токенов")} ·{" "}
                {metric(a.tokens_per_second, " ток/с")}
              </p>
              <pre>{a.raw_answer || "Ответ отсутствует"}</pre>
              <small className="mono">
                SHA-256: {a.answer_sha256 || "Нет данных"}
              </small>
            </details>
          ))}
        </div>
        {data.feedback.length ? (
          data.feedback.map((f, i) => (
            <p key={i}>
              {f.author}: {f.text}
            </p>
          ))
        ) : (
          <p className="muted">
            Замечаний наставников в этих источниках нет. Реальные GPT-наставники
            не подключены.
          </p>
        )}
      </Panel>
    </>
  );
}

export function Resources({ data }: { data: State }) {
  const r = data.resources,
    g = r.gpus[0];
  const used =
    r.ram_total_mib != null && r.ram_available_mib != null
      ? r.ram_total_mib - r.ram_available_mib
      : null;
  return (
    <>
      <div className="page-heading">
        <div className="eyebrow">ПОКАЗАНИЯ ЭТОГО КОМПЬЮТЕРА</div>
        <h1>Ресурсы</h1>
        <p>
          Датчики Windows и NVIDIA. Отсутствующий показатель не заменяется
          нулём.
        </p>
      </div>
      <div className="stats">
        <Stat
          label="Загрузка CPU"
          value={metric(r.cpu_percent, " %")}
          sub="Windows · между двумя наблюдениями"
        />
        <Stat
          label="Оперативная память"
          value={used == null ? "Нет данных" : metric(used / 1024, " ГиБ")}
          sub={`Всего ${metric(r.ram_total_mib == null ? null : r.ram_total_mib / 1024, " ГиБ")}`}
        />
        <Stat
          label="Загрузка GPU"
          value={metric(g?.utilization, " %")}
          sub={g?.name || "Нет данных NVIDIA"}
        />
        <Stat
          label="Видеопамять"
          value={metric(g?.used_mib, " МиБ")}
          sub={`Всего ${metric(g?.total_mib, " МиБ")}`}
        />
      </div>
      <div className="bottom-grid">
        <Panel>
          <div className="panel-title">
            <h2>{g?.name || "Датчики GPU"}</h2>
            <Icon name="pulse" />
          </div>
          <div className="sensor-grid">
            <div>
              <small>Температура</small>
              <strong>{metric(g?.temperature, " °C")}</strong>
            </div>
            <div>
              <small>Энергопотребление</small>
              <strong>{metric(g?.power_w, " Вт")}</strong>
            </div>
          </div>
          <p className="muted small">
            nvidia-smi · фиксированный диагностический запрос
          </p>
        </Panel>
        <Panel>
          <div className="panel-title">
            <h2>Загруженные модели Ollama</h2>
            <Badge status={data.ollama.status} />
          </div>
          {data.ollama.loaded.length ? (
            data.ollama.loaded.map((m) => (
              <p key={m.name}>
                <strong>{m.name}</strong>
                <small>
                  Размер модели: {metric(m.size_mib, " МиБ")} · VRAM:{" "}
                  {metric(m.vram_mib, " МиБ")}
                </small>
              </p>
            ))
          ) : (
            <Empty>
              {data.ollama.status === "available"
                ? "Сейчас моделей в памяти нет. Установленная Qwen доступна для будущего запуска."
                : "Нет данных: Ollama недоступен."}
            </Empty>
          )}
        </Panel>
      </div>
      <Panel>
        <div className="panel-title">
          <h2>Сохранённая производительность ответов</h2>
          <span className="muted">Без новых генераций</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Ответ</th>
                <th>Время</th>
                <th>Выходные токены</th>
                <th>Скорость генерации</th>
              </tr>
            </thead>
            <tbody>
              {data.attempts.map((a) => (
                <tr key={a.source + a.run_id}>
                  <td>
                    {a.lesson_id} · {a.backend || "School"}{" "}
                    {a.backend ? (a.real_inference ? "REAL" : "MOCK") : ""}
                  </td>
                  <td>{metric(a.elapsed_seconds, " с")}</td>
                  <td>{metric(a.tokens)}</td>
                  <td>{metric(a.tokens_per_second, " ток/с")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </>
  );
}

export function App({
  data,
  error = null,
  loading = false,
  initialPage = "home",
}: {
  data: State | null;
  error?: string | null;
  loading?: boolean;
  initialPage?: Page;
}) {
  const [page, setPage] = useState<Page>(initialPage);
  const current = pages.find((p) => p.id === page)!;
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "instant" });
  }, [page]);
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">II</div>
          <div>
            <strong>
              II PRO <span>Studio</span>
            </strong>
            <small>ЛОКАЛЬНЫЙ ИИ-ИНЖЕНЕР</small>
          </div>
        </div>
        <div className="workspace">
          <span className="workspace-icon">II</span>
          <div>
            <strong>Моя студия</strong>
            <small>Локальное пространство</small>
          </div>
          <span className="alpha-tag">α</span>
        </div>
        <div className="nav-label">РАБОЧЕЕ ПРОСТРАНСТВО</div>
        <nav aria-label="Основное меню">
          {pages.map((p) => (
            <button
              key={p.id}
              className={page === p.id ? "active" : ""}
              aria-label={p.title}
              title={p.title}
              aria-current={page === p.id ? "page" : undefined}
              onClick={() => setPage(p.id)}
            >
              <Icon name={p.icon} />
              <span>{p.title}</span>
              {p.id === "school" && data?.latest_exam && (
                <b className="nav-count">{data.exams.length}</b>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="readonly-note">
            <Icon name="lock" />
            <strong>Безопасный режим</strong>
            <p>
              Школа — Mock / Ollama.
              <br />
              Worker — отдельный Approval.
            </p>
          </div>
          <div className="version">
            <span className="live-dot" /> Studio Alpha <span>v0.1</span>
          </div>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div>
            <span className="breadcrumb">Рабочее пространство</span>
            <Icon name="arrow" size={13} />
            <strong>{current.title}</strong>
          </div>
          <div>
            <span className="readonly-pill">
              <Icon name="lock" size={13} /> Данные под контролем
            </span>
            <span className="avatar">II</span>
          </div>
        </header>
        <main>
          <div className="observation">
            <span className={error ? "status-dot" : "status-dot on"} />
            {error
              ? "Данные могли устареть"
              : loading
                ? "Читаем локальные источники…"
                : `Обновлено ${date(data?.observed_at || null)} · МСК`}
            <span>Обновление каждые 5 секунд</span>
          </div>
          {error && (
            <div className="error-banner" role="alert">
              {error}
            </div>
          )}
          {page === "teamtasks" ? <EngineeringWorkspace /> : page === "evolution" ? <EvolutionView /> : !data ? (
            <Panel>
              <Empty>
                {loading
                  ? "Подключаемся к локальному API…"
                  : "Нет данных. Откройте панель через Start-Studio.cmd."}
              </Empty>
            </Panel>
          ) : (
            <>
              {page === "home" && <Home data={data} navigate={setPage} />}{" "}
              {page === "school" && <SchoolView data={data} />}{" "}
              {page === "history" && (
                <>
                  <div className="page-heading">
                    <h1>История заданий</h1>
                    <p>
                      Записи существующих Core. Неопределённый результат всегда
                      остаётся unknown.
                    </p>
                  </div>
                  <History data={data} />
                </>
              )}{" "}
              {page === "resources" && <Resources data={data} />}{" "}
              {page === "team" && (
                <>
                  <div className="page-heading">
                    <div className="eyebrow">ТРИ РОЛИ · ЧЕСТНЫЙ СТАТУС</div>
                    <h1>Команда ИИ</h1>
                    <p>
                      Локальный ученик и наставники. GPT Text показан отдельно
                      от проектных агентов; каждый запрос требует подтверждения.
                    </p>
                  </div>
                  <CodexDiagnosticsPanel />
                  <GptTextPanel />
                  <div className="role-grid">
                    {data.team.map((r, i) => (
                      <Panel key={r.role}>
                        <span className={`role-avatar large role-${i}`}>
                          {["A", "E", "Q"][i]}
                        </span>
                        <h2>{r.role}</h2>
                        <p>{r.description}</p>
                        <Badge status={r.status} />
                        <p className="muted small">
                          {i < 2
                            ? "Доступ к проектам отключён. Статус текстового режима показан выше в GPT Text."
                            : "Подключение определяется по локальному Ollama и наличию модели."}
                        </p>
                      </Panel>
                    ))}
                  </div>
                </>
              )}{" "}
              {page === "projects" && (
                <>
                  <div className="page-heading">
                    <h1>Проекты</h1>
                    <p>
                      Зарегистрированные источники этой студии. Пользовательские
                      каталоги не сканируются.
                    </p>
                  </div>
                  <Panel>
                    <div className="panel-title">
                      <h2>{data.project.name}</h2>
                      <span className="tag">{data.project.language}</span>
                    </div>
                    <p>
                      Ветка <code>{data.project.branch || "Нет данных"}</code> ·
                      коммит <code>{data.project.commit || "Нет данных"}</code>
                    </p>
                    <div className="source-grid">
                      {data.sources.map((s) => (
                        <div key={s.key}>
                          <strong>{s.title}</strong>
                          <Badge status={s.status} />
                          <small>
                            SQLite {s.schema ?? "—"} ·{" "}
                            {names[s.owner_state || ""] || "Нет данных"}
                          </small>
                        </div>
                      ))}
                    </div>
                  </Panel>
                </>
              )}{" "}
              {page === "settings" && (
                <>
                  <div className="page-heading">
                    <h1>Настройки</h1>
                    <p>Параметры Alpha отображаются без изменения системы.</p>
                  </div>
                  <Panel>
                    <h2>Локальная и безопасная</h2>
                    <dl>
                      <dt>Доступ</dt>
                      <dd>Только loopback · авторизованная сессия</dd>
                      <dt>Источники</dt>
                      <dd>
                        Явный список из {data.sources.length} локальных баз
                      </dd>
                      <dt>Обновление</dt>
                      <dd>5 секунд; показания могут быть недоступны</dd>
                      <dt>Конфиденциальность</dt>
                      <dd>Секреты и абсолютные пути не выдаются API</dd>
                      <dt>SQLite</dt>
                      <dd>
                        Чтение частных копий DB + WAL; без миграций и
                        восстановления Core
                      </dd>
                    </dl>
                    <FutureActions />
                  </Panel>
                  <Continuity />
                </>
              )}
            </>
          )}
          <footer>
            <span>
              II PRO Studio Alpha <span className="dim">/</span> Данные остаются
              на компьютере
            </span>
            <span>Никаких автоматических запусков</span>
          </footer>
        </main>
      </div>
    </div>
  );
}
