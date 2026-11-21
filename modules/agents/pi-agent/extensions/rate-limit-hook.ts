import { spawn } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export type Kind = "rate_limit" | "usage_limit" | "not_found" | "timeout";
export type Entry = { command: string[]; providers: string[]; errorKinds: Kind[]; timeoutMs: number };
type Message = { role: string; stopReason?: string; errorMessage?: string; provider?: string; model?: string };
type State = { timeouts: number; retries: number };
type ModelRef = { provider: string; id: string; cost?: { input?: number; output?: number } };
type Ctx = {
  cwd: string;
  model?: ModelRef;
  modelRegistry?: { getAvailable(): ModelRef[] };
  sessionManager: { getSessionId(): string };
  hasUI?: boolean;
  ui: { notify(message: string, type?: "info" | "warning" | "error"): void };
  signal?: AbortSignal;
};
type Run = (entry: Entry, body: string, signal?: AbortSignal) => Promise<boolean>;

// JSON of icedos.ai-tools.pi-agent.hooks.rateLimit, substituted at build time.
const RAW = "@rateLimitHooks@";
const HOOKS: Entry[] = RAW.startsWith("@") ? [] : JSON.parse(RAW);
// 0 means no retry limit.
const RAW_MAX_RETRIES = "@maxRetries@" as string;
const PARSED_MAX_RETRIES = parseInt(RAW_MAX_RETRIES, 10);
const MAX_RETRIES = RAW_MAX_RETRIES.startsWith("@")
  ? 1
  : Number.isNaN(PARSED_MAX_RETRIES)
  ? 3
  : Math.max(0, PARSED_MAX_RETRIES);
// Models tried in order once every hook failed; empty means the provider's free models.
const RAW_FALLBACK = "@fallbackModels@";
const FALLBACK_MODELS: string[] = RAW_FALLBACK.startsWith("@") ? [] : JSON.parse(RAW_FALLBACK);
const FALLBACK_RETURN = "@fallbackReturn@".startsWith("@") ? "prompt" : "@fallbackReturn@";

export type Fallback = {
  models: string[];
  // "prompt", "stay", or a duration such as "15m".
  back: string;
  switchModel(model: ModelRef): Promise<boolean>;
};

// Milliseconds for a duration such as "90s", "15m" or "2h"; undefined for "prompt", "stay" or junk.
function returnAfter(value: string): number | undefined {
  const m = /^(\d+)([smh])$/.exec(value);
  return m ? Number(m[1]) * { s: 1_000, m: 60_000, h: 3_600_000 }[m[2] as "s" | "m" | "h"] : undefined;
}

// "provider/id" or a bare id of the failing provider; free by price when the list is empty.
function candidates(list: string[], provider: string, available: ModelRef[]): ModelRef[] {
  if (list.length === 0) return available.filter((m) => m.provider === provider && !m.cost?.input && !m.cost?.output);
  return list
    .map((ref) => {
      const at = ref.indexOf("/");
      const [p, id] = at > 0 ? [ref.slice(0, at), ref.slice(at + 1)] : [provider, ref];
      return available.find((m) => m.provider === p && m.id === id);
    })
    .filter((m): m is ModelRef => m !== undefined);
}

const key = (m: { provider?: string; id?: string; model?: string }) => `${m.provider}/${m.id ?? m.model}`;

// The next model after `current` in list order, wrapping around and skipping models this prompt already tried.
function nextModel(list: ModelRef[], current: string, tried: Set<string>): ModelRef | undefined {
  const start = list.findIndex((m) => key(m) === current) + 1;
  for (let i = 0; i < list.length; i++) {
    const m = list[(start + i) % list.length];
    if (key(m) !== current && !tried.has(key(m))) return m;
  }
}

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

function payload(ctx: Ctx, message: Message, kind: Kind, retry = 0) {
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
    retry,
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

function createHandlers(hooks: Entry[], run: Run = runHook, maxRetries = MAX_RETRIES, fallback?: Fallback) {
  const state: State = { timeouts: 0, retries: 0 };
  const tried = new Set<string>();
  // What to switch back to, while the session runs on a fallback model.
  let back: { original: ModelRef; model: ModelRef; ctx: Ctx; timer?: NodeJS.Timeout } | undefined;
  const resetMs = fallback ? returnAfter(fallback.back) : undefined;

  // Switches back only while the session still uses the fallback, so a model the user picked stays.
  async function restore() {
    const entry = back;
    if (!entry) return;
    back = undefined;
    clearTimeout(entry.timer);
    let current: ModelRef | undefined;
    try {
      current = entry.ctx.model; // a live getter that throws once the session is gone
    } catch {}
    if (current && key(current) === key(entry.model)) await fallback!.switchModel(entry.original);
  }

  async function switchToFallback(message: Message, ctx: Ctx, kind: Kind): Promise<{ message: Message } | undefined> {
    if (!fallback || !ctx.model) return undefined;
    const provider = message.provider ?? "";
    const current = key(message);
    tried.add(current);
    const next = nextModel(candidates(fallback.models, provider, ctx.modelRegistry?.getAvailable() ?? []), current, tried);
    // Read before switching: ctx.model is live.
    const original = back?.original ?? ctx.model;
    if (!next || !(await fallback.switchModel(next))) return undefined;
    clearTimeout(back?.timer);
    back = { original, model: next, ctx };
    if (resetMs !== undefined) {
      back.timer = setTimeout(() => void restore(), resetMs);
      back.timer.unref?.();
    }
    notify(ctx, `${current} is limited, falling back to ${key(next)}`);
    return { message: { ...message, errorMessage: `rate limit: ${provider} hook failed (${kind}), retrying on ${key(next)}` } };
  }

  return {
    async messageEnd(event: { message: Message }, ctx: Ctx): Promise<{ message: Message } | undefined> {
      const message = event.message;
      const kind = classify(message, state);
      if (!kind || (maxRetries > 0 && state.retries >= maxRetries)) return undefined;
      const provider = message.provider ?? "";
      const entries = entriesFor(hooks, provider, kind);
      if (entries.length === 0) return undefined;
      const body = JSON.stringify(payload(ctx, message, kind, state.retries));
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
          state.retries += 1;
          notify(ctx, `rate-limit hook ran for ${provider}: ${message.errorMessage ?? ""}`);
          // The original text can hold names pi never retries, so it is replaced.
          return { message: { ...message, errorMessage: `rate limit: ${provider} hook ran (${kind}), retrying` } };
        }
        if (cancelled || ctx.signal?.aborted) return undefined;
        return await switchToFallback(message, ctx, kind);
      } finally {
        process.off("SIGINT", onCancel);
        process.off("SIGTERM", onCancel);
      }
    },
    settled() {
      state.timeouts = 0;
      state.retries = 0;
      tried.clear();
    },
    async input() {
      if (fallback?.back === "prompt") await restore();
    },
  };
}

export const lib = { classify, entriesFor, payload, runHook, createHandlers, candidates, nextModel, returnAfter };

export default function rateLimitHook(pi: ExtensionAPI) {
  const handlers = createHandlers(HOOKS, runHook, MAX_RETRIES, {
    models: FALLBACK_MODELS,
    back: FALLBACK_RETURN,
    switchModel: (model) => pi.setModel(model as any).catch(() => false),
  });
  pi.on("message_end", (event, ctx) => handlers.messageEnd(event as any, ctx as any) as any);
  pi.on("agent_settled", () => handlers.settled());
  pi.on("input", () => handlers.input());
}
