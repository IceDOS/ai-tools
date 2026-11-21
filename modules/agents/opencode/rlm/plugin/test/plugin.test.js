import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { getDataDir, loadHarness, renderEntries, buildHarnessSection, rlmChildrenArgs } from "../index.js";

test("getDataDir respects PLUGIN_DATA, XDG_DATA_HOME, and default fallback", () => {
  const origPluginData = process.env.PLUGIN_DATA;
  const origXdg = process.env.XDG_DATA_HOME;
  try {
    process.env.PLUGIN_DATA = "/custom/plugin/data";
    assert.equal(getDataDir(), "/custom/plugin/data");
    delete process.env.PLUGIN_DATA;

    process.env.XDG_DATA_HOME = "/custom/xdg";
    assert.equal(getDataDir(), "/custom/xdg/icedos-rlm");
    delete process.env.XDG_DATA_HOME;

    assert.equal(getDataDir(), path.join(os.homedir(), ".local/share", "icedos-rlm"));
  } finally {
    if (origPluginData !== undefined) process.env.PLUGIN_DATA = origPluginData;
    else delete process.env.PLUGIN_DATA;
    if (origXdg !== undefined) process.env.XDG_DATA_HOME = origXdg;
    else delete process.env.XDG_DATA_HOME;
  }
});

test("loadHarness parses and dedupes jsonl entries", (t) => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "rlm-test-"));
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));
  const file = path.join(tmp, "harness.jsonl");
  const lines = [
    JSON.stringify({ id: "1", title: "note 1", content: "old", kind: "memory" }),
    JSON.stringify({ id: "1", title: "note 1 updated", content: "new", kind: "memory" }),
    JSON.stringify({ id: "2", title: "note 2", content: "deleted", kind: "prompt", deleted: true }),
    JSON.stringify({ id: "3", title: "note 3", content: "policy", kind: "prompt" }),
    "invalid-json",
  ];
  fs.writeFileSync(file, lines.join("\n"));

  const loaded = loadHarness(file);
  assert.equal(loaded.length, 2);
  assert.equal(loaded[0].id, "1");
  assert.equal(loaded[0].content, "new");
  assert.equal(loaded[1].id, "3");
});

test("renderEntries formats and truncates long content", () => {
  const entries = [
    { id: "1", title: "Short", content: "hello" },
    { id: "2", title: "Long", content: "a".repeat(600) },
  ];
  const rendered = renderEntries(entries);
  assert.match(rendered, /- \[1\] Short — hello/);
  assert.match(rendered, /- \[2\] Long — a{500}…/);
});

test("buildHarnessSection generates markdown sections", () => {
  assert.equal(buildHarnessSection([], []), "");
  const memories = [{ id: "m1", title: "Mem", content: "remember", kind: "memory" }];
  const prompts = [{ id: "p1", title: "Pol", content: "rule", kind: "prompt" }];
  const section = buildHarnessSection(memories, prompts);
  assert.match(section, /## Continual harness \(rlm plugin\)/);
  assert.match(section, /### Memories/);
  assert.match(section, /### Policies/);
});

test("rlmChildrenArgs builds CLI args for listing or child transcript", () => {
  assert.deepEqual(rlmChildrenArgs("ses_123", ""), ["children", "--session", "ses_123"]);
  assert.deepEqual(rlmChildrenArgs("ses_123", "   "), ["children", "--session", "ses_123"]);
  assert.deepEqual(rlmChildrenArgs("ses_123", null), ["children", "--session", "ses_123"]);
  assert.deepEqual(rlmChildrenArgs("ses_123", "c1"), ["children", "--session", "ses_123", "c1"]);
  assert.deepEqual(rlmChildrenArgs("ses_123", "#2"), ["children", "--session", "ses_123", "#2"]);
  assert.deepEqual(rlmChildrenArgs("ses_123", "  2d965733  "), ["children", "--session", "ses_123", "2d965733"]);
});
