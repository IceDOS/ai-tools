import { test } from "node:test";
import assert from "node:assert/strict";
import { ancestors, readConfig } from "../core.ts";
import { tmpRelay } from "./helpers.ts";

const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");
const os = process.getBuiltinModule("node:os") as typeof import("node:os");
const path = process.getBuiltinModule("node:path") as typeof import("node:path");

test("register assigns a 5-hex id and writes the record", () => {
  const { relay, root } = tmpRelay();
  const r = relay.register({ kind: "pi", pid: 100, cwd: "/x" });
  assert.match(r.id, /^[0-9a-f]{5}$/);
  assert.ok(fs.existsSync(path.join(root, "sessions", `${r.id}.json`)));
  assert.ok(fs.existsSync(path.join(root, "inbox", r.id)));
});

test("register uses a valid free preferred id", () => {
  const { relay } = tmpRelay();
  assert.equal(relay.register({ kind: "pi", pid: 100, cwd: "/x", id: "3f2a9" }).id, "3f2a9");
});

test("register ignores a taken or invalid preferred id", () => {
  const { relay } = tmpRelay();
  relay.register({ kind: "pi", pid: 100, cwd: "/x", id: "3f2a9" });
  assert.notEqual(relay.register({ kind: "claude", pid: 200, cwd: "/x", id: "3f2a9" }).id, "3f2a9");
  assert.match(relay.register({ kind: "agy", pid: 300, cwd: "/x", id: "NOPE" }).id, /^[0-9a-f]{5}$/);
});

test("register is idempotent per pid and kind and merges agent fields", () => {
  const { relay } = tmpRelay();
  const a = relay.register({ kind: "agy", pid: 100, cwd: "/x", agent: { lsAddress: "1" } });
  const b = relay.register({ kind: "agy", pid: 100, cwd: "/y", agent: { csrf: "2" } });
  assert.equal(a.id, b.id);
  assert.deepEqual(relay.get(a.id)?.agent, { lsAddress: "1", csrf: "2" });
  assert.equal(relay.sessions().length, 1);
});

test("dead sessions are pruned with their inbox", () => {
  const { relay, alive, root } = tmpRelay();
  const r = relay.register({ kind: "pi", pid: 100, cwd: "/x" });
  alive.delete(100);
  assert.equal(relay.sessions().length, 0);
  assert.ok(!fs.existsSync(path.join(root, "inbox", r.id)));
});

test("findByPid returns the first ancestor with a record", () => {
  const { relay } = tmpRelay();
  const r = relay.register({ kind: "claude", pid: 200, cwd: "/x" });
  assert.equal(relay.findByPid([999, 200, 1])?.id, r.id);
  assert.equal(relay.findByPid([999]), undefined);
});

test("ancestors starts at the given pid and includes the parent", () => {
  const chain = ancestors(process.pid);
  assert.equal(chain[0], process.pid);
  assert.equal(chain[1], process.ppid);
});

test("readConfig defaults hopLimit to 10 and reads the config file", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "relay-cfg-"));
  assert.equal(readConfig({ XDG_CONFIG_HOME: dir }).hopLimit, 10);
  fs.mkdirSync(path.join(dir, "agent-relay"));
  fs.writeFileSync(path.join(dir, "agent-relay", "config.json"), JSON.stringify({ hopLimit: 3 }));
  assert.equal(readConfig({ XDG_CONFIG_HOME: dir }).hopLimit, 3);
});
