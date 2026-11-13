import { Relay, RelayError, ancestors, describe, type Message, type Watcher } from "./core.ts";

const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");
const path = process.getBuiltinModule("node:path") as typeof import("node:path");
const os = process.getBuiltinModule("node:os") as typeof import("node:os");
const { spawn } = process.getBuiltinModule("node:child_process") as typeof import("node:child_process");

export class UsageError extends Error {}

export function parentCommandName(ppid = process.ppid): string {
  try {
    const cmd = fs.readFileSync(`/proc/${ppid}/cmdline`, "utf8").split("\0").filter(Boolean)[0];
    if (cmd) {
      const b = path.basename(cmd);
      if (b) return b;
    }
  } catch {}
  return "script";
}

export class RingBuffer {
  readonly limit: number;
  lines: string[] = [];
  private partial = "";

  constructor(limit = 20) {
    this.limit = limit;
  }

  feed(chunk: Buffer | string): void {
    if (this.limit <= 0) return;
    const str = this.partial + chunk.toString();
    const parts = str.split("\n");
    this.partial = parts.pop() ?? "";
    for (const line of parts) {
      this.lines.push(line);
      if (this.lines.length > this.limit) this.lines.shift();
    }
  }

  finish(): string {
    if (this.limit <= 0) return "";
    if (this.partial) {
      this.lines.push(this.partial);
      if (this.lines.length > this.limit) this.lines.shift();
      this.partial = "";
    }
    return this.lines.join("\n");
  }
}

export function formatDuration(ms: number): string {
  const totalSec = Math.max(0, Math.round(ms / 1000));
  if (totalSec < 60) return `${totalSec}s`;
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  if (m < 60) return s > 0 ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const remM = m % 60;
  return remM > 0 ? `${h}h ${remM}m` : `${h}h`;
}

export function resolveTarget(
  relay: Relay,
  toFlag?: string,
  env: Record<string, string | undefined> = process.env,
  pid: number = process.pid,
  ancestorsOf: (pid: number) => number[] = ancestors,
): string {
  const liveAgents = relay.sessions().filter((s) => s.kind !== "script");
  if (toFlag !== undefined) {
    const matched = relay.match(toFlag, liveAgents);
    if (matched.length === 1) return matched[0].id;
    if (matched.length === 0) throw new RelayError(`no session matches "${toFlag}"`);
    throw new RelayError(`"${toFlag}" is ambiguous: ${matched.map(describe).join(", ")}`);
  }
  if (env.AGENT_RELAY_TARGET) {
    const target = env.AGENT_RELAY_TARGET;
    const matched = relay.match(target, liveAgents);
    if (matched.length === 1) return matched[0].id;
    if (matched.length === 0) throw new RelayError(`no session matches "${target}"`);
    throw new RelayError(`"${target}" is ambiguous: ${matched.map(describe).join(", ")}`);
  }
  const fromAncestor = relay.findByPid(ancestorsOf(pid), (s) => s.kind !== "script");
  if (fromAncestor) return fromAncestor.id;
  throw new RelayError("no target session found; specify --to or run from an agent session");
}

export function parseArgs(argv: string[]): {
  cmd: string;
  to?: string;
  name?: string;
  timeout?: number;
  tail?: string;
  dashDash: string[];
  rest: string[];
} {
  const dashDashIdx = argv.indexOf("--");
  const beforeDash = dashDashIdx >= 0 ? argv.slice(0, dashDashIdx) : argv;
  const dashDash = dashDashIdx >= 0 ? argv.slice(dashDashIdx + 1) : [];

  let cmd = "";
  let to: string | undefined;
  let name: string | undefined;
  let timeout: number | undefined;
  let tail: string | undefined;
  const rest: string[] = [];

  for (let i = 0; i < beforeDash.length; i++) {
    const arg = beforeDash[i];
    if (!cmd && !arg.startsWith("-")) {
      cmd = arg;
      continue;
    }
    if (arg === "--to" || arg === "-t") {
      to = beforeDash[++i];
      if (to === undefined) throw new UsageError("--to requires an argument");
    } else if (arg.startsWith("--to=")) {
      to = arg.slice(5);
    } else if (arg === "--name" || arg === "-n") {
      name = beforeDash[++i];
      if (name === undefined) throw new UsageError("--name requires an argument");
    } else if (arg.startsWith("--name=")) {
      name = arg.slice(7);
    } else if (arg === "--timeout") {
      const v = beforeDash[++i];
      if (v === undefined) throw new UsageError("--timeout requires an argument");
      timeout = parseFloat(v);
      if (Number.isNaN(timeout)) throw new UsageError(`invalid --timeout value: ${v}`);
    } else if (arg.startsWith("--timeout=")) {
      timeout = parseFloat(arg.slice(10));
      if (Number.isNaN(timeout)) throw new UsageError(`invalid --timeout value: ${arg.slice(10)}`);
    } else if (arg === "--tail") {
      tail = beforeDash[++i];
      if (tail === undefined) throw new UsageError("--tail requires an argument");
    } else if (arg.startsWith("--tail=")) {
      tail = arg.slice(7);
    } else if (arg === "--help" || arg === "-h") {
      cmd = "help";
    } else if (arg.startsWith("-") && arg !== "-") {
      throw new UsageError(`unknown option: ${arg}`);
    } else {
      rest.push(arg);
    }
  }

  return { cmd, to, name, timeout, tail, dashDash, rest };
}

function printUsage(): void {
  const usage = `Usage: agent-relay <subcommand> [options]

Subcommands:
  id   [--to <target>]
  send [--to <target>] [--name <name>] [TEXT...]
  ask  [--to <target>] [--name <name>] [--timeout <sec>] [TEXT...]
  run  [--to <target>] [--name <name>] [--tail <lines>] -- CMD [ARGS...]
`;
  process.stdout.write(usage);
}

export async function runCli(argv: string[], env = process.env, relay = new Relay()): Promise<number> {
  const parsed = parseArgs(argv);
  switch (parsed.cmd) {
    case "help":
      printUsage();
      return 0;

    case "id": {
      const targetId = resolveTarget(relay, parsed.to, env);
      process.stdout.write(`${targetId}\n`);
      return 0;
    }

    case "send": {
      const targetId = resolveTarget(relay, parsed.to, env);
      const name = parsed.name ?? parentCommandName();
      const text = parsed.rest.length === 0 || (parsed.rest.length === 1 && parsed.rest[0] === "-")
        ? fs.readFileSync(0, "utf8")
        : parsed.rest.join(" ");
      relay.sendScript(targetId, text, { name });
      process.stdout.write(`${targetId}\n`);
      return 0;
    }

    case "ask": {
      const targetId = resolveTarget(relay, parsed.to, env);
      const name = parsed.name ?? parentCommandName();
      const text = parsed.rest.length === 0 || (parsed.rest.length === 1 && parsed.rest[0] === "-")
        ? fs.readFileSync(0, "utf8")
        : parsed.rest.join(" ");

      const scriptRec = relay.register({
        kind: "script",
        pid: process.pid,
        cwd: process.cwd(),
        name,
        agent: { awaiting: targetId },
      });

      let cleaned = false;
      const cleanup = () => {
        if (cleaned) return;
        cleaned = true;
        relay.unregister(scriptRec.id);
      };

      process.once("exit", cleanup);
      process.once("SIGINT", () => {
        cleanup();
        process.exit(130);
      });
      process.once("SIGTERM", () => {
        cleanup();
        process.exit(143);
      });

      relay.sendScript(targetId, text, { fromId: scriptRec.id, name, reply: true });

      return await new Promise<number>((resolve) => {
        let timer: NodeJS.Timeout | undefined;
        let pollTimer: NodeJS.Timeout | undefined;
        let watcher: Watcher | undefined;

        const cleanupAll = () => {
          if (pollTimer) clearInterval(pollTimer);
          if (timer) clearTimeout(timer);
          watcher?.close();
          cleanup();
        };

        if (parsed.timeout !== undefined && parsed.timeout > 0) {
          timer = setTimeout(() => {
            cleanupAll();
            resolve(124);
          }, parsed.timeout * 1000);
          timer.unref?.();
        }

        const pollMs = env.AGENT_RELAY_POLL_MS ? parseInt(env.AGENT_RELAY_POLL_MS, 10) : 3000;
        pollTimer = setInterval(() => {
          if (!relay.get(targetId)) {
            cleanupAll();
            process.stderr.write(`agent ${targetId} exited\n`);
            resolve(1);
          }
        }, pollMs);
        pollTimer.unref?.();

        watcher = relay.watch(scriptRec.id, (m: Message) => {
          process.stdout.write(m.text.endsWith("\n") ? m.text : `${m.text}\n`);
          cleanupAll();
          resolve(0);
          return true;
        });
        void watcher.drain();
      });
    }

    case "run": {
      if (parsed.dashDash.length === 0) {
        throw new UsageError("run requires a command after --");
      }
      const targetId = resolveTarget(relay, parsed.to, env);
      const cmdName = parsed.dashDash[0];
      const cmdStr = parsed.dashDash.join(" ");
      const name = parsed.name ?? path.basename(cmdName);
      const tailLimit = parsed.tail !== undefined ? parseInt(parsed.tail, 10) : 20;
      if (Number.isNaN(tailLimit)) throw new UsageError(`invalid --tail value: ${parsed.tail}`);

      const ring = new RingBuffer(tailLimit);
      const startTime = Date.now();

      return await new Promise<number>((resolve) => {
        const child = spawn(parsed.dashDash[0], parsed.dashDash.slice(1), {
          stdio: ["inherit", "pipe", "pipe"],
        });

        const signals: NodeJS.Signals[] = ["SIGINT", "SIGTERM", "SIGHUP"];
        const sigHandlers = new Map<NodeJS.Signals, () => void>();
        let forwardedSig: NodeJS.Signals | undefined;
        for (const sig of signals) {
          const handler = () => {
            forwardedSig = sig;
            try {
              child.kill(sig);
            } catch {}
          };
          sigHandlers.set(sig, handler);
          process.on(sig, handler);
        }

        child.stdout?.on("data", (chunk: Buffer) => {
          process.stdout.write(chunk);
          ring.feed(chunk);
        });

        child.stderr?.on("data", (chunk: Buffer) => {
          process.stderr.write(chunk);
          ring.feed(chunk);
        });

        let finished = false;
        const finish = (exitCode: number, statusText: string) => {
          if (finished) return;
          finished = true;
          for (const [sig, handler] of sigHandlers) {
            process.removeListener(sig, handler);
          }
          const duration = formatDuration(Date.now() - startTime);
          const tail = ring.finish();
          const body = tail ? `${cmdStr} exited ${statusText} after ${duration}\n${tail}` : `${cmdStr} exited ${statusText} after ${duration}`;
          try {
            relay.sendScript(targetId, body, { name });
          } catch (e) {
            process.stderr.write(`agent-relay: ${(e as Error).message}\n`);
          }
          resolve(exitCode);
        };

        child.on("error", (err: Error) => {
          process.stderr.write(`${err.message}\n`);
          finish(127, "127");
        });

        child.on("close", (code: number | null, signal: NodeJS.Signals | null) => {
          const effectiveSig = signal ?? forwardedSig;
          if (effectiveSig) {
            const sigNum = os.constants.signals[effectiveSig] ?? 15;
            finish(128 + sigNum, effectiveSig);
          } else if (code !== null) {
            finish(code, String(code));
          } else {
            finish(0, "0");
          }
        });
      });
    }

    default:
      if (!parsed.cmd) {
        printUsage();
        return 2;
      }
      throw new UsageError(`unknown command: ${parsed.cmd}`);
  }
}

if (import.meta.main) {
  runCli(process.argv.slice(2))
    .then((code) => {
      process.exit(code);
    })
    .catch((err) => {
      if (err instanceof UsageError) {
        process.stderr.write(`agent-relay: ${err.message}\n`);
        process.exit(2);
      }
      if (err instanceof RelayError) {
        process.stderr.write(`${err.message}\n`);
        process.exit(1);
      }
      process.stderr.write(`agent-relay: ${err.message}\n`);
      process.exit(1);
    });
}
