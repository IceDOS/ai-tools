import { test } from "node:test";
import assert from "node:assert/strict";
import { Relay, formatMessage, pidAlive, readConfig, type Limits, type Message } from "../core.ts";

const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");
const os = process.getBuiltinModule("node:os") as typeof import("node:os");
const path = process.getBuiltinModule("node:path") as typeof import("node:path");

function setup(limits?: Partial<Limits>) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "relay-msg-"));
  const stateDir = path.join(root, "state");
  let t = Date.parse("2026-11-11T10:00:00Z");
  const clock = { tick: (ms = 1000) => (t += ms) };
  const relay = new Relay({
    root,
    stateDir,
    limits,
    isAlive: (p) => p < 1000,
    now: () => new Date(t),
    ancestorsOf: (p) => (p === 500 ? [500, 450, 100] : [p]),
  });
  const a = relay.register({ kind: "pi", pid: 100, cwd: "/a", id: "aaaa1" });
  const b = relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  const c = relay.register({ kind: "agy", pid: 300, cwd: "/c", id: "cccc3" });
  const x = relay.register({ kind: "agy", pid: 400, cwd: "/x", id: "dddd9" });
  relay.link(a.id, b.id);
  relay.link(c.id, a.id);
  return { root, relay, clock, a, b, c, x };
}

const inbox = (root: string, id: string) => fs.readdirSync(path.join(root, "inbox", id)).filter((f) => f.endsWith(".json"));

async function takeDelivery(relay: Relay, id: string): Promise<void> {
  const w = relay.watch(id, () => true, 60_000);
  await w.drain();
  w.close();
}

test("send writes one message into the target inbox", () => {
  const { root, relay, a, b } = setup();
  const r = relay.send(a.id, "bbb", "hi", 10);
  assert.deepEqual(r.targets, [b.id]);
  assert.equal(r.msgs.length, 1);
  assert.match(r.msgs[0].short, /^[0-9a-f]{4}$/);
  const [f] = inbox(root, b.id);
  const m = JSON.parse(fs.readFileSync(path.join(root, "inbox", b.id, f), "utf8")) as Message;
  assert.equal(m.text, "hi");
  assert.equal(m.hop, 1);
  assert.equal(m.state, "queued");
  assert.deepEqual(m.from, { id: a.id, kind: "pi", parent: null });
});

test("send to all fans out without echo", () => {
  const { root, relay, a, b, c } = setup();
  assert.deepEqual(relay.send(a.id, "all", "x", 10).targets.sort(), [b.id, c.id].sort());
  assert.equal(inbox(root, a.id).length, 0);
});

test("send outside the group, unknown target and ungrouped sender are errors", () => {
  const { relay, a, x } = setup();
  assert.throws(() => relay.send(a.id, "dddd9", "x", 10), /not in your group/);
  assert.throws(() => relay.send(a.id, "fffff", "x", 10), /no session matches/);
  assert.throws(() => relay.send(x.id, "aaaa1", "x", 10), /not linked/);
});

test("the hop counts sends to one pair and resets when the receiver takes delivery", async () => {
  const { relay, a, b } = setup();
  const hop = (text: string) => {
    const r = relay.send(a.id, b.id, text, 3);
    const m = relay.pending(b.id).find((q) => q.short === r.msgs[0].short)!;
    return m.hop;
  };
  assert.equal(hop("one"), 1);
  assert.equal(hop("two"), 2);
  assert.equal(hop("three"), 3);
  await takeDelivery(relay, b.id);
  assert.equal(hop("after delivery"), 1);
});

test("a withheld message tells the receiver and refuses the sender", () => {
  const { relay, a, b } = setup();
  for (const t of ["one", "two", "three"]) relay.send(a.id, b.id, t, 3);
  assert.throws(
    () => relay.send(a.id, b.id, "four", 3),
    /^Error: message withheld: 3 messages to you not yet delivered\. Ask the user before continuing$/,
  );
  const notices = relay.pending(b.id).filter((m) => m.notice);
  assert.equal(notices.length, 1);
  const n = notices[0];
  assert.equal(n.notice?.reason, "3 messages to you not yet delivered");
  assert.equal(n.notice?.preview, "four");
  assert.equal(n.notice?.bytes, 4);
  assert.match(formatMessage(n), /, withheld\]/);
  assert.match(formatMessage(n), /\n\(withheld text: "four" 4 bytes\)/);
  assert.throws(() => relay.send(a.id, b.id, "five", 3), /message withheld/);
  assert.equal(relay.pending(b.id).filter((m) => m.notice).length, 1);
  assert.ok(relay.status(a.id).some((r) => r.state === "withheld"));
});

test("the rate guard stops a burst inside the window", () => {
  const { relay, clock, a, b } = setup({ rateLimit: 3, windowMs: 60_000 });
  for (const t of ["one", "two", "three"]) relay.send(a.id, b.id, t);
  assert.throws(() => relay.send(a.id, b.id, "four"), /message withheld: 3 messages to you in 60s/);
  clock.tick(60_001);
  assert.doesNotThrow(() => relay.send(a.id, b.id, "four"));
});

test("an identical message is not queued twice", async () => {
  const { relay, clock, a, b } = setup();
  const first = relay.send(a.id, b.id, "same");
  const dup = relay.send(a.id, b.id, "same");
  assert.deepEqual(dup.msgs, []);
  assert.equal(dup.deduped?.short, first.msgs[0].short);
  assert.equal(relay.pending(b.id).filter((m) => !m.notice).length, 1);
  await takeDelivery(relay, b.id);
  assert.equal(relay.send(a.id, b.id, "same").deduped?.short, first.msgs[0].short);
  clock.tick(30_001);
  assert.equal(relay.send(a.id, b.id, "same").msgs.length, 1);
});

test("supersedes drops a queued message and flags the new one", async () => {
  const { relay, a, b } = setup();
  const old = relay.send(a.id, b.id, "draft").msgs[0];
  const next = relay.send(a.id, b.id, "final", undefined, { supersedes: [old.short] }).msgs[0];
  assert.deepEqual(relay.pending(b.id).map((m) => m.text), ["final"]);
  assert.deepEqual(relay.pending(b.id)[0].supersedes, [old.short]);
  assert.match(formatMessage(relay.pending(b.id)[0]), new RegExp(`replaces ${old.short}`));
  assert.equal(relay.status(a.id, old.short)[0].state, "superseded");
  assert.equal(relay.status(a.id, next.short)[0].state, "queued");
  await takeDelivery(relay, b.id);
  relay.send(a.id, b.id, "later", undefined, { supersedes: [old.short] });
  assert.deepEqual(relay.pending(b.id)[0].supersedes, [old.short]);
  assert.equal(relay.status(a.id, old.short)[0].state, "superseded");
});

test("cancels drops a queued message without replacing it", () => {
  const { relay, a, b } = setup();
  const victim = relay.send(a.id, b.id, "typo").msgs[0];
  relay.send(a.id, b.id, "fixed", undefined, { cancels: [victim.short] });
  assert.deepEqual(relay.pending(b.id).map((m) => m.text), ["fixed"]);
  assert.deepEqual(relay.pending(b.id)[0].cancels, [victim.short]);
  assert.equal(relay.status(a.id, victim.short)[0].state, "superseded");
  assert.match(formatMessage(relay.pending(b.id)[0]), new RegExp(`cancels ${victim.short}`));
});

test("delivery moves a message from queued to delivered and out of the inbox", async () => {
  const { root, relay, a, b } = setup();
  const sent = relay.send(a.id, b.id, "hello").msgs[0];
  assert.equal(relay.status(a.id, sent.short)[0].state, "queued");
  assert.deepEqual(relay.unread(b.id), { [a.id]: 1 });
  await takeDelivery(relay, b.id);
  assert.equal(inbox(root, b.id).length, 0);
  assert.equal(relay.status(a.id, sent.short)[0].state, "delivered");
  assert.deepEqual(relay.unread(b.id), {});
});

test("status lists the last ten sent and resolves a short id", () => {
  const { relay, clock, a, b } = setup({ pairLimit: 50, rateLimit: 50 });
  const shorts = Array.from({ length: 12 }, (_, i) => {
    clock.tick();
    return relay.send(a.id, b.id, `m${i}`).msgs[0].short;
  });
  const rows = relay.status(a.id);
  assert.equal(rows.length, 10);
  assert.equal(rows[0].short, shorts[11]);
  assert.equal(rows[9].short, shorts[2]);
  assert.deepEqual(relay.status(a.id, shorts[3]).map((r) => r.short), [shorts[3]]);
  assert.throws(() => relay.status(a.id, "zzzz"), /no message matches "zzzz"/);
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

test("formatMessage adds the short id and the reply target", () => {
  const m: Message = {
    v: 1,
    id: "1",
    short: "ab12",
    from: { id: "3f2a9", kind: "claude", name: "auth" },
    to: "b",
    hop: 2,
    ts: "2026-11-11T10:00:00.000Z",
    text: "hello",
    inReplyTo: "cd34",
  };
  assert.equal(
    formatMessage(m),
    '[relay from claude 3f2a9 "auth", hop 2, msg ab12 10:00:00, reply to cd34]\nhello\n(reply with relay_send to="3f2a9" in_reply_to="ab12")',
  );
});

test("a session spawned under another agent carries it as parent in messages", () => {
  const { relay, a, b } = setup();
  const kid = relay.register({ kind: "pi", pid: 500, cwd: "/a", id: "eeee5" });
  assert.deepEqual(kid.parent, { id: a.id, kind: "pi" });
  assert.equal(b.parent, null);
  relay.link(kid.id, b.id);
  relay.send(kid.id, b.id, "done", 10);
  const [m] = relay.pending(b.id);
  assert.match(formatMessage(m), /^\[relay from pi eeee5, parent pi aaaa1, hop 1, msg [0-9a-f]{4} \d\d:\d\d:\d\d\]\ndone\n/);
});

test("a script speaking for an agent says so and answers to the agent", () => {
  const { relay, a, b } = setup();
  const m = relay.sendScript(b.id, "build ok", { fromId: "50000", name: "make", onBehalfOf: a.id });
  assert.deepEqual(m.from.onBehalfOf, { id: a.id, kind: "pi" });
  assert.equal(m.hop, 1);
  const text = formatMessage(m);
  assert.match(text, /^\[relay from script "make" on behalf of pi aaaa1, msg [0-9a-f]{4} \d\d:\d\d:\d\d\]\nbuild ok\n/);
  assert.match(text, /\(reply with relay_send to="aaaa1" in_reply_to="[0-9a-f]{4}"\)$/);
});

test("pruning a dead session marks its queued mail withheld for the senders", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "relay-msg-"));
  const stateDir = path.join(root, "state");
  const alive = new Set([100, 200]);
  const relay = new Relay({ root, stateDir, isAlive: (p) => alive.has(p), ancestorsOf: (p) => [p] });
  const a = relay.register({ kind: "pi", pid: 100, cwd: "/a", id: "aaaa1" });
  const b = relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  relay.link(a.id, b.id);
  const one = relay.send(a.id, b.id, "one").msgs[0];
  relay.send(a.id, b.id, "two");
  assert.equal(relay.pending(b.id).length, 2);
  alive.delete(200);
  assert.equal(relay.get(b.id), undefined);
  assert.equal(fs.existsSync(path.join(root, "inbox", b.id)), false);
  const rows = relay.status(a.id);
  assert.equal(rows.length, 2);
  for (const r of rows) {
    assert.equal(r.state, "withheld");
    assert.equal(r.reason, "receiver exited before delivery");
  }
  assert.equal(relay.status(a.id, one.short)[0].state, "withheld");
});

test("a zombie process counts as dead", () => {
  assert.equal(pidAlive(2 ** 31 - 1), false);
  assert.equal(pidAlive(process.pid), true);
  assert.equal(pidAlive(process.pid, () => "Z"), false);
  assert.equal(pidAlive(process.pid, () => "S"), true);
});

test("a script that overruns the pair budget leaves a withheld notice", () => {
  const { relay, a, b } = setup({ pairLimit: 1 });
  relay.sendScript(b.id, "first", { fromId: "50000", name: "make", onBehalfOf: a.id });
  assert.throws(
    () => relay.sendScript(b.id, "second", { fromId: "50001", name: "make", onBehalfOf: a.id }),
    /message withheld: 1 messages to you not yet delivered/,
  );
  const notices = relay.pending(b.id).filter((m) => m.notice);
  assert.equal(notices.length, 1);
  assert.equal(notices[0].notice?.reason, "1 messages to you not yet delivered");
  assert.equal(notices[0].notice?.preview, "second");
  assert.ok(relay.status(a.id).some((r) => r.state === "withheld" && r.reason === "1 messages to you not yet delivered"));
});

test("updateAgent merges into the agent instead of replacing the record", () => {
  const { relay, a } = setup();
  relay.update(a.id, { agent: { busy: true, watcher: "w1" } });
  relay.updateAgent(a.id, { busy: false });
  assert.deepEqual(relay.get(a.id)?.agent, { busy: false, watcher: "w1" });
  const rec = relay.updateAgent(a.id, { busyAt: "2026-11-11T10:00:01Z" });
  assert.equal(rec.agent?.watcher, "w1");
  assert.equal(rec.agent?.busy, false);
  assert.throws(() => relay.updateAgent("fffff", { a: 1 }), /not registered/);
});

test("a terminal ledger row is dropped after a day, a queued row is kept", () => {
  const { root, relay, a, b } = setup();
  const one = relay.send(a.id, b.id, "one", 10).msgs[0];
  const two = relay.send(a.id, b.id, "two", 10).msgs[0];
  const three = relay.send(a.id, b.id, "three", 10).msgs[0];
  const four = relay.send(a.id, b.id, "four", 10).msgs[0];
  const rowFile = (id: string) => path.join(root, "outbox", a.id, `${id}.json`);
  const old = new Date(Date.parse("2026-11-11T10:00:00Z") - 25 * 3600_000).toISOString();
  const terminal = JSON.parse(fs.readFileSync(rowFile(one.id), "utf8"));
  fs.writeFileSync(rowFile(one.id), JSON.stringify({ ...terminal, state: "read", stateAt: old }));
  const queued = JSON.parse(fs.readFileSync(rowFile(two.id), "utf8"));
  fs.writeFileSync(rowFile(two.id), JSON.stringify({ ...queued, state: "queued", ts: old, stateAt: old }));
  // A restart can leave a row at delivered for good, so that state ages out too.
  const delivered = JSON.parse(fs.readFileSync(rowFile(three.id), "utf8"));
  fs.writeFileSync(rowFile(three.id), JSON.stringify({ ...delivered, state: "delivered", stateAt: old }));
  const fresh = JSON.parse(fs.readFileSync(rowFile(four.id), "utf8"));
  const recent = new Date(Date.parse("2026-11-11T10:00:00Z") - 3600_000).toISOString();
  fs.writeFileSync(rowFile(four.id), JSON.stringify({ ...fresh, state: "delivered", stateAt: recent }));
  const ids = relay.status(a.id).map((r) => r.id);
  assert.ok(!ids.includes(one.id), "a read row older than a day is dropped");
  assert.ok(!ids.includes(three.id), "a delivered row older than a day is dropped");
  assert.ok(ids.includes(four.id), "a fresh delivered row stays");
  assert.ok(ids.includes(two.id), "a queued row survives however old it is");
});

test("the sweep removes outbox and pair dirs of sessions that are gone", () => {
  const { root, relay, a } = setup();
  const gone = relay.register({ kind: "pi", pid: 600, cwd: "/gone", id: "eeee1" });
  relay.link(a.id, gone.id);
  const sent = relay.send(gone.id, a.id, "bye", 10).msgs[0];
  relay.unregister(gone.id);
  const outbox = path.join(root, "outbox", gone.id);
  assert.ok(fs.existsSync(outbox), "the ledger of a gone session outlives it");
  const row = JSON.parse(fs.readFileSync(path.join(outbox, `${sent.id}.json`), "utf8"));
  const old = new Date(Date.parse("2026-11-11T10:00:00Z") - 25 * 3600_000).toISOString();
  fs.writeFileSync(path.join(outbox, `${sent.id}.json`), JSON.stringify({ ...row, state: "withheld", stateAt: old }));

  const pairDir = path.join(root, "pairs", "dead1");
  fs.mkdirSync(pairDir, { recursive: true });
  const deadPair = path.join(pairDir, "dead2.json");
  const youngPair = path.join(pairDir, "live9.json");
  for (const f of [deadPair, youngPair]) fs.writeFileSync(f, JSON.stringify({ n: 1, lastAt: "", recent: [] }));
  const oldTime = new Date(Date.now() - 3_600_000);
  fs.utimesSync(deadPair, oldTime, oldTime);

  relay.status(a.id);
  assert.ok(!fs.existsSync(outbox), "an empty outbox dir of a gone session is removed");
  assert.ok(!fs.existsSync(deadPair), "an aged pair file of two gone sessions is removed");
  assert.ok(fs.existsSync(youngPair), "a young pair file is kept");
});

const rewriteRow = (root: string, dir: string, id: string, patch: Record<string, unknown>): void => {
  const file = path.join(root, dir, `${id}.json`);
  fs.writeFileSync(file, JSON.stringify({ ...JSON.parse(fs.readFileSync(file, "utf8")), ...patch }));
};

test("a ref resolves against the sender's own ledger, never a foreign row", () => {
  const { root, relay, a, b, c } = setup();
  const mine = relay.send(a.id, b.id, "mine", 10).msgs[0];
  const foreign = relay.send(c.id, b.id, "theirs", 10).msgs[0];
  // The foreign short is an exact match for a ref that is only a prefix of the sender's own row.
  rewriteRow(root, path.join("outbox", c.id), foreign.id, { short: "abcd" });
  rewriteRow(root, path.join("outbox", a.id), mine.id, { short: "abcd11" });
  relay.send(a.id, b.id, "next", 10, { supersedes: ["abcd"] });
  assert.equal(relay.status(a.id, "abcd11")[0].state, "superseded");
});

test("an ambiguous supersede ref is refused instead of quietly picking one", () => {
  const { root, relay, a, b } = setup();
  const one = relay.send(a.id, b.id, "one", 10).msgs[0];
  const two = relay.send(a.id, b.id, "two", 10).msgs[0];
  rewriteRow(root, path.join("outbox", a.id), one.id, { short: "abcd11" });
  rewriteRow(root, path.join("outbox", a.id), two.id, { short: "abcd22" });
  assert.throws(() => relay.send(a.id, b.id, "three", 10, { supersedes: ["abcd"] }), /"abcd" is ambiguous/);
  assert.equal(relay.status(a.id, "abcd11")[0].state, "queued");
});

test("a resend that replaces an earlier message is not deduped away", () => {
  const { relay, a, b } = setup();
  const first = relay.send(a.id, b.id, "same", 10).msgs[0];
  assert.equal(relay.send(a.id, b.id, "same", 10).deduped?.short, first.short);
  const again = relay.send(a.id, b.id, "same", 10, { supersedes: [first.short] });
  assert.equal(again.msgs.length, 1);
  assert.deepEqual(relay.pending(b.id).map((m) => m.supersedes), [[first.short]]);
  assert.equal(relay.status(a.id, first.short)[0].state, "superseded");
});

test("a config file holding null does not break every relay", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "relay-cfg-"));
  fs.mkdirSync(path.join(home, "agent-relay"), { recursive: true });
  fs.writeFileSync(path.join(home, "agent-relay", "config.json"), "null");
  const cfg = readConfig({ XDG_CONFIG_HOME: home });
  assert.equal(cfg.pairLimit, 10);
  assert.equal(cfg.hopLimit, 10);
});

test("a withheld notice does not refund the pair budget it reports", () => {
  const { relay, a, b } = setup({ pairLimit: 1 });
  relay.send(a.id, b.id, "first", 1);
  assert.throws(() => relay.send(a.id, b.id, "second", 1), /message withheld/);
  const notice = relay.pending(b.id).find((m) => m.notice)!;
  assert.ok(relay.delivered(notice), "the notice is delivered");
  assert.throws(() => relay.send(a.id, b.id, "third", 1), /message withheld/);
});

test("a pair file whose receiver is gone is dropped once it ages out", () => {
  const { root, relay, a } = setup();
  const dir = path.join(root, "pairs", a.id);
  fs.mkdirSync(dir, { recursive: true });
  const dead = path.join(dir, "gone7.json");
  const young = path.join(dir, "gone8.json");
  for (const f of [dead, young]) fs.writeFileSync(f, JSON.stringify({ n: 1, lastAt: "", recent: [] }));
  const old = new Date(Date.now() - 3_600_000);
  fs.utimesSync(dead, old, old);
  relay.status(a.id);
  assert.ok(!fs.existsSync(dead), "an aged pair file of a gone receiver is removed");
  assert.ok(fs.existsSync(young), "a young pair file is kept");
});

test("a send to all claims a replacement only in the copy that carries it", () => {
  const { relay, a, b, c } = setup();
  const victim = relay.send(a.id, b.id, "first", 10).msgs[0];
  const sent = relay.send(a.id, "all", "second", 10, { supersedes: [victim.short] });
  assert.equal(sent.msgs.length, 2);
  const toB = relay.pending(b.id).find((m) => m.text === "second")!;
  const toC = relay.pending(c.id).find((m) => m.text === "second")!;
  assert.deepEqual(toB.supersedes, [victim.short]);
  assert.equal(toC.supersedes, undefined);
  assert.equal(relay.status(a.id, victim.short)[0].state, "superseded");
});

test("formatMessage reports the reply target in the fixed header order", () => {
  const { relay, a, b } = setup();
  const first = relay.send(a.id, b.id, "one", 10).msgs[0];
  relay.send(a.id, b.id, "two", 10, { inReplyTo: first.short });
  const m = relay.pending(b.id).find((x) => x.text === "two")!;
  assert.match(formatMessage(m), new RegExp(`, msg ${m.short} \\d{2}:\\d{2}:\\d{2}, reply to ${first.short}\\]`));
  assert.match(formatMessage({ ...m, supersedes: ["abcd"] }), new RegExp(`, reply to ${first.short}, replaces abcd\\]`));
});
