import { test } from "node:test";
import assert from "node:assert/strict";
import { McpServer } from "../mcp.ts";
import { agentCwd, agentPid } from "../mcp-server.ts";
import { TOOLS } from "../tools.ts";

const rpc = (id: number, method: string, params: object = {}) => JSON.stringify({ jsonrpc: "2.0", id, method, params });

test("initialize advertises tools, the channel capability and instructions", () => {
  const s = new McpServer({ name: "agent-relay", instructions: "I", tools: TOOLS, experimental: { "claude/channel": {} }, call: () => "ok" });
  const [line] = s.handle(rpc(1, "initialize", { protocolVersion: "2025-06-18" }));
  const r = JSON.parse(line).result;
  assert.equal(r.protocolVersion, "2025-06-18");
  assert.deepEqual(r.capabilities.experimental, { "claude/channel": {} });
  assert.equal(r.instructions, "I");
});

test("tools/list and tools/call, including errors", () => {
  const s = new McpServer({ name: "x", instructions: "", tools: TOOLS, call: (n) => { if (n === "relay_send") throw new Error("boom"); return "fine"; } });
  assert.equal(JSON.parse(s.handle(rpc(2, "tools/list"))[0]).result.tools.length, 3);
  const ok = JSON.parse(s.handle(rpc(3, "tools/call", { name: "relay_peers", arguments: {} }))[0]).result;
  assert.deepEqual(ok, { content: [{ type: "text", text: "fine" }] });
  const bad = JSON.parse(s.handle(rpc(4, "tools/call", { name: "relay_send", arguments: {} }))[0]).result;
  assert.equal(bad.isError, true);
  assert.equal(bad.content[0].text, "boom");
});

test("initialized notification fires the hook, unknown methods error, notifications get no reply", () => {
  let fired = false;
  const s = new McpServer({ name: "x", instructions: "", tools: [], call: () => "" });
  s.onInitialized = () => (fired = true);
  assert.deepEqual(s.handle(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" })), []);
  assert.ok(fired);
  assert.equal(JSON.parse(s.handle(rpc(5, "nope"))[0]).error.code, -32601);
  assert.equal(JSON.parse(s.handle(rpc(6, "ping"))[0]).result && true, true);
});

test("agentPid picks the first ancestor whose command matches the agent", () => {
  const cmd: Record<number, string> = { 10: "sh", 20: "/nix/store/x-claude-code/bin/.claude-wrapped", 30: "systemd" };
  assert.equal(agentPid("claude", [10, 20, 30], (p) => cmd[p]), 20);
  assert.equal(agentPid("agy", [10, 20, 30], (p) => cmd[p]), 10);
});

test("agentCwd reads /proc/<pid>/cwd or falls back to process.cwd()", () => {
  assert.equal(agentCwd(1234, (p) => {
    if (p === "/proc/1234/cwd") return "/custom/workspace";
    throw new Error("enoent");
  }), "/custom/workspace");
  assert.equal(agentCwd(9999, () => { throw new Error("enoent"); }), process.cwd());
});
