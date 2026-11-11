import { RELAY_PREFIX, Relay, ancestors, formatMessage, type Message } from "./core.ts";
import { agentCwd, agentPid } from "./mcp-server.ts";

const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");
const path = process.getBuiltinModule("node:path") as typeof import("node:path");

export function runHook(
  event: string,
  stdin: string,
  relay: Relay,
  pid: number,
  env: Record<string, string | undefined>,
  cwdOf: (pid: number) => string = agentCwd,
): string {
  const rec = relay.findByPid(ancestors(pid));
  switch (event) {
    case "claude-prompt": {
      if (!rec) return "";
      let text = "";
      try {
        const payload = JSON.parse(stdin);
        text = typeof payload?.prompt === "string" ? payload.prompt : stdin;
      } catch {
        text = stdin;
      }
      const isRelay = text.includes(RELAY_PREFIX) || /<channel\b/i.test(text);
      if (!isRelay) relay.markHuman(rec.id);
      return "";
    }
    case "agy-session-start":
      relay.register({
        kind: "agy",
        pid,
        cwd: cwdOf(pid),
        id: env.AGENT_RELAY_ID,
      });
      return "{}";
    case "agy-pre-invocation": {
      if (!rec) return "{}";
      let text = "";
      try {
        const payload = JSON.parse(stdin);
        text = typeof payload?.lastUserInput === "string" ? payload.lastUserInput : stdin;
      } catch {
        text = stdin;
      }
      const isRelay = text.trim().startsWith(RELAY_PREFIX.trim());
      const lastInput = rec.agent?.lastInput as string | undefined;
      if (text !== lastInput) {
        if (!isRelay) relay.markHuman(rec.id);
        relay.update(rec.id, { agent: { ...rec.agent, lastInput: text } });
      }
      return "{}";
    }
    case "agy-stop": {
      if (!rec) return "{}";
      const dir = path.join(relay.root, "inbox", rec.id);
      const files = fs.existsSync(dir) ? fs.readdirSync(dir).filter((f) => f.endsWith(".json")).sort() : [];
      const texts: string[] = [];
      for (const f of files) {
        try {
          const m = JSON.parse(fs.readFileSync(path.join(dir, f), "utf8")) as Message;
          texts.push(formatMessage(m));
          relay.markInjected(rec.id, m.hop);
        } catch {}
        fs.rmSync(path.join(dir, f), { force: true });
      }
      return texts.length ? JSON.stringify({ decision: "continue", reason: texts.join("\n\n") }) : "{}";
    }
    default:
      return "";
  }
}

if (import.meta.main) {
  const event = process.argv[2] ?? "";
  let stdin = "";
  try {
    stdin = fs.readFileSync(0, "utf8");
  } catch {}
  const kind = event.startsWith("agy") ? "agy" : "claude";
  try {
    process.stdout.write(runHook(event, stdin, new Relay(), agentPid(kind), process.env));
  } catch (e) {
    process.stderr.write(`agent-relay hook: ${(e as Error).message}\n`);
  }
}
