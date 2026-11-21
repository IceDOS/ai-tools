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

test("pin reuse beats AGENT_RELAY_ID", () => {
  const { relay, alive } = tmpRelay();
  const r1 = relay.register({ kind: "claude", pid: 100, cwd: "/x", session: "sess-1" });
  assert.match(r1.id, /^[0-9a-f]{5}$/);
  alive.delete(100);
  relay.sessions();
  const r2 = relay.register({ kind: "claude", pid: 200, cwd: "/x", session: "sess-1", id: "aaaaa" });
  assert.equal(r2.id, r1.id);
  assert.notEqual(r2.id, "aaaaa");
});

test("pin ignored when id taken by live session", () => {
  const { relay } = tmpRelay();
  const r1 = relay.register({ kind: "claude", pid: 100, cwd: "/x", session: "sess-1" });
  relay.writePin("claude", "sess-2", r1.id);
  const r2 = relay.register({ kind: "claude", pid: 200, cwd: "/x", session: "sess-2" });
  assert.notEqual(r2.id, r1.id);
  assert.match(r2.id, /^[0-9a-f]{5}$/);
  assert.equal(relay.readPin("claude", "sess-2"), r2.id);
});

test("prunePins removes pins older than maxAgeMs", () => {
  const { relay } = tmpRelay();
  relay.writePin("pi", "old-sess", "11111");
  relay.writePin("pi", "new-sess", "22222");
  const p = relay.pinPath("pi", "old-sess");
  const tenDaysAgo = new Date(Date.now() - 10 * 86_400_000);
  fs.utimesSync(p, tenDaysAgo, tenDaysAgo);
  relay.prunePins(5 * 86_400_000);
  assert.equal(relay.readPin("pi", "old-sess"), undefined);
  assert.equal(relay.readPin("pi", "new-sess"), "22222");
});

test("non-pinned register with input.id rejects id in pin files", () => {
  const { relay } = tmpRelay();
  relay.writePin("claude", "sess-pinned", "11111");
  const r = relay.register({ kind: "pi", pid: 100, cwd: "/x", id: "11111" });
  assert.notEqual(r.id, "11111");
  assert.match(r.id, /^[0-9a-f]{5}$/);
});

test("non-pinned register with input.id rejects dead member id in group", () => {
  const { relay } = tmpRelay();
  const groupFile = relay.groupPath("g100");
  fs.writeFileSync(groupFile, JSON.stringify({ id: "g100", members: ["22222", "33333"] }));
  const r = relay.register({ kind: "pi", pid: 100, cwd: "/x", id: "22222" });
  assert.notEqual(r.id, "22222");
  assert.match(r.id, /^[0-9a-f]{5}$/);
  assert.equal(r.group, undefined);
});

test("hex loop avoids ids in pin files and group members", () => {
  const { relay } = tmpRelay();
  relay.writePin("claude", "sess-pinned", "11111");
  const groupFile = relay.groupPath("g100");
  fs.writeFileSync(groupFile, JSON.stringify({ id: "g100", members: ["22222", "33333"] }));
  for (let i = 0; i < 10; i++) {
    const r = relay.register({ kind: "pi", pid: 100 + i, cwd: "/x" });
    assert.notEqual(r.id, "11111");
    assert.notEqual(r.id, "22222");
    assert.notEqual(r.id, "33333");
  }
});

test("pinned reuse of own id stays allowed even when in pin store and group", () => {
  const { relay, alive } = tmpRelay();
  const a = relay.register({ kind: "claude", pid: 100, cwd: "/a", session: "c-sess-own" });
  const b = relay.register({ kind: "pi", pid: 200, cwd: "/b", session: "p-sess-own" });
  const g = relay.link(a.id, b.id);
  alive.delete(100);
  relay.sessions();
  const a2 = relay.register({ kind: "claude", pid: 300, cwd: "/a", session: "c-sess-own" });
  assert.equal(a2.id, a.id);
  assert.equal(a2.group, g.id);
});

test("same-process session switch takes back the resumed session's pinned ID and group (pi /resume)", () => {
  const { relay } = tmpRelay();
  const old = relay.register({ kind: "pi", pid: 100, cwd: "/a", session: "s-old" });
  const peer = relay.register({ kind: "claude", pid: 200, cwd: "/b" });
  relay.link(old.id, peer.id);
  const gid = relay.get(old.id)!.group!;
  relay.unregister(old.id);
  const fresh = relay.register({ kind: "pi", pid: 100, cwd: "/a", session: "s-new" });
  const back = relay.register({ kind: "pi", pid: 100, cwd: "/a", session: "s-old" });
  assert.equal(back.id, old.id);
  assert.equal(back.group, gid);
  assert.equal(relay.get(fresh.id), undefined);
  assert.equal(relay.readPin("pi", "s-new"), fresh.id);
  assert.equal(relay.readPin("pi", "s-old"), old.id);
});

test("session switch leaves a pin held by a live session alone", () => {
  const { relay } = tmpRelay();
  const a = relay.register({ kind: "pi", pid: 100, cwd: "/a", session: "s1" });
  const b = relay.register({ kind: "pi", pid: 200, cwd: "/b", session: "s2" });
  const r = relay.register({ kind: "pi", pid: 200, cwd: "/b", session: "s1" });
  assert.equal(r.id, b.id);
  assert.equal(r.session, "s1");
  assert.equal(relay.readPin("pi", "s1"), a.id);
});

test("opencode root sessions resume their pinned ID per scope", () => {
  const { relay } = tmpRelay();
  const old = relay.register({ kind: "opencode", pid: 100, cwd: "/a", scope: "r-old", session: "r-old" });
  relay.unregister(old.id);
  relay.register({ kind: "opencode", pid: 100, cwd: "/a", scope: "r-new", session: "r-new" });
  const back = relay.register({ kind: "opencode", pid: 100, cwd: "/a", scope: "r-old", session: "r-old" });
  assert.equal(back.id, old.id);
});
