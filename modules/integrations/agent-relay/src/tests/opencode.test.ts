import { test } from "node:test";
import assert from "node:assert/strict";
import { startTui } from "../opencode-tui.ts";
import { serverHooks } from "../opencode-hooks.ts";
import { tmpRelay } from "./helpers.ts";

function fakeApi(route: any) {
  const prompts: any[] = [];
  const toasts: string[] = [];
  return {
    prompts,
    toasts,
    api: {
      route: { current: route },
      ui: { toast: (t: any) => toasts.push(t.message) },
      client: { session: { promptAsync: async (p: any) => (prompts.push(p), {}) } },
      lifecycle: { onDispose: () => {} },
    },
  };
}

test("TUI plugin injects into the open session", async () => {
  const { relay } = tmpRelay(new Set([process.pid, 100]));
  const f = fakeApi({ name: "session", params: { sessionID: "s1" } });
  const w = startTui(f.api, relay);
  const me = relay.findByPid([process.pid])!;
  const a = relay.register({ kind: "pi", pid: 100, cwd: "/a", id: "aaaa1" });
  relay.link(a.id, me.id);
  relay.send(a.id, me.id, "hi", 10);
  await w.drain();
  w.close();
  assert.equal(f.prompts[0].sessionID, "s1");
  assert.match(f.prompts[0].parts[0].text, /^\[relay from pi aaaa1, hop 1\]/);
  assert.equal(relay.get(me.id)!.hop, 1);
});

test("TUI plugin keeps messages and toasts once while no session is open", async () => {
  const { relay } = tmpRelay(new Set([process.pid, 100]));
  const f = fakeApi({ name: "home" });
  const w = startTui(f.api, relay);
  const me = relay.findByPid([process.pid])!;
  const a = relay.register({ kind: "pi", pid: 100, cwd: "/a" });
  relay.link(a.id, me.id);
  relay.send(a.id, me.id, "hi", 10);
  await w.drain();
  await w.drain();
  assert.equal(f.prompts.length, 0);
  assert.equal(f.toasts.filter((t) => t.includes("waiting")).length, 1);
  f.api.route.current = { name: "session", params: { sessionID: "s2" } };
  await w.drain();
  w.close();
  assert.equal(f.prompts.length, 1);
});

test("server hooks expose tools and reset hops on human messages", async () => {
  const { relay } = tmpRelay(new Set([100, 200]));
  const me = relay.register({ kind: "opencode", pid: 100, cwd: "/o" });
  relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  const schema = { string: () => ({ describe: () => "S" }) };
  const tool: any = Object.assign((d: any) => d, { schema });
  const hooks = serverHooks(relay, tool, () => relay.get(me.id));
  assert.deepEqual(Object.keys(hooks.tool), ["relay_send", "relay_link", "relay_peers"]);
  assert.match(await hooks.tool.relay_link.execute({ peer: "bbbb2" }), /claude bbbb2/);
  relay.markInjected(me.id, 3);
  await hooks["chat.message"]({}, { parts: [{ type: "text", text: "[relay from claude bbbb2, hop 3]\nx" }] });
  assert.equal(relay.get(me.id)!.hopReset, undefined);
  await hooks["chat.message"]({}, { parts: [{ type: "text", text: "my own words" }] });
  assert.ok(relay.get(me.id)!.hopReset);
});
