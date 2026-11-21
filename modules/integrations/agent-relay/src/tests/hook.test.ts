import { test } from "node:test";
import assert from "node:assert/strict";
import { CONTINUE_HINT, runHook } from "../hook.ts";
import { Relay } from "../core.ts";
import { tmpRelay } from "./helpers.ts";

const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");
const path = process.getBuiltinModule("node:path") as typeof import("node:path");

const inbox = (root: string, id: string) =>
  fs.existsSync(path.join(root, "inbox", id)) ? fs.readdirSync(path.join(root, "inbox", id)).filter((f) => f.endsWith(".json")) : [];

// The hop of a fresh send is what a human prompt resets; the record no longer keeps it.
let note = 0;
function sendHop(relay: Relay, from: string, to: string): number {
  relay.send(from, to, `note ${++note}`, 10); // unique text: identical sends are deduped
  return relay.pending(to).at(-1)!.hop;
}

function claudeAndPi() {
  const t = tmpRelay();
  const a = t.relay.register({ kind: "pi", pid: 100, cwd: "/a", id: "aaaa1" });
  const c = t.relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  t.relay.link(a.id, c.id);
  return { ...t, a, c };
}

test("claude-prompt marks human input, ignores relay prompts", () => {
  const { relay, a, c } = claudeAndPi();
  assert.equal(sendHop(relay, c.id, a.id), 1);
  assert.equal(sendHop(relay, c.id, a.id), 2);
  runHook("claude-prompt", JSON.stringify({ prompt: "[relay from pi aaaa1, hop 2]\nhi" }), relay, 200, {});
  assert.equal(sendHop(relay, c.id, a.id), 3);
  runHook("claude-prompt", JSON.stringify({ prompt: '<channel source="agent-relay">[relay from pi aaaa1, hop 2]\nhi</channel>' }), relay, 200, {});
  assert.equal(sendHop(relay, c.id, a.id), 4);
  runHook("claude-prompt", JSON.stringify({ prompt: "human prompt" }), relay, 200, {});
  assert.equal(sendHop(relay, c.id, a.id), 1);
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
  assert.equal(rec.agent?.busy, false);
});

test("agy-session-start drops a busy flag left by a turn without its Stop", () => {
  const { relay } = tmpRelay();
  const r = relay.register({ kind: "agy", pid: 300, cwd: "/c", agent: { busy: true, busyAt: 1, pendingIds: ["m1"] } });
  runHook("agy-session-start", "{}", relay, 300, {}, () => "/c");
  const agent = relay.get(r.id)!.agent!;
  assert.equal(agent.busy, false);
  assert.ok(typeof agent.busyAt === "number" && agent.busyAt > 1);
  assert.deepEqual(agent.pendingIds, []);
});

test("agy-pre-invocation resets the pair only for non-relay prompts", () => {
  const { relay } = tmpRelay();
  const a = relay.register({ kind: "pi", pid: 100, cwd: "/a", id: "aaaa1" });
  const g = relay.register({ kind: "agy", pid: 300, cwd: "/c", id: "cccc3" });
  relay.link(a.id, g.id);
  assert.equal(sendHop(relay, g.id, a.id), 1);
  runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: "[relay from pi aaaa1, hop 2]\nx" }), relay, 300, {});
  assert.equal(sendHop(relay, g.id, a.id), 2);
  // Mid-turn batch header and a reason agy wrapped: the relay line sits inside the text, not in front.
  runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: "[relay from agent-relay, batch of 2 while you were busy, newest last]\n\n[relay from pi aaaa1, hop 2]\nx" }), relay, 300, {});
  assert.equal(sendHop(relay, g.id, a.id), 3);
  runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: "Agent continued after a stop:\n\n[relay from pi aaaa1, hop 3]\ny" }), relay, 300, {});
  assert.equal(sendHop(relay, g.id, a.id), 4);
  runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: "do the thing" }), relay, 300, {});
  assert.equal(sendHop(relay, g.id, a.id), 1);
});

test("agy-pre-invocation handles invalid JSON fallback", () => {
  const { relay } = tmpRelay();
  const a = relay.register({ kind: "pi", pid: 100, cwd: "/a", id: "aaaa1" });
  const g = relay.register({ kind: "agy", pid: 300, cwd: "/c", id: "cccc3" });
  relay.link(a.id, g.id);
  assert.equal(sendHop(relay, g.id, a.id), 1);
  runHook("agy-pre-invocation", "plain non-json text", relay, 300, {});
  assert.equal(sendHop(relay, g.id, a.id), 1);
  assert.equal(relay.get(g.id)!.agent?.lastInput, "plain non-json text");
});

test("agy-pre-invocation marks busy on every call, also for a repeated input", () => {
  const { relay } = tmpRelay();
  const r = relay.register({ kind: "agy", pid: 300, cwd: "/c", agent: { busy: false } });
  runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: "same" }), relay, 300, {});
  assert.equal(relay.get(r.id)!.agent?.busy, true);
  assert.equal(relay.get(r.id)!.agent?.lastInput, "same");
  const first = relay.get(r.id)!.agent?.busyAt;
  relay.update(r.id, { agent: { ...relay.get(r.id)!.agent, busy: false } });
  runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: "same" }), relay, 300, {});
  assert.equal(relay.get(r.id)!.agent?.busy, true);
  assert.ok((relay.get(r.id)!.agent?.busyAt as number) >= (first as number));
});

test("agy-stop asks for a continue and keeps the messages for the ack", () => {
  const { relay, root } = tmpRelay();
  const a = relay.register({ kind: "pi", pid: 100, cwd: "/a", id: "aaaa1" });
  const g = relay.register({ kind: "agy", pid: 300, cwd: "/c", id: "cccc3", agent: { busy: true } });
  relay.link(a.id, g.id);
  relay.send(a.id, g.id, "check this", 10);
  const out = JSON.parse(runHook("agy-stop", "{}", relay, 300, {}));
  assert.equal(out.decision, "continue");
  assert.match(out.reason, /check this/);
  assert.equal(relay.get(g.id)!.agent?.busy, false);
  assert.equal(inbox(root, g.id).length, 1);
  assert.equal((relay.get(g.id)!.agent?.pendingIds as string[]).length, 1);
  // agy may ignore the continue: the same messages are handed over again, not lost.
  const again = JSON.parse(runHook("agy-stop", "{}", relay, 300, {}));
  assert.equal(again.decision, "continue");
  assert.equal(again.reason, out.reason);
  assert.equal(inbox(root, g.id).length, 1);
});

test("agy-pre-invocation acks a continue: the messages leave the queue and the ledger says delivered", () => {
  const { relay, root } = tmpRelay();
  const a = relay.register({ kind: "pi", pid: 100, cwd: "/a", id: "aaaa1" });
  const g = relay.register({ kind: "agy", pid: 300, cwd: "/c", id: "cccc3" });
  relay.link(a.id, g.id);
  relay.send(a.id, g.id, "ack me", 10);
  runHook("agy-stop", "{}", relay, 300, {});
  runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: "ack me" }), relay, 300, {});
  assert.equal(inbox(root, g.id).length, 0);
  assert.deepEqual(relay.get(g.id)!.agent?.pendingIds, []);
  assert.deepEqual(relay.status(a.id).map((r) => r.state), ["delivered"]);
  // The next Stop ends the turn that saw them, so they are read now.
  assert.equal(runHook("agy-stop", "{}", relay, 300, {}), "{}");
  assert.deepEqual(relay.status(a.id).map((r) => r.state), ["read"]);
  assert.deepEqual(relay.get(g.id)!.agent?.awaitingRead, []);
});

test("agy-stop leaves the batch to a live watcher instead of continuing the turn", () => {
  const { relay, root, a } = claudeAndPi();
  const g = relay.register({ kind: "agy", pid: 300, cwd: "/c", id: "cccc3", agent: { busy: true, watcher: process.pid } });
  relay.link(a.id, g.id);
  relay.send(a.id, g.id, "deliver me as a prompt", 10);
  assert.equal(runHook("agy-stop", "{}", relay, 300, {}), "{}");
  const agent = relay.get(g.id)!.agent!;
  assert.equal(agent.busy, false);
  assert.deepEqual(agent.pendingIds, []);
  assert.equal(inbox(root, g.id).length, 1); // the watcher pastes it, so the file stays
  assert.equal(relay.status(a.id)[0].state, "queued");
});

test("agy-stop without a watcher asks agy to continue, with an instruction line first", () => {
  const { relay, a } = claudeAndPi();
  const g = relay.register({ kind: "agy", pid: 300, cwd: "/c", id: "cccc3", agent: { busy: true } });
  relay.link(a.id, g.id);
  relay.send(a.id, g.id, "answer me", 10);
  const out = JSON.parse(runHook("agy-stop", "{}", relay, 300, {}));
  assert.equal(out.decision, "continue");
  assert.ok(out.reason.startsWith(CONTINUE_HINT + "\n\n"), out.reason);
  assert.ok(out.reason.includes("[relay from pi aaaa1"));
  assert.equal(relay.get(g.id)!.agent?.pendingIds?.length, 1);
});

test("a dead watcher pid falls back to the continue path", () => {
  const { relay, root, a } = claudeAndPi();
  const g = relay.register({ kind: "agy", pid: 300, cwd: "/c", id: "cccc3", agent: { busy: true, watcher: 999999 } });
  relay.link(a.id, g.id);
  relay.send(a.id, g.id, "nobody is watching", 10);
  const out = JSON.parse(runHook("agy-stop", "{}", relay, 300, {}));
  assert.equal(out.decision, "continue");
  assert.ok(out.reason.startsWith(CONTINUE_HINT));
  assert.equal(inbox(root, g.id).length, 1);
});

test("agy-stop with an empty inbox marks idle and clears a stale continue", () => {
  const { relay } = tmpRelay();
  const r = relay.register({ kind: "agy", pid: 300, cwd: "/c", agent: { busy: true, lastInput: "x", pendingIds: ["gone"] } });
  assert.equal(runHook("agy-stop", "{}", relay, 300, {}), "{}");
  const agent = relay.get(r.id)!.agent!;
  assert.equal(agent.busy, false);
  assert.equal(agent.lastInput, "x");
  assert.deepEqual(agent.pendingIds, []);
});

test("agy-pre-invocation injects mail that arrived during a tool call into the next model step", () => {
  const { relay, root, a } = claudeAndPi();
  const g = relay.register({ kind: "agy", pid: 300, cwd: "/c", id: "cccc3", agent: { busy: true } });
  relay.link(a.id, g.id);
  runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: "build it", invocationNum: 0 }), relay, 300, {});
  relay.send(a.id, g.id, "stop, wrong branch", 10);
  const out = JSON.parse(runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: "build it", invocationNum: 1 }), relay, 300, {}));
  const text = out.injectSteps[0].systemMessage.systemMessage;
  assert.deepEqual(Object.keys(out.injectSteps[0].systemMessage), ["systemMessage"], "agy drops a systemMessage with metadata");
  assert.ok(text.startsWith(CONTINUE_HINT + "\n\n"), text);
  assert.match(text, /stop, wrong branch/);
  assert.equal(inbox(root, g.id).length, 0);
  assert.deepEqual(relay.status(a.id).map((r) => r.state), ["delivered"]);
  assert.equal(runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: "build it", invocationNum: 2 }), relay, 300, {}), "{}", "injected once");
  runHook("agy-stop", "{}", relay, 300, {});
  assert.deepEqual(relay.status(a.id).map((r) => r.state), ["read"]);
});

test("agy-pre-invocation claims a batch the watcher just pasted without injecting it again", () => {
  const { relay, a } = claudeAndPi();
  const g = relay.register({ kind: "agy", pid: 300, cwd: "/c", id: "cccc3", agent: { busy: false } });
  relay.link(a.id, g.id);
  relay.send(a.id, g.id, "pasted", 10);
  const m = relay.pending(g.id)[0];
  const pasted = `[relay from pi aaaa1, hop 1, msg ${m.short} 10:00:00]\npasted`;
  assert.equal(runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: pasted, invocationNum: 0 }), relay, 300, {}), "{}");
  assert.deepEqual(relay.status(a.id).map((r) => r.state), ["delivered"]);
  assert.equal(relay.delivered(m), false, "the watcher's late claim is a no-op");
});

test("a hook working from a stale record keeps the watcher field written meanwhile", () => {
  const { relay } = tmpRelay();
  const g = relay.register({ kind: "agy", pid: 300, cwd: "/c", id: "cccc3", agent: { busy: false } });
  // The watcher registers between the hook's read and its write.
  const stale = new Proxy(relay, {
    get(target: any, key: string | symbol) {
      if (key === "findByPid") return (...a: any[]) => { const r = target.findByPid(...a); target.updateAgent(g.id, { watcher: 4242 }); return r; };
      const v = target[key];
      return typeof v === "function" ? v.bind(target) : v;
    },
  });
  runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: "hi" }), stale, 300, {});
  assert.equal(relay.get(g.id)!.agent?.watcher, 4242);
  assert.equal(relay.get(g.id)!.agent?.busy, true);
  runHook("agy-stop", "{}", stale, 300, {});
  assert.equal(relay.get(g.id)!.agent?.watcher, 4242);
  assert.equal(relay.get(g.id)!.agent?.busy, false);
});

test("claude-prompt ignores a pi record further up the process chain", () => {
  const { relay } = tmpRelay(new Set([process.pid, 200]));
  // This test process stands in for a pi session that launched claude from its bash tool.
  const p = relay.register({ kind: "pi", pid: process.pid, cwd: "/a", id: "aaaa1" });
  const c = relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  relay.link(p.id, c.id);
  sendHop(relay, p.id, c.id);
  assert.equal(sendHop(relay, p.id, c.id), 2);
  runHook("claude-prompt", JSON.stringify({ prompt: "human" }), relay, process.pid, {});
  assert.equal(sendHop(relay, p.id, c.id), 3, "a claude hook must not reset the pi session's pairs");
});

test("agy-session-start and agy-pre-invocation pin conversationId", () => {
  const { relay } = tmpRelay();
  const convId = "conv-session-start-test";
  runHook("agy-session-start", JSON.stringify({ conversationId: convId }), relay, 300, {}, () => "/c");
  const rec = relay.findByPid([300], ["agy"]);
  assert.ok(rec);
  assert.equal(relay.readPin("agy", convId), rec.id);

  // Running agy-pre-invocation with same conversationId keeps the pin
  runHook("agy-pre-invocation", JSON.stringify({ conversationId: convId, lastUserInput: "test" }), relay, 300, {});
  assert.equal(relay.readPin("agy", convId), rec.id);
});

test("hook does not clobber a different pin", () => {
  const { relay } = tmpRelay();
  const convId = "conv-protected-pin";
  relay.writePin("agy", convId, "11111");

  relay.register({ kind: "agy", pid: 300, cwd: "/c", id: "22222" });
  const rec = relay.findByPid([300], ["agy"])!;
  assert.equal(rec.id, "22222");

  runHook("agy-session-start", JSON.stringify({ conversationId: convId }), relay, 300, {}, () => "/c");
  assert.equal(relay.readPin("agy", convId), "11111");

  runHook("agy-pre-invocation", JSON.stringify({ conversationId: convId, lastUserInput: "hi" }), relay, 300, {});
  assert.equal(relay.readPin("agy", convId), "11111");
});

test("agy-pre-invocation fallback registers when no prior record existed", () => {
  const { relay } = tmpRelay();
  const convId = "conv-fallback-test";
  relay.writePin("agy", convId, "55555");
  assert.equal(relay.findByPid([300], ["agy"]), undefined);
  runHook("agy-pre-invocation", JSON.stringify({ conversationId: convId, lastUserInput: "first prompt" }), relay, 300, {}, () => "/c");
  const rec = relay.findByPid([300], ["agy"]);
  assert.ok(rec);
  assert.equal(rec.id, "55555");
});

test("agy-pre-invocation skips registration and pin write when already registered", () => {
  const { relay } = tmpRelay();
  const convId = "conv-skip-test";
  runHook("agy-session-start", JSON.stringify({ conversationId: convId }), relay, 300, {}, () => "/c");
  const rec = relay.findByPid([300], ["agy"]);
  assert.ok(rec);
  assert.equal(rec.session, convId);

  let registerCalls = 0;
  let pinWrites = 0;
  const origRegister = relay.register.bind(relay);
  const origWritePin = relay.writePin.bind(relay);
  relay.register = (input) => {
    registerCalls++;
    return origRegister(input);
  };
  relay.writePin = (kind, key, id) => {
    pinWrites++;
    return origWritePin(kind, key, id);
  };

  runHook("agy-pre-invocation", JSON.stringify({ conversationId: convId, lastUserInput: "step 1" }), relay, 300, {});
  assert.equal(registerCalls, 0, "should not call register when session already matches");
  assert.equal(pinWrites, 0, "should not rewrite pin when session already matches");
});

test("claude /resume takes back the resumed conversation's ID and group", () => {
  const { relay } = tmpRelay();
  const old = relay.register({ kind: "claude", pid: 200, cwd: "/b", session: "conv-old" });
  const peer = relay.register({ kind: "pi", pid: 100, cwd: "/a" });
  relay.link(old.id, peer.id);
  const gid = relay.get(old.id)!.group!;
  relay.unregister(old.id); // claude killed
  const fresh = relay.register({ kind: "claude", pid: 200, cwd: "/b", session: "conv-new" });
  assert.notEqual(fresh.id, old.id);
  const note = runHook("claude-session-start", JSON.stringify({ session_id: "conv-old", source: "resume" }), relay, 200, {});
  const rec = relay.findByPid([200], ["claude"])!;
  assert.equal(rec.id, old.id);
  assert.equal(rec.group, gid);
  assert.equal(relay.get(fresh.id), undefined);
  assert.match(note, new RegExp(old.id));
  // The ID it left stays with the new conversation.
  assert.equal(relay.readPin("claude", "conv-new"), fresh.id);
  // Same session again is a no-op.
  assert.equal(runHook("claude-prompt", JSON.stringify({ prompt: "hi", session_id: "conv-old" }), relay, 200, {}), "");
});

test("agy-pre-invocation follows a conversation switch to its pinned ID", () => {
  const { relay } = tmpRelay();
  const old = relay.register({ kind: "agy", pid: 300, cwd: "/c", session: "c-old" });
  relay.unregister(old.id);
  runHook("agy-session-start", JSON.stringify({ conversationId: "c-new" }), relay, 300, {}, () => "/c");
  const fresh = relay.findByPid([300], ["agy"])!;
  assert.notEqual(fresh.id, old.id);
  runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: "hi", conversationId: "c-old" }), relay, 300, {}, () => "/c");
  assert.equal(relay.findByPid([300], ["agy"])!.id, old.id);
});
