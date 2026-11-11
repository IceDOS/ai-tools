import { test } from "node:test";
import assert from "node:assert/strict";
import ext from "../pi-extension.ts";
import { tmpRelay } from "./helpers.ts";

function fakePi() {
  const handlers: Record<string, Function> = {};
  const tools: Record<string, any> = {};
  const commands: Record<string, any> = {};
  const sent: any[] = [];
  const statuses: Record<string, string | undefined> = {};
  const pi = {
    on: (e: string, h: Function) => (handlers[e] = h),
    registerTool: (t: any) => (tools[t.name] = t),
    registerCommand: (n: string, c: any) => (commands[n] = c),
    registerMessageRenderer: () => {},
    sendMessage: (m: any, o: any) => sent.push({ m, o }),
  };
  const ctx = {
    hasUI: true,
    cwd: "/proj",
    sessionManager: { getSessionName: () => "planner", getCwd: () => "/proj" },
    ui: { notify: () => {}, setStatus: (k: string, v?: string) => (statuses[k] = v) },
  };
  return { pi, ctx, handlers, tools, commands, sent, statuses };
}

test("pi adapter registers, injects as followUp and tracks hops", async () => {
  const { relay } = tmpRelay(new Set([process.pid, 200]));
  const f = fakePi();
  ext(f.pi as any, () => relay);
  await f.handlers.session_start({}, f.ctx);
  const me = relay.findByPid([process.pid])!;
  assert.equal(me.kind, "pi");
  assert.equal(me.name, "planner");
  assert.match(f.statuses.relay!, new RegExp(`relay ${me.id}`));

  const peer = relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  assert.match((await f.tools.relay_link.execute("1", { peer: "bbbb2" })).content[0].text, /claude bbbb2/);
  relay.markInjected(peer.id, 3);
  relay.send(peer.id, me.id, "hello", 10);
  await new Promise((r) => setTimeout(r, 200));
  assert.equal(f.sent.length, 1);
  assert.equal(f.sent[0].m.customType, "relay");
  assert.match(f.sent[0].m.content, /^\[relay from claude bbbb2, hop 4\]/);
  assert.deepEqual(f.sent[0].o, { triggerTurn: true, deliverAs: "followUp" });
  assert.equal(relay.get(me.id)!.hop, 4);

  await f.handlers.input({ source: "interactive", text: "hi" }, f.ctx);
  assert.equal((await f.tools.relay_send.execute("2", { to: "bbbb2", text: "x" })).content[0].text, "sent to bbbb2");
  await f.handlers.session_shutdown({}, f.ctx);
});
