import { test } from "node:test";
import assert from "node:assert/strict";
import { findAgy, makeDeliver, type Paste } from "../agy-watch.ts";
import { tmpRelay } from "./helpers.ts";

const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");
const path = process.getBuiltinModule("node:path") as typeof import("node:path");

function linked(busy: boolean) {
  const t = tmpRelay();
  const a = t.relay.register({ kind: "pi", pid: 100, cwd: "/a", id: "aaaa1" });
  const g = t.relay.register({ kind: "agy", pid: 300, cwd: "/c", id: "cccc3", agent: { busy } });
  t.relay.link(a.id, g.id);
  return { ...t, a, g };
}

function recorder(result: "ok" | "dirty" | "error" = "ok") {
  const sent: string[] = [];
  const paste: Paste = async (text) => {
    sent.push(text);
    return result;
  };
  return { sent, paste };
}

const inbox = (root: string, id: string) => fs.readdirSync(path.join(root, "inbox", id)).filter((f) => f.endsWith(".json"));

test("findAgy matches the agy record whose pid descends from the launched pid", () => {
  const { relay } = tmpRelay();
  relay.register({ kind: "pi", pid: 100, cwd: "/a" });
  const g = relay.register({ kind: "agy", pid: 300, cwd: "/c" });
  const chain = (pid: number) => (pid === 300 ? [300, 250, 1] : [pid, 1]);
  assert.equal(findAgy(relay, 250, chain)?.id, g.id);
  assert.equal(findAgy(relay, 999, chain), undefined);
});

test("idle agy: pastes the formatted message, marks busy and injected", async () => {
  const { relay, root, a, g } = linked(false);
  relay.send(a.id, g.id, "wake up", 10);
  const { sent, paste } = recorder();
  const w = relay.watch(g.id, makeDeliver(relay, g.id, paste), 60_000);
  await w.drain();
  w.close();
  assert.equal(sent.length, 1);
  assert.ok(sent[0].startsWith("[relay from pi aaaa1, hop 1]\nwake up"));
  assert.equal(relay.get(g.id)!.agent?.busy, true);
  assert.equal(relay.get(g.id)!.hop, 1);
  assert.equal(inbox(root, g.id).length, 0);
});

test("busy agy: nothing is pasted and the message stays for the Stop hook", async () => {
  const { relay, root, a, g } = linked(true);
  relay.send(a.id, g.id, "later", 10);
  const { sent, paste } = recorder();
  const w = relay.watch(g.id, makeDeliver(relay, g.id, paste), 60_000);
  await w.drain();
  w.close();
  assert.equal(sent.length, 0);
  assert.equal(inbox(root, g.id).length, 1);
});

test("a draft in agy's input: the message stays and busy is unchanged", async () => {
  const { relay, root, a, g } = linked(false);
  relay.send(a.id, g.id, "x", 10);
  const { sent, paste } = recorder("dirty");
  const w = relay.watch(g.id, makeDeliver(relay, g.id, paste), 60_000);
  await w.drain();
  w.close();
  assert.equal(sent.length, 1);
  assert.equal(relay.get(g.id)!.agent?.busy, false);
  assert.equal(inbox(root, g.id).length, 1);
});

test("two queued messages: only the first is pasted, the second waits", async () => {
  const { relay, root, a, g } = linked(false);
  relay.send(a.id, g.id, "one", 10);
  relay.send(a.id, g.id, "two", 10);
  const { sent, paste } = recorder();
  const w = relay.watch(g.id, makeDeliver(relay, g.id, paste), 60_000);
  await w.drain();
  w.close();
  assert.equal(sent.length, 1);
  assert.match(sent[0], /one/);
  assert.equal(inbox(root, g.id).length, 1);
});
