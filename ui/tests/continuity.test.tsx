import { strict as assert } from "node:assert";
import { test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Continuity } from "../src/Continuity.js";

test("Continuity offers a package and waits for a verified result", () => {
  const html = renderToStaticMarkup(createElement(Continuity));
  assert.match(html, /Создать пакет для нового чата/);
  assert.doesNotMatch(html, /Проверена при формировании/);
});
