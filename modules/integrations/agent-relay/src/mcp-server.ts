import { Relay, ancestors, formatMessage, readConfig, type Kind, type Message } from "./core.ts";
import { McpServer } from "./mcp.ts";
import { INSTRUCTIONS, TOOLS, runTool } from "./tools.ts";

const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");
const path = process.getBuiltinModule("node:path") as typeof import("node:path");
const readline = process.getBuiltinModule("node:readline") as typeof import("node:readline");

const PATTERNS: Record<string, RegExp> = { claude: /claude/, agy: /agy|antigravity|language_server/, opencode: /opencode/ };

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
  const out = (line: string) => process.stdout.write(line + "\n");

  if (kind === "opencode") {
    // OpenCode shares one server instance across sessions; caller is identified by _meta sessionID.
    const server = new McpServer({
      name: "agent-relay",
      instructions: INSTRUCTIONS,
      tools: TOOLS,
      call: (name, args, meta) => {
        const sessionID = meta?.["ai.opencode/sessionID"];
        if (typeof sessionID !== "string" || !sessionID) {
          throw new Error("Missing ai.opencode/sessionID in tool call metadata");
        }
        const all = relay.sessions();
        let target = all.find((s) => s.kind === "opencode" && s.scope === sessionID);
        if (!target) {
          const rootFile = path.join(relay.root, "opencode", "roots", `${sessionID}.json`);
          try {
            const data = JSON.parse(fs.readFileSync(rootFile, "utf8"));
            const rootId = typeof data === "string" ? data : data?.rootSessionID;
            if (rootId) {
              target = all.find((s) => s.kind === "opencode" && s.scope === rootId);
            }
          } catch {}
        }
        if (!target) {
          throw new Error(`No registered relay session found for opencode session ${sessionID}`);
        }
        return runTool(relay, target.id, readConfig().hopLimit, name, args);
      },
    });

    const rl = readline.createInterface({ input: process.stdin });
    rl.on("line", (line) => {
      if (line.trim()) for (const r of server.handle(line)) out(r);
    });
    rl.on("close", () => process.exit(0));
    return;
  }

  const pid = agentPid(kind);
  const rec = relay.register({
    kind,
    pid,
    cwd: agentCwd(pid),
    id: process.env.AGENT_RELAY_ID,
  });
  const injected: Message[] = [];
  const server = new McpServer({
    name: "agent-relay",
    instructions: `${INSTRUCTIONS} Your relay ID is ${rec.id}.`,
    tools: TOOLS,
    experimental: kind === "claude" ? { "claude/channel": {} } : undefined,
    call: (name, args) => {
      // Claude has seen the channel notifications once it calls a relay tool.
      for (const m of injected.splice(0)) relay.read(m);
      return runTool(relay, rec.id, readConfig().hopLimit, name, args);
    },
  });

  const deliver = async (m: Message): Promise<boolean> => {
    const text = formatMessage(m);
    out(server.notify("notifications/claude/channel", { content: text, meta: { from: m.from.id, kind: m.from.kind, hop: String(m.hop) } }));
    relay.delivered(m);
    injected.push(m);
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
if (import.meta.main && (kind === "claude" || kind === "agy" || kind === "opencode")) main(kind);
