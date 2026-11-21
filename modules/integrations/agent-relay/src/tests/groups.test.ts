import { test } from "node:test";
import assert from "node:assert/strict";
const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");
const os = process.getBuiltinModule("node:os") as typeof import("node:os");
const path = process.getBuiltinModule("node:path") as typeof import("node:path");
import { Relay, RelayError } from "../core.ts";

import { tmpRelay } from "./helpers.ts";

function four() {
  const t = tmpRelay();
  const a = t.relay.register({ kind: "pi", pid: 100, cwd: "/a", id: "aaaa1", name: "planner" });
  const b = t.relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  const c = t.relay.register({ kind: "claude", pid: 300, cwd: "/c", id: "cccc3" });
  const d = t.relay.register({ kind: "agy", pid: 400, cwd: "/d", id: "dddd4" });
  return { ...t, a, b, c, d };
}

test("link creates a group visible from both sides", () => {
  const { relay, a, b } = four();
  const g = relay.link(a.id, "bbb");
  assert.deepEqual(g.members.sort(), [a.id, b.id].sort());
  assert.equal(relay.get(a.id)?.group, g.id);
  assert.equal(relay.get(b.id)?.group, g.id);
  assert.deepEqual(relay.members(b.id).map((s) => s.id).sort(), [a.id, b.id].sort());
});

test("link by name works and a third session joins the group", () => {
  const { relay, a, b, c } = four();
  const g = relay.link(b.id, "planner");
  relay.link(c.id, a.id);
  assert.deepEqual(relay.group(g.id)?.members.sort(), [a.id, b.id, c.id].sort());
});

test("linking members of two groups merges them", () => {
  const { relay, a, b, c, d } = four();
  relay.link(a.id, b.id);
  relay.link(c.id, d.id);
  const g = relay.link(b.id, c.id);
  assert.deepEqual(g.members.sort(), [a.id, b.id, c.id, d.id].sort());
  for (const s of [a, b, c, d]) assert.equal(relay.get(s.id)?.group, g.id);
});

test("unknown, empty and self targets are errors", () => {
  const { relay, a } = four();
  assert.throws(() => relay.link(a.id, "zzzzz"), RelayError);
  assert.throws(() => relay.link(a.id, ""), RelayError);
  assert.throws(() => relay.link(a.id, a.id), /no session matches/);
});

test("unlink leaves the group and a one-member group disappears", () => {
  const { relay, a, b } = four();
  const g = relay.link(a.id, b.id);
  relay.unlink(a.id);
  assert.equal(relay.get(a.id)?.group, undefined);
  assert.equal(relay.get(b.id)?.group, undefined);
  assert.equal(relay.group(g.id), undefined);
});

test("a dead member is removed from its group", () => {
  const { relay, alive, a, b, c } = four();
  const g = relay.link(a.id, b.id);
  relay.link(c.id, a.id);
  alive.delete(300);
  relay.sessions();
  assert.deepEqual(relay.group(g.id)?.members.sort(), [a.id, b.id].sort());
});

test("rejoin group after restart", () => {
  const { relay, alive } = tmpRelay();
  const a = relay.register({ kind: "claude", pid: 100, cwd: "/a", session: "c-sess-1" });
  const b = relay.register({ kind: "pi", pid: 200, cwd: "/b", session: "p-sess-2" });
  const g = relay.link(a.id, b.id);
  assert.equal(relay.get(a.id)?.group, g.id);
  assert.equal(relay.get(b.id)?.group, g.id);

  alive.delete(100);
  relay.sessions();
  assert.deepEqual(relay.members(b.id).map((s) => s.id), [b.id]);
  assert.equal(relay.group(g.id)?.id, g.id);

  alive.add(300);
  const a2 = relay.register({ kind: "claude", pid: 300, cwd: "/a", session: "c-sess-1" });
  assert.equal(a2.id, a.id);
  assert.equal(a2.group, g.id);
  assert.deepEqual(relay.members(a2.id).map((s) => s.id).sort(), [a.id, b.id].sort());
  assert.deepEqual(relay.members(b.id).map((s) => s.id).sort(), [a.id, b.id].sort());
});

test("group survives a wiped runtime root with pins kept", () => {
  const { relay, root, stateDir, alive } = tmpRelay();
  const a = relay.register({ kind: "claude", pid: 100, cwd: "/a", session: "c-sess-wiped" });
  const b = relay.register({ kind: "pi", pid: 200, cwd: "/b", session: "p-sess-wiped" });
  const g = relay.link(a.id, b.id);
  assert.equal(relay.get(a.id)?.group, g.id);

  for (const f of fs.readdirSync(root)) {
    if (f !== "state") fs.rmSync(path.join(root, f), { recursive: true, force: true });
  }

  const rebootRelay = new Relay({ root, stateDir, isAlive: (p) => alive.has(p), ancestorsOf: (p) => [p] });
  assert.equal(rebootRelay.sessions().length, 0);

  alive.add(300);
  alive.add(400);
  const a2 = rebootRelay.register({ kind: "claude", pid: 300, cwd: "/a", session: "c-sess-wiped" });
  const b2 = rebootRelay.register({ kind: "pi", pid: 400, cwd: "/b", session: "p-sess-wiped" });
  assert.equal(a2.id, a.id);
  assert.equal(b2.id, b.id);
  assert.equal(a2.group, g.id);
  assert.equal(b2.group, g.id);
  assert.deepEqual(rebootRelay.members(a2.id).map((s) => s.id).sort(), [a.id, b.id].sort());
});

test("stale group pruned", () => {
  const { relay, alive } = tmpRelay();
  const a = relay.register({ kind: "claude", pid: 100, cwd: "/a", session: "c-sess-stale" });
  const b = relay.register({ kind: "pi", pid: 200, cwd: "/b", session: "p-sess-stale" });
  const g = relay.link(a.id, b.id);
  alive.delete(100);
  alive.delete(200);
  relay.sessions();

  const thirtyOneDaysAgo = new Date(Date.now() - 31 * 86_400_000);
  fs.utimesSync(relay.pinPath("claude", "c-sess-stale"), thirtyOneDaysAgo, thirtyOneDaysAgo);
  fs.utimesSync(relay.pinPath("pi", "p-sess-stale"), thirtyOneDaysAgo, thirtyOneDaysAgo);

  relay.pruneGroups(30 * 86_400_000);
  assert.equal(relay.group(g.id), undefined);
});

test("migration of old groups dir", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "relay-legacy-"));
  const stateDir = path.join(root, "state");
  const legacyDir = path.join(root, "groups");
  fs.mkdirSync(legacyDir, { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(legacyDir, "g1f9.json"), JSON.stringify({ id: "g1f9", members: ["90387", "de97c"] }));

  const relay = new Relay({ root, stateDir });
  assert.ok(fs.existsSync(relay.groupPath("g1f9")));
  assert.deepEqual(relay.group("g1f9")?.members, ["90387", "de97c"]);
});

test("prune/unlink a migrated group stays gone across new Relay constructors", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "relay-migrated-"));
  const stateDir = path.join(root, "state");
  const legacyDir = path.join(root, "groups");
  fs.mkdirSync(legacyDir, { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(legacyDir, "g1f9.json"), JSON.stringify({ id: "g1f9", members: ["90387", "de97c"] }));

  const r1 = new Relay({ root, stateDir });
  assert.ok(fs.existsSync(r1.groupPath("g1f9")));
  assert.equal(fs.existsSync(path.join(legacyDir, "g1f9.json")), false);

  // Prune the migrated group (members are dead and have no pins)
  r1.pruneGroups(0);
  assert.equal(r1.group("g1f9"), undefined);

  // Construct a new Relay - group must not resurrect from legacyDir
  const r2 = new Relay({ root, stateDir });
  assert.equal(r2.group("g1f9"), undefined);
  assert.equal(fs.existsSync(r2.groupPath("g1f9")), false);
});
