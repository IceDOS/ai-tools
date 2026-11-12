import { test } from "node:test";
import assert from "node:assert/strict";
import { runHook } from "../hook.ts";
import { tmpRelay } from "./helpers.ts";

test("claude-prompt marks human input on the agent's record, ignores relay prompts", () => {
  const { relay } = tmpRelay();
  const r = relay.register({ kind: "claude", pid: 200, cwd: "/b" });
  relay.markInjected(r.id, 5);
  runHook("claude-prompt", JSON.stringify({ prompt: "[relay from pi aaaa1, hop 2]\nhi" }), relay, 200, {});
  assert.equal(relay.get(r.id)!.hopReset, undefined);
  runHook("claude-prompt", JSON.stringify({ prompt: "<channel source=\"agent-relay\">[relay from pi aaaa1, hop 2]\nhi</channel>" }), relay, 200, {});
  assert.equal(relay.get(r.id)!.hopReset, undefined);
  runHook("claude-prompt", JSON.stringify({ prompt: "human prompt" }), relay, 200, {});
  const fresh = relay.get(r.id)!;
  assert.ok(fresh.hopReset! >= fresh.hopAt!);
});

test("claude-prompt without a record does nothing", () => {
  const { relay } = tmpRelay();
  assert.equal(runHook("claude-prompt", "{}", relay, 200, {}), "");
});

test("agy-session-start registers with AGENT_RELAY_ID and agent cwd", () => {
  const { relay } = tmpRelay();
  const env = { AGENT_RELAY_ID: "3f2a9" };
  assert.equal(runHook("agy-session-start", "{}", relay, 300, env, () => "/from/proc"), "{}");
  const rec = relay.findByPid([300])!;
  assert.equal(rec.kind, "agy");
  assert.equal(rec.id, "3f2a9");
  assert.equal(rec.cwd, "/from/proc");
});

test("agy-pre-invocation resets hops only for non-relay prompts", () => {
  const { relay } = tmpRelay();
  const r = relay.register({ kind: "agy", pid: 300, cwd: "/c" });
  relay.markInjected(r.id, 2);
  runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: "[relay from pi aaaa1, hop 2]\nx" }), relay, 300, {});
  assert.equal(relay.get(r.id)!.hopReset, undefined);
  runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: "do the thing" }), relay, 300, {});
  assert.ok(relay.get(r.id)!.hopReset);
});

test("agy-pre-invocation handles invalid JSON fallback", () => {
  const { relay } = tmpRelay();
  const r = relay.register({ kind: "agy", pid: 300, cwd: "/c" });
  relay.markInjected(r.id, 2);
  runHook("agy-pre-invocation", "plain non-json text", relay, 300, {});
  assert.ok(relay.get(r.id)!.hopReset);
});

test("agy-stop drains the inbox into a continue decision", () => {
  const { relay } = tmpRelay();
  const a = relay.register({ kind: "pi", pid: 100, cwd: "/a", id: "aaaa1" });
  const g = relay.register({ kind: "agy", pid: 300, cwd: "/c", id: "cccc3" });
  relay.link(a.id, g.id);
  relay.send(a.id, g.id, "check this", 10);
  const out = JSON.parse(runHook("agy-stop", "{}", relay, 300, {}));
  assert.equal(out.decision, "continue");
  assert.match(out.reason, /check this/);
  assert.equal(runHook("agy-stop", "{}", relay, 300, {}), "{}");
});

test("agy-pre-invocation tracks lastInput: repeated calls with same input reset only once", () => {
  // Each clock read advances 1s, so hop timestamps never collide within a millisecond.
  let t = Date.parse("2026-11-11T10:00:00Z");
  const { relay } = tmpRelay(undefined, () => new Date((t += 1000)));
  const r = relay.register({ kind: "agy", pid: 300, cwd: "/c" });

  runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: "first prompt" }), relay, 300, {});
  const reset1 = relay.get(r.id)!.hopReset;
  assert.ok(reset1);
  assert.equal(relay.get(r.id)!.agent?.lastInput, "first prompt");

  relay.markInjected(r.id, 3);

  runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: "first prompt" }), relay, 300, {});
  assert.equal(relay.get(r.id)!.hopReset, reset1);
  assert.ok(relay.get(r.id)!.hopAt! > relay.get(r.id)!.hopReset!);

  runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: "second prompt" }), relay, 300, {});
  const reset2 = relay.get(r.id)!.hopReset;
  assert.ok(reset2! > reset1!);
  assert.equal(relay.get(r.id)!.agent?.lastInput, "second prompt");
});

test("agy-session-start marks the session idle", () => {
  const { relay } = tmpRelay();
  runHook("agy-session-start", "{}", relay, 300, {}, () => "/c");
  assert.equal(relay.findByPid([300])!.agent?.busy, false);
});

test("agy-session-start keeps a busy flag the watcher already set", () => {
  const { relay } = tmpRelay();
  const r = relay.register({ kind: "agy", pid: 300, cwd: "/c", agent: { busy: true } });
  runHook("agy-session-start", "{}", relay, 300, {}, () => "/c");
  assert.equal(relay.get(r.id)!.agent?.busy, true);
});

test("agy-pre-invocation marks busy on every call, also for a repeated input", () => {
  const { relay } = tmpRelay();
  const r = relay.register({ kind: "agy", pid: 300, cwd: "/c", agent: { busy: false } });
  runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: "same" }), relay, 300, {});
  assert.equal(relay.get(r.id)!.agent?.busy, true);
  assert.equal(relay.get(r.id)!.agent?.lastInput, "same");
  relay.update(r.id, { agent: { ...relay.get(r.id)!.agent, busy: false } });
  runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: "same" }), relay, 300, {});
  assert.equal(relay.get(r.id)!.agent?.busy, true);
});

test("agy-stop with an empty inbox marks idle, with messages stays busy", () => {
  const { relay } = tmpRelay();
  const a = relay.register({ kind: "pi", pid: 100, cwd: "/a", id: "aaaa1" });
  const g = relay.register({ kind: "agy", pid: 300, cwd: "/c", id: "cccc3", agent: { busy: true, lastInput: "x" } });
  relay.link(a.id, g.id);
  relay.send(a.id, g.id, "hello", 10);
  runHook("agy-stop", "{}", relay, 300, {});
  assert.equal(relay.get(g.id)!.agent?.busy, true);
  assert.equal(runHook("agy-stop", "{}", relay, 300, {}), "{}");
  assert.equal(relay.get(g.id)!.agent?.busy, false);
  assert.equal(relay.get(g.id)!.agent?.lastInput, "x");
});
