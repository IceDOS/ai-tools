import { spawn } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export type Kind = "rate_limit" | "usage_limit" | "not_found" | "timeout";
export type Entry = { command: string[]; providers: string[]; errorKinds: Kind[]; timeoutMs: number };
type Message = { role: string; stopReason?: string; errorMessage?: string; provider?: string; model?: string };
type State = { timeouts: number; ran: boolean };
type Ctx = {
  cwd: string;
  sessionManager: { getSessionId(): string };
  hasUI?: boolean;
  ui: { notify(message: string, type?: "info" | "warning" | "error"): void };
  signal?: AbortSignal;
};
type Run = (entry: Entry, body: string, signal?: AbortSignal) => Promise<boolean>;

// JSON of icedos.ai-tools.pi-agent.hooks.rateLimit, substituted at build time.
const RAW = "@rateLimitHooks@";
const HOOKS: Entry[] = RAW.startsWith("@") ? [] : JSON.parse(RAW);

// pi's non-retryable account limits (pi-ai utils/retry); checked first so a 429 inside them stays usage_limit.
const USAGE_LIMIT =
  /GoUsageLimitError|FreeUsageLimitError|Monthly usage limit|available balance|insufficient_quota|out of budget|quota exceeded|billing/i;
const RATE_LIMIT = /(?<![a-z0-9])429(?![a-z0-9])|rate.?limit|too many requests/i;
const NOT_FOUND = /(?<![a-z0-9])404(?![a-z0-9])/i;
const TIMEOUT = /timed out|timeout|ETIMEDOUT/i;
const TIMEOUTS_BEFORE_HOOK = 2;

function classify(message: Message, state: State): Kind | null {
  if (message.role !== "assistant" || message.stopReason !== "error") return null;
  const text = message.errorMessage ?? "";
  if (USAGE_LIMIT.test(text)) return "usage_limit";
  if (RATE_LIMIT.test(text)) return "rate_limit";
  if (NOT_FOUND.test(text)) return "not_found";
  if (!TIMEOUT.test(text)) return null;
  state.timeouts += 1;
  if (state.timeouts < TIMEOUTS_BEFORE_HOOK) return null;
  state.timeouts = 0;
  return "timeout";
}

function entriesFor(hooks: Entry[], provider: string, kind: Kind): Entry[] {
  return hooks.filter((e) => e.command.length > 0 && e.providers.includes(provider) && e.errorKinds.includes(kind));
}

function sessionId(ctx: Ctx): string {
  try {
    return ctx.sessionManager.getSessionId();
  } catch {
    return "";
  }
}

function payload(ctx: Ctx, message: Message, kind: Kind) {
  return {
    hook_event_name: "RateLimit",
    notification_type: "",
    cwd: ctx.cwd,
    session_id: sessionId(ctx),
    permission_mode: "",
    provider: message.provider ?? "",
    model: message.model ?? "",
    errorKind: kind,
    errorMessage: message.errorMessage ?? "",
  };
}

// Own process group, so a timeout or cancel signal kills everything the hook started.
function runHook(entry: Entry, body: string, signal?: AbortSignal): Promise<boolean> {
  if (signal?.aborted) return Promise.resolve(false);
  return new Promise((resolve) => {
    let child: ReturnType<typeof spawn>;
    try {
      child = spawn(entry.command[0], entry.command.slice(1), {
        detached: true,
        stdio: ["pipe", "ignore", "ignore"],
      });
    } catch {
      resolve(false);
      return;
    }

    let settled = false;
    let timer: NodeJS.Timeout | null = null;
    const cleanup = () => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      process.off("SIGINT", cancel);
      process.off("SIGTERM", cancel);
      if (signal) signal.removeEventListener("abort", cancel);
    };

    const cancel = () => {
      if (settled) return;
      try {
        if (child.pid) process.kill(-child.pid, "SIGTERM");
      } catch {}
      const grace = setTimeout(() => {
        try {
          if (child.pid) process.kill(-child.pid, "SIGKILL");
        } catch {}
      }, 2000);
      grace.unref();
      cleanup();
      resolve(false);
    };

    if (signal) signal.addEventListener("abort", cancel, { once: true });
    process.on("SIGINT", cancel);
    process.on("SIGTERM", cancel);

    timer = setTimeout(cancel, entry.timeoutMs);

    child.on("error", () => {
      cleanup();
      resolve(false);
    });
    child.on("exit", (code) => {
      cleanup();
      resolve(code === 0);
    });
    child.stdin?.on("error", () => {});
    child.stdin?.end(body);
  });
}

function notify(ctx: Ctx, text: string) {
  try {
    if (ctx.hasUI) ctx.ui.notify(text, "warning");
  } catch {}
}

function createHandlers(hooks: Entry[], run: Run = runHook) {
  const state: State = { timeouts: 0, ran: false };
  return {
    async messageEnd(event: { message: Message }, ctx: Ctx): Promise<{ message: Message } | undefined> {
      const message = event.message;
      const kind = classify(message, state);
      if (!kind || state.ran) return undefined;
      const provider = message.provider ?? "";
      const entries = entriesFor(hooks, provider, kind);
      if (entries.length === 0) return undefined;
      state.ran = true;
      const body = JSON.stringify(payload(ctx, message, kind));
      let cancelled = false;
      const onCancel = () => { cancelled = true; };
      process.on("SIGINT", onCancel);
      process.on("SIGTERM", onCancel);
      try {
        for (const entry of entries) {
          if (cancelled || ctx.signal?.aborted) return undefined;
          notify(ctx, `running rate-limit hook for ${provider}... (press Esc to cancel)`);
          const ok = await run(entry, body, ctx.signal);
          if (cancelled || ctx.signal?.aborted) return undefined;
          if (!ok) continue;
          notify(ctx, `rate-limit hook ran for ${provider}: ${message.errorMessage ?? ""}`);
          // The original text can hold names pi never retries, so it is replaced.
          return { message: { ...message, errorMessage: `rate limit: ${provider} hook ran (${kind}), retrying` } };
        }
      } finally {
        process.off("SIGINT", onCancel);
        process.off("SIGTERM", onCancel);
      }
      return undefined;
    },
    settled() {
      state.timeouts = 0;
      state.ran = false;
    },
  };
}

export const lib = { classify, entriesFor, payload, runHook, createHandlers };

export default function rateLimitHook(pi: ExtensionAPI) {
  const handlers = createHandlers(HOOKS);
  pi.on("message_end", (event, ctx) => handlers.messageEnd(event as any, ctx as any) as any);
  pi.on("agent_settled", () => handlers.settled());
}
