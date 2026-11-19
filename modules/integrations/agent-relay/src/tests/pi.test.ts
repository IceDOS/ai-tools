import { test } from "node:test";
import assert from "node:assert/strict";
import ext from "../pi-extension.ts";
import { tmpRelay } from "./helpers.ts";

function fakePi() {
  const handlers: Record<string, Function[]> = {};
  const tools: Record<string, any> = {};
  const commands: Record<string, any> = {};
  const sent: any[] = [];
  const statuses: Record<string, string | undefined> = {};
  const state = { idle: true };
  const pi = {
    on: (e: string, h: Function) => (handlers[e] ??= []).push(h),
    registerTool: (t: any) => (tools[t.name] = t),
    registerCommand: (n: string, c: any) => (commands[n] = c),
    sendMessage: (m: any, o: any) => sent.push({ m, o }),
  };
  const ctx = {
    hasUI: true,
    cwd: "/proj",
    sessionManager: { getSessionName: () => "planner", getCwd: () => "/proj" },
    isIdle: () => state.idle,
    ui: { notify: () => {}, setStatus: (k: string, v?: string) => (statuses[k] = v) },
  };
  const fire = async (e: string, ev: any = {}) => {
    for (const h of handlers[e] ?? []) await h(ev, ctx);
  };
  return { pi, ctx, tools, commands, sent, statuses, state, fire };
}

// Virtual time: the extension only sees these timers, so a test moves them on purpose instead of sleeping.
function fakeClock() {
  let now = 0;
  let seq = 0;
  const q = new Map<number, { at: number; fn: () => void }>();
  return {
    setTimeout(fn: () => void, ms: number) {
      const id = ++seq;
      q.set(id, { at: now + ms, fn });
      return id;
    },
    clearTimeout(id: any) {
      if (typeof id === "number") q.delete(id);
    },
    advance(ms: number) {
      const end = now + ms;
      for (;;) {
        const due = [...q.entries()].filter(([, v]) => v.at <= end).sort((a, b) => a[1].at - b[1].at || a[0] - b[0]);
        if (!due.length) break;
        const [id, v] = due[0];
        q.delete(id);
        now = v.at;
        v.fn();
      }
      now = end;
    },
  };
}

async function setup(ackMs?: number, probeMs?: number) {
  const { relay } = tmpRelay(new Set([process.pid, 200]));
  const f = fakePi();
  const clock = fakeClock();
  ext(f.pi as any, () => relay, ackMs, probeMs, clock);
  await f.fire("session_start", {});
  const me = relay.findByPid([process.pid])!;
  const peer = relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  relay.link(me.id, "bbbb2");
  return { relay, me, peer, clock, ...f };
}

type Ctx = Awaited<ReturnType<typeof setup>>;

// fs.watch decides when the real watcher runs, so wait for the effect rather than for a fixed delay.
async function until(cond: () => boolean, what: string, timeoutMs = 10_000) {
  const end = Date.now() + timeoutMs;
  while (!cond()) {
    if (Date.now() > end) assert.fail(`timeout after ${timeoutMs}ms waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 5));
  }
}

// agent_settled is the only hook that awaits a full watcher pass, so it doubles as "drain now".
const drain = (t: Ctx) => t.fire("agent_settled");

const errMsg = (text = "429 rate limited") => ({ message: { role: "assistant", stopReason: "error", errorMessage: text } });
const okMsg = () => ({ message: { role: "assistant", stopReason: "stop" } });
const relayMsg = () => ({ message: { role: "custom", customType: "relay" } });

test("registers as pi and reports peers", async () => {
  const t = await setup();
  assert.equal(t.me.kind, "pi");
  assert.equal(t.me.name, "planner");
  assert.match(t.statuses.relay!, new RegExp(`relay ${t.me.id}`));
  const peers = await t.tools.relay_peers.execute("1", {});
  assert.match(peers.content[0].text, /claude bbbb2/);
  await t.fire("session_shutdown");
});

test("busy pi keeps relay messages in the inbox", async () => {
  const t = await setup();
  t.state.idle = false;
  t.relay.send(t.peer.id, t.me.id, "hello", 10);
  await drain(t);
  assert.equal(t.sent.length, 0);
  assert.equal(t.relay.pending(t.me.id).length, 1);
  await t.fire("session_shutdown");
});

test("settle injects one collapsed batch in ts order", async () => {
  const t = await setup();
  t.state.idle = false;
  t.relay.send(t.peer.id, t.me.id, "first", 10);
  t.relay.send(t.peer.id, t.me.id, "second", 10);
  await drain(t);
  assert.equal(t.sent.length, 0);

  t.state.idle = true;
  await drain(t);
  assert.equal(t.sent.length, 1);
  const text = t.sent[0].m.content as string;
  assert.match(text, /^2 relay messages arrived while you were busy/);
  assert.ok(text.indexOf("first") < text.indexOf("second"));
  assert.match(text, /\[relay from claude bbbb2/);
  assert.equal(t.sent[0].o.triggerTurn, true);
  assert.equal("deliverAs" in t.sent[0].o, false);
  await t.fire("session_shutdown");
});

test("a superseded message is never injected", async () => {
  const t = await setup();
  t.state.idle = false;
  const a = t.relay.send(t.peer.id, t.me.id, "old plan", 10).msgs[0];
  t.relay.send(t.peer.id, t.me.id, "new plan", 10, { supersedes: [a.short] });
  assert.equal(t.relay.pending(t.me.id).length, 1);

  t.state.idle = true;
  await drain(t);
  assert.equal(t.sent.length, 1);
  const text = t.sent[0].m.content as string;
  assert.doesNotMatch(text, /old plan/);
  assert.match(text, new RegExp(`replaces ${a.short}`));
  await t.fire("session_shutdown");
});

test("a drain during an unacked batch does not inject twice", async () => {
  const t = await setup();
  t.state.idle = true;
  t.relay.send(t.peer.id, t.me.id, "one", 10);
  await until(() => t.sent.length === 1, "the idle message to be injected");

  await drain(t);
  assert.equal(t.sent.length, 1);
  assert.equal(t.relay.pending(t.me.id).length, 1);

  await t.fire("message_start", relayMsg());
  assert.equal(t.relay.pending(t.me.id).length, 0);
  await drain(t);
  assert.equal(t.sent.length, 1);
  await t.fire("session_shutdown");
});

test("an unacked batch is retried at the next settle", async () => {
  const t = await setup(40);
  t.state.idle = true;
  t.relay.send(t.peer.id, t.me.id, "lost", 10);
  await until(() => t.sent.length === 1, "the first injection");
  assert.equal(t.relay.pending(t.me.id).length, 1);

  t.clock.advance(40);
  await drain(t);
  assert.equal(t.sent.length, 2);
  assert.equal(t.relay.pending(t.me.id).length, 1);
  await t.fire("session_shutdown");
});

test("a delivered batch is read once its run settles", async () => {
  const t = await setup();
  t.state.idle = true;
  t.relay.send(t.peer.id, t.me.id, "hi", 10);
  await until(() => t.sent.length === 1, "the message to be injected");
  assert.equal(t.relay.pending(t.me.id).length, 1);

  await t.fire("message_start", relayMsg());
  assert.equal(t.relay.pending(t.me.id).length, 0);
  assert.equal(t.relay.status(t.peer.id)[0].state, "delivered");

  await drain(t);
  assert.equal(t.relay.status(t.peer.id)[0].state, "read");
  await t.fire("session_shutdown");
});

test("an open UI prompt holds delivery", async () => {
  const t = await setup();
  await t.fire("ui_prompt_start", {});
  t.relay.send(t.peer.id, t.me.id, "held", 10);
  await drain(t);
  assert.equal(t.sent.length, 0);
  await t.fire("ui_prompt_end", {});
  assert.equal(t.sent.length, 1);
  await t.fire("session_shutdown");
});

test("a failed run holds queued messages", async () => {
  const t = await setup(undefined, 60_000);
  t.state.idle = false;
  t.relay.send(t.peer.id, t.me.id, "hello", 10);
  await t.fire("message_end", errMsg());
  t.state.idle = true;
  await drain(t);
  assert.equal(t.sent.length, 0);
  assert.equal(t.relay.pending(t.me.id).length, 1);
  await t.fire("session_shutdown");
});

test("a live model clears the hold and the next settle flushes", async () => {
  const t = await setup(undefined, 60_000);
  t.state.idle = false;
  t.relay.send(t.peer.id, t.me.id, "hello", 10);
  await t.fire("message_end", errMsg());
  t.state.idle = true;
  await drain(t);
  assert.equal(t.sent.length, 0);

  t.state.idle = false;
  await t.fire("message_end", okMsg());
  t.state.idle = true;
  await drain(t);
  assert.equal(t.sent.length, 1);
  await t.fire("session_shutdown");
});

test("the backoff probe injects once and doubles after another failure", async () => {
  const t = await setup(undefined, 40);
  t.state.idle = false;
  t.relay.send(t.peer.id, t.me.id, "hello", 10);
  await t.fire("message_end", errMsg());
  t.state.idle = true;
  await drain(t);
  assert.equal(t.sent.length, 0);

  t.clock.advance(40);
  await until(() => t.sent.length === 1, "the probe to flush the held batch");
  await t.fire("message_start", relayMsg());

  t.relay.send(t.peer.id, t.me.id, "second", 10);
  t.state.idle = false;
  await t.fire("message_end", errMsg("still down"));
  t.state.idle = true;
  await drain(t);
  assert.equal(t.sent.length, 1, "the doubled wait has not elapsed");

  t.clock.advance(80);
  await until(() => t.sent.length === 2, "the doubled probe to flush");
  await t.fire("session_shutdown");
});

test("the auto-retry window holds messages", async () => {
  const t = await setup(undefined, 60_000);
  t.state.idle = false;
  t.relay.send(t.peer.id, t.me.id, "hello", 10);
  await t.fire("message_end", errMsg());
  await drain(t);
  assert.equal(t.sent.length, 0);
  assert.equal(t.relay.pending(t.me.id).length, 1);
  await t.fire("session_shutdown");
});

// The watcher only calls deliver when a file is there, so drive that callback directly to pin the empty-inbox branch.
test("an empty probe keeps its pass for the next message", async () => {
  const { relay } = tmpRelay(new Set([process.pid, 200]));
  let inject: ((m: any) => unknown) | undefined;
  const proxied = new Proxy(relay as any, {
    get(target: any, key: string | symbol) {
      if (key === "watch") return (_id: string, d: any) => ((inject = d), { close() {}, drain: async () => {} });
      const v = target[key as any];
      return typeof v === "function" ? v.bind(target) : v;
    },
  });
  const f = fakePi();
  const clock = fakeClock();
  ext(f.pi as any, () => proxied as any, undefined, 40, clock);
  await f.fire("session_start", {});
  const me = relay.findByPid([process.pid])!;
  const peer = relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  relay.link(me.id, "bbbb2");
  const probe = () => inject!({} as any);

  // a failed run holds the mail, and the first probe has nothing to carry
  f.state.idle = false;
  await f.fire("message_end", errMsg());
  f.state.idle = true;
  await f.fire("agent_settled");
  clock.advance(40);
  probe();

  relay.send(peer.id, me.id, "first", 10);
  probe();
  assert.equal(f.sent.length, 1, "the armed probe carries the next message");
  assert.match(f.sent[0].m.content, /first/);

  // that run failed too, so the next wait doubles
  await f.fire("message_start", relayMsg());
  f.state.idle = false;
  await f.fire("message_end", errMsg("still down"));
  f.state.idle = true;
  await f.fire("agent_settled");
  relay.send(peer.id, me.id, "second", 10);
  clock.advance(79);
  probe();
  assert.equal(f.sent.length, 1, "the doubled wait has not elapsed");
  clock.advance(1);
  probe();
  assert.equal(f.sent.length, 2, "the doubled wait then injects");
  await f.fire("session_shutdown");
});

test("a message that arrives during a tool call is steered in after it", async () => {
  const t = await setup();
  t.state.idle = false;
  await t.fire("message_end", okMsg());
  await t.fire("tool_execution_start");
  t.relay.send(t.peer.id, t.me.id, "mid-run", 10);
  await t.fire("tool_execution_end");
  assert.equal(t.sent.length, 1);
  assert.deepEqual(t.sent[0].o, { triggerTurn: true, deliverAs: "steer" });
  assert.match(t.sent[0].m.content, /mid-run/);
  await t.fire("turn_end");
  await t.fire("message_start", relayMsg());
  assert.equal(t.relay.pending(t.me.id).length, 0, "the loop injected it, so the inbox is clear");
  await t.fire("session_shutdown");
});

test("a message that arrives while the model streams waits for the next tool call", async () => {
  const t = await setup();
  t.state.idle = false;
  await t.fire("turn_end");
  t.relay.send(t.peer.id, t.me.id, "later", 10);
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(t.sent.length, 0, "no steer outside a tool batch");
  await t.fire("message_end", okMsg());
  await t.fire("tool_execution_start");
  await t.fire("tool_execution_end");
  assert.equal(t.sent.length, 1);
  assert.equal(t.sent[0].o.deliverAs, "steer");
  await t.fire("session_shutdown");
});

test("a steer dropped by an abort is offered again after the run", async () => {
  const t = await setup();
  t.state.idle = false;
  await t.fire("tool_execution_start");
  t.relay.send(t.peer.id, t.me.id, "dropped", 10);
  await t.fire("tool_execution_end");
  assert.equal(t.sent.length, 1);
  await t.fire("tool_execution_end");
  assert.equal(t.sent.length, 1, "no second steer while one is in flight");
  t.state.idle = true;
  await drain(t);
  assert.equal(t.sent.length, 2, "never acked, so the settle offers it again");
  assert.deepEqual(t.sent[1].o, { triggerTurn: true });
  await t.fire("session_shutdown");
});

test("rate-limit retries hold mail until a retry succeeds", async () => {
  const t = await setup(undefined, 60_000);
  t.state.idle = false;
  await t.fire("message_end", errMsg());
  t.relay.send(t.peer.id, t.me.id, "during backoff", 10);
  await new Promise((r) => setTimeout(r, 50));
  await t.fire("message_end", errMsg());
  assert.equal(t.sent.length, 0, "nothing reaches pi during the retries");
  // the next retry answers; the extension still waits for the run to settle or a tool batch
  await t.fire("message_end", okMsg());
  assert.equal(t.sent.length, 0);
  t.state.idle = true;
  await drain(t);
  assert.equal(t.sent.length, 1);
  assert.deepEqual(t.sent[0].o, { triggerTurn: true });
  await t.fire("session_shutdown");
});

test("after every retry fails, a held message is the probe and a good answer reopens delivery", async () => {
  const t = await setup(undefined, 60_000);
  t.state.idle = false;
  for (let i = 0; i < 4; i++) await t.fire("message_end", errMsg());
  t.relay.send(t.peer.id, t.me.id, "held", 10);
  t.state.idle = true;
  await drain(t);
  assert.equal(t.sent.length, 0, "the failed run holds the mail");
  assert.equal(t.relay.pending(t.me.id).length, 1);
  t.clock.advance(60_000);
  await until(() => t.sent.length === 1, "the probe to carry the held message");
  await t.fire("message_start", relayMsg());
  t.state.idle = false;
  await t.fire("message_end", okMsg());
  t.relay.send(t.peer.id, t.me.id, "next", 10);
  t.state.idle = true;
  await drain(t);
  assert.equal(t.sent.length, 2, "the model answered, so the next message flows at once");
  await t.fire("session_shutdown");
});
