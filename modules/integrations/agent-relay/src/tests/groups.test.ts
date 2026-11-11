import { test } from "node:test";
import assert from "node:assert/strict";
import { RelayError } from "../core.ts";
import { tmpRelay } from "./helpers.ts";

function four() {
  const t = tmpRelay();
  const a = t.relay.register({ kind: "pi", pid: 100, cwd: "/a", id: "aaaa1", name: "planner" });
  const b = t.relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  const c = t.relay.register({ kind: "opencode", pid: 300, cwd: "/c", id: "cccc3" });
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
