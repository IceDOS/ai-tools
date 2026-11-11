import { Relay, ancestors, formatMessage, readConfig, type Kind, type Message } from "./core.ts";
import { McpServer } from "./mcp.ts";
import { INSTRUCTIONS, TOOLS, runTool } from "./tools.ts";

const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");
const readline = process.getBuiltinModule("node:readline") as typeof import("node:readline");

const PATTERNS: Record<string, RegExp> = { claude: /claude/, agy: /agy|antigravity|language_server/ };

function cmdline(pid: number): string {
  try {
    return fs.readFileSync(`/proc/${pid}/cmdline`, "utf8").split("\0")[0];
  } catch {
    return "";
  }
}

// The agent process owns the record; MCP servers and hooks may sit behind a shell.
export function agentPid(kind: string, chain: number[] = ancestors(process.ppid), cmd: (pid: number) => string = cmdline): number {
  const re = PATTERNS[kind];
  return chain.find((p) => re?.test(cmd(p))) ?? chain[0] ?? process.ppid;
}

export function agentCwd(pid: number, readlink: (p: string) => string = (p) => fs.readlinkSync(p)): string {
  try {
    return readlink(`/proc/${pid}/cwd`);
  } catch {
    return process.cwd();
  }
}

function main(kind: Kind) {
  const relay = new Relay();
  const pid = agentPid(kind);
  const rec = relay.register({
    kind,
    pid,
    cwd: agentCwd(pid),
    id: process.env.AGENT_RELAY_ID,
  });
  const out = (line: string) => process.stdout.write(line + "\n");
  const server = new McpServer({
    name: "agent-relay",
    instructions: `${INSTRUCTIONS} Your relay ID is ${rec.id}.`,
    tools: TOOLS,
    experimental: kind === "claude" ? { "claude/channel": {} } : undefined,
    call: (name, args) => runTool(relay, rec.id, readConfig().hopLimit, name, args),
  });

  const deliver = async (m: Message): Promise<boolean> => {
    const text = formatMessage(m);
    out(server.notify("notifications/claude/channel", { content: text, meta: { from: m.from.id, kind: m.from.kind, hop: String(m.hop) } }));
    relay.markInjected(rec.id, m.hop);
    return true;
  };

  let started = false;
  server.onInitialized = () => {
    if (started) return;
    started = true;
    if (kind === "claude") relay.watch(rec.id, deliver);
  };

  const rl = readline.createInterface({ input: process.stdin });
  rl.on("line", (line) => {
    if (line.trim()) for (const r of server.handle(line)) out(r);
  });
  rl.on("close", () => process.exit(0));
}

const kind = process.argv[2];
if (import.meta.main && (kind === "claude" || kind === "agy")) main(kind);
