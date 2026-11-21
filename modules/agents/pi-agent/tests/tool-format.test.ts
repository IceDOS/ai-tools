import { test } from "node:test";
import assert from "node:assert/strict";
import { formatToolDuration, shortTool, composeToolTimer } from "../extensions/tool-format.ts";

test("formatToolDuration formats ms identical to pi tool rows", () => {
  assert.equal(formatToolDuration(0), "0.0s");
  assert.equal(formatToolDuration(4200), "4.2s");
  assert.equal(formatToolDuration(59999), "60.0s");
  assert.equal(formatToolDuration(60000), "1m 0s");
  assert.equal(formatToolDuration(90500), "1m 30s");
  assert.equal(formatToolDuration(3725000), "1h 2m 5s");
  assert.equal(formatToolDuration(-500), "0.0s");
});

test("shortTool strips mcp server prefixes", () => {
  assert.equal(shortTool("mcp__server__my_tool"), "my_tool");
  assert.equal(shortTool("bash"), "bash");
});

test("composeToolTimer formats tool name and duration suffix", () => {
  assert.equal(composeToolTimer("bash", 4200), "⏱ bash 4.2s");
  assert.equal(composeToolTimer("mcp__fs__read_file", 65000), "⏱ read_file 1m 5s");
});
