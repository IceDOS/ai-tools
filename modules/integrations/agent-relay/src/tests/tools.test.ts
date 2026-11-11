import { test } from "node:test";
import assert from "node:assert/strict";
import { TOOLS, runTool } from "../tools.ts";
import { RelayError } from "../core.ts";
import { tmpRelay } from "./helpers.ts";

test("TOOLS names and required args match the spec", () => {
  assert.deepEqual(TOOLS.map((t) => t.name), ["relay_send", "relay_link", "relay_peers"]);
  assert.deepEqual(TOOLS[0].inputSchema.required, ["to", "text"]);
  assert.deepEqual(TOOLS[1].inputSchema.required, ["peer"]);
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
  assert.equal(runTool(relay, a.id, 10, "relay_send", { to: "bbbb2", text: "hi" }), "sent to bbbb2");
});

test("bad arguments and unknown tools throw RelayError", () => {
  const { relay } = tmpRelay();
  const a = relay.register({ kind: "pi", pid: 100, cwd: "/a" });
  assert.throws(() => runTool(relay, a.id, 10, "relay_send", { to: 3 }), RelayError);
  assert.throws(() => runTool(relay, a.id, 10, "nope", {}), RelayError);
});
