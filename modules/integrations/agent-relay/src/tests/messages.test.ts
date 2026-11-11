import { test } from "node:test";
import assert from "node:assert/strict";
import { Relay, formatMessage, nextHop, type Message } from "../core.ts";

const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");
const os = process.getBuiltinModule("node:os") as typeof import("node:os");
const path = process.getBuiltinModule("node:path") as typeof import("node:path");

function setup() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "relay-msg-"));
  let t = Date.parse("2026-11-11T10:00:00Z");
  const clock = { tick: (ms = 1000) => (t += ms) };
  const relay = new Relay({ root, isAlive: (p) => p < 1000, now: () => new Date(t) });
  const a = relay.register({ kind: "pi", pid: 100, cwd: "/a", id: "aaaa1" });
  const b = relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  const c = relay.register({ kind: "agy", pid: 300, cwd: "/c", id: "cccc3" });
  const x = relay.register({ kind: "opencode", pid: 400, cwd: "/x", id: "dddd9" });
  relay.link(a.id, b.id);
  relay.link(c.id, a.id);
  return { root, relay, clock, a, b, c, x };
}

const inbox = (root: string, id: string) => fs.readdirSync(path.join(root, "inbox", id)).filter((f) => f.endsWith(".json"));

test("send writes one message into the target inbox", () => {
  const { root, relay, a, b } = setup();
  assert.deepEqual(relay.send(a.id, "bbb", "hi", 10), [b.id]);
  const [f] = inbox(root, b.id);
  const m = JSON.parse(fs.readFileSync(path.join(root, "inbox", b.id, f), "utf8")) as Message;
  assert.equal(m.text, "hi");
  assert.equal(m.hop, 1);
  assert.deepEqual(m.from, { id: a.id, kind: "pi" });
});

test("send to all fans out without echo", () => {
  const { root, relay, a, b, c } = setup();
  assert.deepEqual(relay.send(a.id, "all", "x", 10).sort(), [b.id, c.id].sort());
  assert.equal(inbox(root, a.id).length, 0);
});

test("send outside the group, unknown target and ungrouped sender are errors", () => {
  const { relay, a, x } = setup();
  assert.throws(() => relay.send(a.id, "dddd9", "x", 10), /not in your group/);
  assert.throws(() => relay.send(a.id, "fffff", "x", 10), /no session matches/);
  assert.throws(() => relay.send(x.id, "aaaa1", "x", 10), /not linked/);
});

test("hop follows the last injected message and resets on human input", () => {
  const { relay, clock, a, b } = setup();
  assert.equal(nextHop(relay.get(b.id)!), 1);
  relay.markInjected(b.id, 4);
  assert.equal(nextHop(relay.get(b.id)!), 5);
  clock.tick();
  relay.markHuman(b.id);
  assert.equal(nextHop(relay.get(b.id)!), 1);
  clock.tick();
  relay.markInjected(b.id, 2);
  assert.equal(nextHop(relay.get(b.id)!), 3);
});

test("send refuses above the hop limit and writes nothing", () => {
  const { root, relay, a, b } = setup();
  relay.markInjected(a.id, 10);
  assert.throws(() => relay.send(a.id, b.id, "x", 10), /hop limit reached, ask the user before continuing/);
  assert.equal(inbox(root, b.id).length, 0);
});

test("alternating injections between two sessions reach hop limit at hop 11", () => {
  const { root, relay, a, b } = setup();
  let sender = a;
  let receiver = b;
  let lastHop = 0;

  for (let i = 1; i <= 10; i++) {
    const ids = relay.send(sender.id, receiver.id, `ping ${i}`, 10);
    assert.equal(ids.length, 1);
    const [f] = inbox(root, receiver.id);
    const m = JSON.parse(fs.readFileSync(path.join(root, "inbox", receiver.id, f), "utf8")) as Message;
    assert.equal(m.hop, i);
    fs.rmSync(path.join(root, "inbox", receiver.id, f));
    relay.markInjected(receiver.id, m.hop);
    lastHop = i;
    [sender, receiver] = [receiver, sender];
  }
  assert.equal(lastHop, 10);
  assert.throws(
    () => relay.send(sender.id, receiver.id, "ping 11", 10),
    /hop limit reached, ask the user before continuing/,
  );
});

test("watcher delivers existing messages in order and deletes them", async () => {
  const { root, relay, clock, a, b } = setup();
  relay.send(a.id, b.id, "one", 10);
  clock.tick();
  relay.send(a.id, b.id, "two", 10);
  fs.writeFileSync(path.join(root, "inbox", b.id, "zzz.json.123.tmp"), "{");
  const got: string[] = [];
  const w = relay.watch(b.id, (m) => (got.push(m.text), true), 60_000);
  await w.drain();
  w.close();
  assert.deepEqual(got, ["one", "two"]);
  assert.equal(inbox(root, b.id).length, 0);
});

test("watcher keeps a message the adapter could not deliver", async () => {
  const { root, relay, a, b } = setup();
  relay.send(a.id, b.id, "later", 10);
  let ok = false;
  const got: string[] = [];
  const w = relay.watch(b.id, (m) => (ok && got.push(m.text), ok), 60_000);
  await w.drain();
  assert.equal(inbox(root, b.id).length, 1);
  ok = true;
  await w.drain();
  w.close();
  assert.deepEqual(got, ["later"]);
});

test("formatMessage matches the spec", () => {
  const m: Message = { v: 1, id: "1", from: { id: "3f2a9", kind: "claude", name: "auth" }, to: "b", group: "g", hop: 2, ts: "", text: "hello" };
  assert.equal(formatMessage(m), '[relay from claude 3f2a9 "auth", hop 2]\nhello\n(reply with relay_send to="3f2a9")');
});
