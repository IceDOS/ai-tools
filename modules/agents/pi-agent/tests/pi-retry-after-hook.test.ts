import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

const patcher = new URL("../patches/pi-retry-after-hook.mjs", import.meta.url).pathname;
const CALL = "this._replaceMessageInPlace(event.message, normalized);";

function dist(files: Record<string, string>) {
  const dir = mkdtempSync(join(tmpdir(), "pi-patch-"));
  for (const [name, body] of Object.entries(files)) {
    const path = join(dir, name);
    mkdirSync(join(path, ".."), { recursive: true });
    writeFileSync(path, body);
  }
  return dir;
}

const run = (dir: string) => spawnSync("node", [patcher, dir], { encoding: "utf8" });

test("patches the bundle copy and the ESM core, and is idempotent", () => {
  const dir = dist({ "bundle/chunks/c.js": `if (r) { ${CALL} }`, "core/agent-session.js": `${CALL}\n` });
  try {
    assert.equal(run(dir).status, 0);
    for (const file of ["bundle/chunks/c.js", "core/agent-session.js"]) {
      const text = readFileSync(join(dir, file), "utf8");
      assert.ok(text.includes("this._retryAttempt = 0;"), file);
      assert.ok(text.includes("patched:"), file);
    }
    assert.equal(run(dir).status, 0);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("fails when nothing matches", () => {
  const dir = dist({ "bundle/chunks/c.js": "nothing here" });
  try {
    assert.equal(run(dir).status, 1);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("fails when only the ESM core matches", () => {
  const dir = dist({ "core/agent-session.js": `${CALL}\n` });
  try {
    const result = run(dir);
    assert.equal(result.status, 1);
    assert.match(result.stderr, /no bundle copy/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("fails when one file holds two replacements", () => {
  const dir = dist({ "bundle/chunks/c.js": `${CALL} ${CALL}` });
  try {
    const result = run(dir);
    assert.equal(result.status, 1);
    assert.match(result.stderr, /2 message_end replacements/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
