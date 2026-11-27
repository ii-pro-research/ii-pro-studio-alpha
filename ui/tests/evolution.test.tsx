import { strict as assert } from "node:assert";
import { test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { EvolutionPanel } from "../src/Evolution.js";

test("unknown preserves the evidence and blocks a generation or deployment", () => {
  const view = renderToStaticMarkup(createElement(EvolutionPanel, {busy: false, command: () => {}, state: {
    plan: {id: "probe", sha256: "hash", baseline: "A", candidate: "B", order: []},
    frozen: true, blocked: true, local_enabled: true, anchor: "pricing",
    slots: [{job: "job", case: "fixture", arm: "a", status: "unknown", correct: null}],
    evaluation: {verdict: "inconclusive"}, observations: [{reason: "deadline"}],
    problems: [], hypotheses: [], decisions: [], rollback_available: false,
  }}));
  assert.match(view, /Данных недостаточно/);
  assert.match(view, /deadline/);
  assert.match(view, /заблокирован/);
  assert.doesNotMatch(view, /Подтвердить один шаг Local|Подготовить внедрение/);
});

test("a supported experiment displays distinct operator approval and rollback history", () => {
  const view = renderToStaticMarkup(createElement(EvolutionPanel, {busy: false, command: () => {}, state: {
    frozen: true, blocked: false, local_enabled: true, anchor: "pricing",
    slots: [], evaluation: {verdict: "supported", a: 1, b: 2, pairs: 2}, observations: [],
    problems: [], hypotheses: [], decisions: [], rollback_available: false,
    pending_decision: {id: "decision", sha256: "evidence", operation: "accept"},
  }}));
  assert.match(view, /A 1\/2, B 2\/2/);
  assert.match(view, /Внедрить · подтвердить/);
  assert.match(view, /История решений и откатов/);
});
