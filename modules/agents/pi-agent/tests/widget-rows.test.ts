// Run: node --experimental-transform-types --test tests/widget-rows.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { stripTerminalSequences, visibleWidth } from "@earendil-works/pi-tui";
import { WidgetRows } from "../extensions/widget-rows.ts";

const costRow = " sonnet-4.5  202k/1M  ████░░░░░░  21%  resets 3:12  $0.412";

test("a cost row wider than 40 cols still renders as one line at width 40", () => {
  assert.ok(costRow.length > 40, `row is ${costRow.length} cols`);
  const lines = new WidgetRows([costRow]).render(40);
  assert.equal(lines.length, 1);
  assert.equal(visibleWidth(lines[0]), 40);
  assert.ok(stripTerminalSequences(lines[0]).trimEnd().endsWith("…"));
});

test("many over-wide rows keep one line each", () => {
  const rows = ["a".repeat(400), "rlm 3 running · 12 finished", "b".repeat(120)];
  for (const width of [20, 40, 80]) {
    const lines = new WidgetRows(rows).render(width);
    assert.equal(lines.length, rows.length);
    for (const line of lines) assert.equal(visibleWidth(line), width);
  }
});

test("no rows renders nothing and a zero width still yields one line", () => {
  assert.deepEqual(new WidgetRows([]).render(40), []);
  assert.equal(new WidgetRows(["abc"]).render(0).length, 1);
});
