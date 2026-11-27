import { strict as assert } from "node:assert";
import { test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { StudyProgress, type StudyState } from "../src/Education.js";

test("no grades or graphs before any measured results", () => {
  const html = renderToStaticMarkup(createElement(StudyProgress, { groups: {} }));
  assert.match(html, /Без памяти/);
  assert.match(html, /С проверенной памятью/);
  assert.match(html, /С наставником/);
  assert.equal((html.match(/Оценённых задач пока нет/g) || []).length, 3);
  assert.doesNotMatch(html, /<meter|Решено 0/);
});

test("progress graphs render only the measured denominator in each arm", () => {
  const groups: StudyState["arms"] = { c: {
    attempts: {}, completed: 2, solved: 1, passed: 4, total: 6, hints: 2, retries: 0, tool_errors: null,
  } };
  const html = renderToStaticMarkup(createElement(StudyProgress, { groups }));
  assert.match(html, /Решено 1 из 2 оценённых задач/);
  assert.match(html, /Подсказок: 2/);
  assert.match(html, /<meter min="0" max="6" value="4"/);
});
