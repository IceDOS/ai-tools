// Run: node --experimental-transform-types --test tests/rlm-children.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { stripTerminalSequences, visibleWidth } from "@earendil-works/pi-tui";
import { lib } from "../extensions/rlm-children.ts";

const theme = { fg: (_color: string, text: string) => text } as never;

function child(prompt: string) {
  return { seq: 1, depth: 1, prompt, startedAt: 0, turns: 0, tokens: 0, cost: 0, streaming: "", entries: [] } as never;
}

const row = (prompt: string) => ` ${lib.childRow(child(prompt), theme, 1000)}`;

test("a 2000-char prompt still takes exactly one widget line at width 80", () => {
  const long = row("word ".repeat(400));
  assert.ok(long.length > 2000, `row is ${long.length} chars`);
  const lines = new lib.WidgetRows([long]).render(80);
  assert.equal(lines.length, 1);
  assert.equal(visibleWidth(lines[0]), 80);
  assert.ok(stripTerminalSequences(lines[0]).trimEnd().endsWith("…"));
});

test("the widget keeps one line per child at any width", () => {
  const rows = ["rlm 2 running · 0 finished", row("a".repeat(500)), row("b\nc".repeat(300))];
  for (const width of [20, 80, 200]) {
    const lines = new lib.WidgetRows(rows).render(width);
    assert.equal(lines.length, rows.length);
    for (const line of lines) assert.equal(visibleWidth(line), width);
  }
});

test("child rows flatten whitespace so a prompt cannot start a new line", () => {
  assert.ok(!lib.childRow(child("a\nb\tc"), theme, 1000).includes("\n"));
});
