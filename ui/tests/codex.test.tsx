import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup as render } from "react-dom/server";
import { CodexDiagnosticsView, type CodexDiagnostic } from "../src/Codex.js";

const connected: CodexDiagnostic = {
  cli_installed: true,
  cli_version: "0.171.0-alpha.13",
  app_server_version: "0.171.0-alpha.13",
  connection: "closed",
  auth: "confirmed",
  auth_mode: "chatgpt",
  observed_at: "2026-11-21T21:20:45Z",
  last_error: null,
  stop_confirmed: true,
  run_id: "a".repeat(32),
  run_status: "succeeded",
  agent_sessions: 0,
  turns: 0,
  models: [
    {
      model: "fixture-model",
      is_default: true,
      reasoning_efforts: ["low"],
      access_verified: false,
    },
  ],
};
test("Real diagnostic receipt shows versions, ChatGPT auth and closed connection", () => {
  const html = render(<CodexDiagnosticsView data={connected} />);
  assert.match(html, /0.171.0-alpha.13/);
  assert.match(html, /Подтверждена App Server/);
  assert.match(html, /chatgpt/);
  assert.match(html, /штатно завершён/);
});
test("Catalog entry never becomes verified generation access", () => {
  const html = render(<CodexDiagnosticsView data={connected} />);
  assert.match(html, /fixture-model/);
  assert.match(html, /не доказывает доступ к генерации/);
  assert.match(html, /Доступ к генерации не проверен/);
});
test("No active roles or command buttons are invented from successful metadata", () => {
  const html = render(<CodexDiagnosticsView data={connected} />);
  assert.match(html, /агентские сессии не запускались/);
  assert.doesNotMatch(html, /<button/);
  assert.match(html, /Запуск и завершение Codex через интерфейс недоступны/);
});
test("Missing Codex has no fake version or catalog", () => {
  const html = render(
    <CodexDiagnosticsView
      data={{
        cli_installed: false,
        connection: "not_checked",
        auth: "not_checked",
        models: [],
      }}
    />,
  );
  assert.match(html, /Не установлен/);
  assert.match(html, /Каталог не получен/);
  assert.doesNotMatch(html, /0.171/);
});
test("Login-required and auth-error remain different statuses", () => {
  assert.match(
    render(
      <CodexDiagnosticsView
        data={{ connection: "closed", auth: "login_required" }}
      />,
    ),
    /Требуется вход/,
  );
  assert.match(
    render(
      <CodexDiagnosticsView
        data={{
          connection: "error",
          auth: "error",
          last_error: "TransportLost",
        }}
      />,
    ),
    /Ошибка проверки авторизации/,
  );
});
test("Unconfirmed exit and unknown display a blocking state", () => {
  const html = render(
    <CodexDiagnosticsView
      data={{
        ...connected,
        connection: "unconfirmed",
        run_status: "unknown",
        stop_confirmed: false,
      }}
    />,
  );
  assert.match(html, /Новое соединение заблокировано/);
  assert.match(html, /Остановка собственного процесса пока не подтверждена/);
});
test("Model names and diagnostics are escaped", () => {
  const html = render(
    <CodexDiagnosticsView
      data={{
        ...connected,
        last_error: "<script>bad</script>",
        models: [
          {
            model: "<img src=x>",
            is_default: false,
            reasoning_efforts: [],
            access_verified: false,
          },
        ],
      }}
    />,
  );
  assert.ok(!html.includes("<script>"));
  assert.ok(!html.includes("<img src=x>"));
});
test("Failed effective restriction is not drawn as verified", () => {
  const html = render(
    <CodexDiagnosticsView
      data={{
        ...connected,
        restrictions: { read_only: true, no_mcp_servers: false },
        trace: [{ direction: "outbound", method: "config/read" }],
      }}
    />,
  );
  assert.match(html, /no_mcp_servers: Не подтверждено/);
  assert.match(html, /config\/read/);
  assert.match(html, /OS-sandbox агентской сессии не испытывались/);
});
