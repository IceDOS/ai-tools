import { test } from "node:test";
import assert from "node:assert/strict";
import { TOOLS, runTool } from "../tools.ts";
import { Relay, RelayError, type Limits } from "../core.ts";
import { tmpRelay } from "./helpers.ts";

const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");
const os = process.getBuiltinModule("node:os") as typeof import("node:os");
const path = process.getBuiltinModule("node:path") as typeof import("node:path");

function clocked(limits?: Partial<Limits>) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "relay-tools-"));
  let t = Date.parse("2026-11-11T10:00:00Z");
  const clock = { tick: (ms = 1000) => (t += ms) };
  const relay = new Relay({ root, limits, isAlive: (p) => p < 1000, now: () => new Date(t), ancestorsOf: (p) => [p] });
  const a = relay.register({ kind: "pi", pid: 100, cwd: "/a", id: "aaaa1" });
  const b = relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  const c = relay.register({ kind: "agy", pid: 300, cwd: "/c", id: "cccc3" });
  relay.link(a.id, b.id);
  relay.link(c.id, a.id);
  return { root, relay, clock, a, b, c };
}

test("TOOLS names and required args match the spec", () => {
  assert.deepEqual(TOOLS.map((t) => t.name), ["relay_send", "relay_link", "relay_peers", "relay_status"]);
  assert.deepEqual(TOOLS[0].inputSchema.required, ["to", "text"]);
  assert.deepEqual(TOOLS[1].inputSchema.required, ["peer"]);
  assert.deepEqual(TOOLS[3].inputSchema.required, []);
  assert.ok("in_reply_to" in TOOLS[0].inputSchema.properties);
  assert.ok("supersedes" in TOOLS[0].inputSchema.properties);
  assert.ok("cancels" in TOOLS[0].inputSchema.properties);
});

test("relay_peers before linking explains how to link", () => {
  const { relay } = tmpRelay();
  const a = relay.register({ kind: "pi", pid: 100, cwd: "/a", id: "aaaa1" });
  const out = runTool(relay, a.id, 10, "relay_peers", {});
  assert.match(out, /^you: pi aaaa1/);
  assert.match(out, /not linked/);
});

test("relay_link then relay_send", () => {
  const { relay } = tmpRelay();
  const a = relay.register({ kind: "pi", pid: 100, cwd: "/a", id: "aaaa1" });
  relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  assert.match(runTool(relay, a.id, 10, "relay_link", { peer: "bbbb" }), /- claude bbbb2 cwd=\/b/);
  assert.match(runTool(relay, a.id, 10, "relay_send", { to: "bbbb2", text: "hi" }), /^sent to bbbb2 as [0-9a-f]{4}$/);
});

test("relay_send reports replaces, cancels and duplicates", () => {
  const { relay, a, b } = clocked();
  const first = runTool(relay, a.id, 10, "relay_send", { to: "bbbb2", text: "draft" }).split(" ").pop()!;
  runTool(relay, a.id, 10, "relay_send", { to: "bbbb2", text: "final", supersedes: first });
  assert.deepEqual(relay.pending(b.id).map((m) => m.text), ["final"]);
  assert.deepEqual(relay.pending(b.id)[0].supersedes, [first]);
  const second = relay.pending(b.id)[0].short!;
  runTool(relay, a.id, 10, "relay_send", { to: "bbbb2", text: "typo", cancels: ` ${second}, ` });
  assert.deepEqual(relay.pending(b.id).map((m) => m.text), ["typo"]);
  assert.deepEqual(relay.pending(b.id)[0].cancels, [second]);
  assert.equal(runTool(relay, a.id, 10, "relay_send", { to: "bbbb2", text: "typo" }), "already sent to bbbb2 as " + relay.pending(b.id)[0].short + " (duplicate suppressed)");
});

test("relay_send passes in_reply_to through", () => {
  const { relay, a, b } = clocked();
  const short = runTool(relay, a.id, 10, "relay_send", { to: "bbbb2", text: "q" }).split(" ").pop()!;
  runTool(relay, b.id, 10, "relay_send", { to: "aaaa1", text: "a", in_reply_to: short });
  assert.equal(relay.pending(a.id)[0].inReplyTo, short);
});

test("relay_status lists recent sends and one message by id", () => {
  const { relay, clock, a } = clocked();
  assert.equal(runTool(relay, a.id, 10, "relay_status", {}), "no messages sent yet");
  const shorts = ["one", "two", "three"].map((text) => {
    clock.tick();
    return runTool(relay, a.id, 10, "relay_send", { to: "bbbb2", text }).split(" ").pop()!;
  });
  const lines = runTool(relay, a.id, 10, "relay_status", {}).split("\n");
  assert.equal(lines.length, 3);
  assert.match(lines[0], new RegExp(`^msg ${shorts[2]} to bbbb2 10:00:03 queued$`));
  assert.equal(runTool(relay, a.id, 10, "relay_status", { id: shorts[1] }), `msg ${shorts[1]} to bbbb2 10:00:02 queued`);
  assert.throws(() => runTool(relay, a.id, 10, "relay_status", { id: "zzzz" }), /no message matches "zzzz"/);
});

test("relay_peers shows unread counts", () => {
  const { relay, a, b } = clocked();
  runTool(relay, a.id, 10, "relay_send", { to: "bbbb2", text: "one" });
  runTool(relay, a.id, 10, "relay_send", { to: "bbbb2", text: "two" });
  assert.match(runTool(relay, b.id, 10, "relay_peers", {}), /- pi aaaa1 cwd=\/a 2 unread/);
});

test("a withheld send is reported without failing the whole call", () => {
  const { relay, a, b } = clocked({ pairLimit: 1 });
  runTool(relay, a.id, 10, "relay_send", { to: "bbbb2", text: "one" });
  const out = runTool(relay, a.id, 1, "relay_send", { to: "all", text: "two" });
  assert.match(out, /^sent to cccc3 as [0-9a-f]{4}\nwithheld to bbbb2: 1 messages to you not yet delivered$/);
  assert.match(runTool(relay, a.id, 10, "relay_status", {}), /^msg [0-9a-f]{4} to bbbb2 \S+ withheld \(1 messages to you not yet delivered\)$/m);
  assert.throws(() => runTool(relay, a.id, 1, "relay_send", { to: "bbbb2", text: "three" }), /message withheld: 1 messages to you not yet delivered/);
  assert.equal(relay.pending(b.id).filter((m) => m.notice).length, 1);
});

test("bad arguments and unknown tools throw RelayError", () => {
  const { relay } = tmpRelay();
  const a = relay.register({ kind: "pi", pid: 100, cwd: "/a" });
  assert.throws(() => runTool(relay, a.id, 10, "relay_send", { to: 3 }), RelayError);
  assert.throws(() => runTool(relay, a.id, 10, "relay_send", { to: "x", text: "y", cancels: 7 }), RelayError);
  assert.throws(() => runTool(relay, a.id, 10, "nope", {}), RelayError);
});
