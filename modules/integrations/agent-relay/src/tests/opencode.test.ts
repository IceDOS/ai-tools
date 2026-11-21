import { test } from "node:test";
import assert from "node:assert/strict";
import { Relay } from "../core.ts";
import { createPlugin } from "../opencode/index.ts";
import { tmpRelay } from "./helpers.ts";

const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");
const path = process.getBuiltinModule("node:path") as typeof import("node:path");

let note = 0;
function sendHop(relay: Relay, from: string, to: string): number {
  relay.send(from, to, `note ${++note}`, 10);
  return relay.pending(to).at(-1)!.hop;
}

async function until(fn: () => boolean, timeoutMs = 2000): Promise<void> {
  const start = Date.now();
  while (!fn()) {
    if (Date.now() - start > timeoutMs) throw new Error("timeout waiting for condition");
    await new Promise((r) => setTimeout(r, 10));
  }
}

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

function fakeOpencode(initialSessions: any[] = []) {
  const hooks: Record<string, Function[]> = {};
  const sessions = new Map<string, any>(initialSessions.map((s) => [s.id, s]));
  const syntheticCalls: any[] = [];
  const eventSubscribers: ((ev: any) => void)[] = [];

  const ctx = {
    location: { directory: "/project" },
    session: {
      get: async (id: string) => {
        const s = sessions.get(id);
        if (!s) throw new Error(`session not found: ${id}`);
        return s;
      },
      list: async () => Array.from(sessions.values()),
      synthetic: async (input: any) => {
        syntheticCalls.push(input);
        return { ok: true };
      },
      hook: async (name: string, handler: Function) => {
        (hooks[name] ??= []).push(handler);
      },
    },
    tool: {
      hook: async (name: string, handler: Function) => {
        (hooks[`tool:${name}`] ??= []).push(handler);
      },
    },
    event: {
      subscribe: (options?: { signal?: AbortSignal }) => ({
        [Symbol.asyncIterator]: () => {
          let resolveNext: ((v: any) => void) | undefined;
          const queue: any[] = [];
          let ended = false;
          options?.signal?.addEventListener("abort", () => {
            ended = true;
            if (resolveNext) {
              const r = resolveNext;
              resolveNext = undefined;
              r({ value: undefined, done: true });
            }
          });
          const push = (ev: any) => {
            if (ended) return;
            if (resolveNext) {
              const r = resolveNext;
              resolveNext = undefined;
              r({ value: ev, done: false });
            } else {
              queue.push(ev);
            }
          };
          eventSubscribers.push(push);
          return {
            next: () => {
              if (ended) return Promise.resolve({ value: undefined, done: true });
              if (queue.length) return Promise.resolve({ value: queue.shift(), done: false });
              return new Promise((r) => {
                resolveNext = r;
              });
            },
          };
        },
      }),
    },
  };

  const triggerHook = async (name: string, event: any) => {
    for (const h of hooks[name] ?? []) await h(event);
  };

  const emitEvent = (ev: any) => {
    for (const sub of eventSubscribers) sub(ev);
  };

  return { ctx, sessions, syntheticCalls, triggerHook, emitEvent };
}

test("registers session lazily on context and binds root session", async (t) => {
  const { relay } = tmpRelay(new Set([process.pid, 200]));
  const fake = fakeOpencode([{ id: "s-root-1", parentID: undefined }]);
  const plugin = createPlugin(() => relay);
  const cleanup = await plugin.setup(fake.ctx);
  if (typeof cleanup === "function") t.after(() => cleanup());

  assert.equal(relay.findByPid([process.pid]), undefined);

  const sys: any[] = [];
  await fake.triggerHook("context", { sessionID: "s-root-1", system: sys });
  const me = relay.sessions().find((s) => s.scope === "s-root-1")!;
  assert.ok(me);
  assert.equal(me.kind, "opencode");
  assert.equal(me.cwd, "/project");
  assert.match(sys[0].text, new RegExp(`Your relay ID is ${me.id}`));
});

test("ignores subagent sessions on prompt and marks human on root prompt", async (t) => {
  const { relay } = tmpRelay(new Set([process.pid, 200]));
  const fake = fakeOpencode([
    { id: "s-root-1", parentID: undefined },
    { id: "s-child-1", parentID: "s-root-1" },
  ]);
  const peer = relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  const plugin = createPlugin(() => relay);
  const cleanup = await plugin.setup(fake.ctx);
  if (typeof cleanup === "function") t.after(() => cleanup());

  await fake.triggerHook("context", { sessionID: "s-root-1", system: [] });
  const me = relay.sessions().find((s) => s.scope === "s-root-1")!;
  relay.link(me.id, peer.id);

  assert.equal(sendHop(relay, peer.id, me.id), 1);
  assert.equal(sendHop(relay, peer.id, me.id), 2);

  // Subagent prompt should not reset human hop
  await fake.triggerHook("prompt", { sessionID: "s-child-1", prompt: { text: "hello" } });
  assert.equal(sendHop(relay, peer.id, me.id), 3);

  // Relay prompt should be ignored
  await fake.triggerHook("prompt", { sessionID: "s-root-1", prompt: { text: "[relay from claude bbbb2] hi" } });
  assert.equal(sendHop(relay, peer.id, me.id), 4);

  // Root human prompt resets hop count
  await fake.triggerHook("prompt", { sessionID: "s-root-1", prompt: { text: "human prompt" } });
  assert.equal(sendHop(relay, peer.id, me.id), 1);
});

test("delivers pending mail while idle via synthetic steer", async (t) => {
  const { relay } = tmpRelay(new Set([process.pid, 200]));
  const fake = fakeOpencode([{ id: "s-root-1", parentID: undefined }]);
  const peer = relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  const plugin = createPlugin(() => relay);
  const cleanup = await plugin.setup(fake.ctx);
  if (typeof cleanup === "function") t.after(() => cleanup());

  await fake.triggerHook("context", { sessionID: "s-root-1", system: [] });
  const me = relay.sessions().find((s) => s.scope === "s-root-1")!;
  relay.link(me.id, peer.id);

  relay.send(me.id, peer.id, "hey");
  relay.send(peer.id, me.id, "back at you");

  await until(() => fake.syntheticCalls.length === 1);
  assert.equal(fake.syntheticCalls[0].sessionID, "s-root-1");
  assert.equal(fake.syntheticCalls[0].delivery, "steer");
  assert.match(fake.syntheticCalls[0].text, /back at you/);
});

test("delivers pending mail between tool calls via execute.after", async (t) => {
  const { relay } = tmpRelay(new Set([process.pid, 200]));
  const fake = fakeOpencode();
  const peer = relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  const plugin = createPlugin(() => relay);
  const cleanup = await plugin.setup(fake.ctx);
  if (typeof cleanup === "function") t.after(() => cleanup());

  fake.sessions.set("s-root-2", { id: "s-root-2", parentID: undefined });
  await fake.triggerHook("context", { sessionID: "s-root-2", system: [] });
  const me = relay.sessions().find((s) => s.scope === "s-root-2")!;
  relay.link(me.id, peer.id);

  relay.send(peer.id, me.id, "mid-turn mail");

  await fake.triggerHook("tool:execute.after", { sessionID: "s-root-2", tool: "bash" });

  assert.equal(fake.syntheticCalls.length, 1);
  assert.equal(fake.syntheticCalls[0].sessionID, "s-root-2");
  assert.equal(fake.syntheticCalls[0].delivery, "steer");
  assert.match(fake.syntheticCalls[0].text, /mid-turn mail/);
});

test("experimental.idle marks delivered mail read and continues if new mail arrived", async (t) => {
  const { relay } = tmpRelay(new Set([process.pid, 200]));
  const fake = fakeOpencode([{ id: "s-root-1", parentID: undefined }]);
  const peer = relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  const plugin = createPlugin(() => relay);
  const cleanup = await plugin.setup(fake.ctx);
  if (typeof cleanup === "function") t.after(() => cleanup());

  await fake.triggerHook("context", { sessionID: "s-root-1", system: [] });
  const me = relay.sessions().find((s) => s.scope === "s-root-1")!;
  relay.link(me.id, peer.id);

  relay.send(peer.id, me.id, "turn 1 mail");
  await until(() => fake.syntheticCalls.length === 1);

  relay.send(peer.id, me.id, "turn 2 mail");

  await fake.triggerHook("experimental.idle", { sessionID: "s-root-1" });

  assert.equal(fake.syntheticCalls.length, 2);
  assert.match(fake.syntheticCalls[1].text, /turn 2 mail/);

  await fake.triggerHook("experimental.idle", { sessionID: "s-root-1" });
  assert.equal(relay.get(me.id)!.agent?.busy, false);
});

test("rate-limit hold on retry hook with probe backoff", async (t) => {
  const { relay } = tmpRelay(new Set([process.pid, 200]));
  const fake = fakeOpencode([{ id: "s-root-1", parentID: undefined }]);
  const peer = relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  const clock = fakeClock();
  const plugin = createPlugin(() => relay, 10_000, clock);
  const cleanup = await plugin.setup(fake.ctx);
  if (typeof cleanup === "function") t.after(() => cleanup());

  await fake.triggerHook("context", { sessionID: "s-root-1", system: [] });
  const me = relay.sessions().find((s) => s.scope === "s-root-1")!;
  relay.link(me.id, peer.id);

  await fake.triggerHook("retry", { sessionID: "s-root-1", error: { message: "rate limited" } });

  relay.send(peer.id, me.id, "held mail");
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(fake.syntheticCalls.length, 0);

  clock.advance(10_000);
  assert.equal(fake.syntheticCalls.length, 1);
  assert.match(fake.syntheticCalls[0].text, /held mail/);
});

test("rate-limit hold on execution.failed and recovery on execution.succeeded", async (t) => {
  const { relay } = tmpRelay(new Set([process.pid, 200]));
  const fake = fakeOpencode([{ id: "s-root-1", parentID: undefined }]);
  const peer = relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  const clock = fakeClock();
  const plugin = createPlugin(() => relay, 10_000, clock);
  const cleanup = await plugin.setup(fake.ctx);
  if (typeof cleanup === "function") t.after(() => cleanup());

  await fake.triggerHook("context", { sessionID: "s-root-1", system: [] });
  const me = relay.sessions().find((s) => s.scope === "s-root-1")!;
  relay.link(me.id, peer.id);

  fake.emitEvent({ type: "session.execution.failed", data: { sessionID: "s-root-1" } });
  await new Promise((r) => setTimeout(r, 10));

  relay.send(peer.id, me.id, "held after fail");
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(fake.syntheticCalls.length, 0);

  fake.emitEvent({ type: "session.execution.succeeded", data: { sessionID: "s-root-1" } });
  await new Promise((r) => setTimeout(r, 10));

  await fake.triggerHook("tool:execute.after", { sessionID: "s-root-1", tool: "read" });
  assert.equal(fake.syntheticCalls.length, 1);
});

test("2 concurrent root sessions get separate IDs and separate delivery", async (t) => {
  const { relay } = tmpRelay(new Set([process.pid, 200]));
  const fake = fakeOpencode([
    { id: "s-root-1", parentID: undefined },
    { id: "s-root-2", parentID: undefined },
  ]);
  const peer = relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  const plugin = createPlugin(() => relay);
  const cleanup = await plugin.setup(fake.ctx);
  if (typeof cleanup === "function") t.after(() => cleanup());

  const sys1: any[] = [];
  const sys2: any[] = [];
  await fake.triggerHook("context", { sessionID: "s-root-1", system: sys1 });
  await fake.triggerHook("context", { sessionID: "s-root-2", system: sys2 });

  const r1 = relay.sessions().find((s) => s.scope === "s-root-1")!;
  const r2 = relay.sessions().find((s) => s.scope === "s-root-2")!;
  assert.ok(r1);
  assert.ok(r2);
  assert.notEqual(r1.id, r2.id);

  relay.link(r1.id, peer.id);
  relay.link(r2.id, peer.id);

  relay.send(peer.id, r1.id, "message for session 1");
  relay.send(peer.id, r2.id, "message for session 2");

  await until(() => fake.syntheticCalls.length === 2);
  const call1 = fake.syntheticCalls.find((c) => c.sessionID === "s-root-1");
  const call2 = fake.syntheticCalls.find((c) => c.sessionID === "s-root-2");

  assert.ok(call1);
  assert.ok(call2);
  assert.match(call1.text, /message for session 1/);
  assert.match(call2.text, /message for session 2/);
});

test("subagent inherits root's relay ID in system instructions and maps root for MCP", async (t) => {
  const { relay } = tmpRelay(new Set([process.pid]));
  const fake = fakeOpencode([
    { id: "s-root-1", parentID: undefined },
    { id: "s-child-1", parentID: "s-root-1" },
  ]);
  const plugin = createPlugin(() => relay);
  const cleanup = await plugin.setup(fake.ctx);
  if (typeof cleanup === "function") t.after(() => cleanup());

  const subSys: any[] = [];
  await fake.triggerHook("context", { sessionID: "s-child-1", system: subSys });

  const rootRec = relay.sessions().find((s) => s.scope === "s-root-1")!;
  assert.ok(rootRec);
  assert.match(subSys[0].text, new RegExp(`Your relay ID is ${rootRec.id}`));

  const rootMapFile = path.join(relay.root, "opencode", "roots", "s-child-1.json");
  assert.ok(fs.existsSync(rootMapFile));
  const mapping = JSON.parse(fs.readFileSync(rootMapFile, "utf8"));
  assert.equal(mapping.rootSessionID, "s-root-1");
  assert.equal(mapping.relayId, rootRec.id);
});

test("session.deleted event retires session, closes watcher, cleans pin and root files", async (t) => {
  const { relay } = tmpRelay(new Set([process.pid]));
  const fake = fakeOpencode([
    { id: "s-root-1", parentID: undefined },
    { id: "s-child-1", parentID: "s-root-1" },
  ]);
  const plugin = createPlugin(() => relay);
  const cleanup = await plugin.setup(fake.ctx);
  if (typeof cleanup === "function") t.after(() => cleanup());

  await fake.triggerHook("context", { sessionID: "s-child-1", system: [] });
  const rootRec = relay.sessions().find((s) => s.scope === "s-root-1")!;
  assert.ok(rootRec);

  const pinPath = relay.pinPath("opencode", "s-root-1");
  const rootMap = path.join(relay.root, "opencode", "roots", "s-child-1.json");
  assert.ok(fs.existsSync(pinPath));
  assert.ok(fs.existsSync(rootMap));

  fake.emitEvent({ type: "session.deleted", data: { sessionID: "s-root-1" } });
  await until(() => !relay.get(rootRec.id));

  assert.equal(relay.get(rootRec.id), undefined);
  assert.equal(fs.existsSync(pinPath), false);
  assert.equal(fs.existsSync(rootMap), false);
});

test("resume reuses pinned relay ID", async (t) => {
  const { relay } = tmpRelay(new Set([process.pid]));
  const fake1 = fakeOpencode([{ id: "s-root-1", parentID: undefined }]);
  const plugin1 = createPlugin(() => relay);
  const cleanup1 = await plugin1.setup(fake1.ctx);

  await fake1.triggerHook("context", { sessionID: "s-root-1", system: [] });
  const rec1 = relay.sessions().find((s) => s.scope === "s-root-1")!;
  assert.ok(rec1);
  const pinnedId = rec1.id;

  if (typeof cleanup1 === "function") await cleanup1();

  // Second plugin instance resumes the session
  const fake2 = fakeOpencode([{ id: "s-root-1", parentID: undefined }]);
  const plugin2 = createPlugin(() => relay);
  const cleanup2 = await plugin2.setup(fake2.ctx);
  if (typeof cleanup2 === "function") t.after(() => cleanup2());

  await fake2.triggerHook("context", { sessionID: "s-root-1", system: [] });
  const rec2 = relay.sessions().find((s) => s.scope === "s-root-1")!;
  assert.equal(rec2.id, pinnedId);
});

test("opencode migration migrates legacy private pins into core store", async (t) => {
  const { relay } = tmpRelay(new Set([process.pid]));
  const legacyDir = path.join(relay.root, "opencode", "pins");
  fs.mkdirSync(legacyDir, { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(legacyDir, "s-legacy-1.txt"), "c001a\n");

  const fake = fakeOpencode([{ id: "s-legacy-1", parentID: undefined }]);
  const plugin = createPlugin(() => relay);
  const cleanup = await plugin.setup(fake.ctx);
  if (typeof cleanup === "function") t.after(() => cleanup());

  assert.equal(fs.existsSync(path.join(legacyDir, "s-legacy-1.txt")), false);
  assert.equal(relay.readPin("opencode", "s-legacy-1"), "c001a");

  await fake.triggerHook("context", { sessionID: "s-legacy-1", system: [] });
  const rec = relay.sessions().find((s) => s.scope === "s-legacy-1")!;
  assert.equal(rec.id, "c001a");
});
