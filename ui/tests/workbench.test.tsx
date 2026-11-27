import { strict as assert } from "node:assert";
import { test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { WorkingCopyView, type WorkingCopy } from "../src/Team.js";
import { MemoryView } from "../src/LocalEngine.js";

const copy: WorkingCopy = {
  revision: "revision", snapshot: "snapshot", source: { run: "engineer" },
  files: [{ path: "pricing.py", before: "return price + 10", after: "return price - 10", before_sha256: "hash" }],
  diff: "-return price + 10\n+return price - 10", backup: "sealed",
  rollback: "baseline_selected", test_contract: { cases: [] }, worker_ready: true,
  test_permitted: false, worker_profile: null,
  test: { status: "mock_failed", passed: 2, total: 6 },
  phases: {
    apply: { run: "apply", status: "succeeded", approval: null, artifacts: [] },
    test: { run: "test", status: "created", approval: { id: "approval", status: "pending", digest: "digest", expires_at: "date" }, artifacts: [] },
  },
};

test("shows original, candidate, backup, rollback and explicitly labeled Mock metrics", () => {
  const html = renderToStaticMarkup(createElement(WorkingCopyView, { job: "job", copy, blocked: false, command: () => {} }));
  assert.match(html, /return price \+ 10/);
  assert.match(html, /return price - 10/);
  assert.match(html, /Сохранена и проверена/);
  assert.match(html, /Возвращена исходная версия/);
  assert.match(html, /Mock: сценарий с провалом · 2\/6/);
  assert.match(html, /<button type="button" disabled="">Подтвердить: запустить функциональные тесты/);
});

test("places mentoring evidence outside trusted memory", () => {
  const html = renderToStaticMarkup(createElement(MemoryView, {
    entries: [],
    unreviewed: [{ id: "artifact", run: "run", sha256: "hash", author: "GPT Architect",
      limitation: "Статический разбор", material: { coaching: "Проверить выражение" } }],
  }));
  assert.match(html, /Непроверенные материалы/);
  assert.match(html, /исключены из доверенного контекста/);
  assert.match(html, /Проверить выражение/);
});
