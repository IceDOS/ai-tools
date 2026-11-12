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
    case "agy-session-start": {
      // agy runs this at the first prompt, after the watcher may have marked it busy.
      const known = rec?.pid === pid && rec.agent?.busy !== undefined;
      relay.register({
        kind: "agy",
        pid,
        cwd: cwdOf(pid),
        id: env.AGENT_RELAY_ID,
        ...(known ? {} : { agent: { busy: false } }),
      });
      return "{}";
    }
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
      const agent: Record<string, unknown> = { ...rec.agent, busy: true };
      if (text !== rec.agent?.lastInput) {
        if (!isRelay) relay.markHuman(rec.id);
        agent.lastInput = text;
      }
      relay.update(rec.id, { agent });
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
      if (texts.length) return JSON.stringify({ decision: "continue", reason: texts.join("\n\n") });
      // agent-relay-agy-watch pastes queued messages only while this flag is false.
      relay.update(rec.id, { agent: { ...relay.get(rec.id)?.agent, busy: false } });
      return "{}";
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
