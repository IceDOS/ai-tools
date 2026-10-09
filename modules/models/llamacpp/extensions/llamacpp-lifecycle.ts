// Starts the local llama.cpp server on demand and stops it once /slots reports every slot idle.
// Agent events cannot detect idleness: a long generation looks idle from here, is_processing does not.

import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, readdirSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Substituted at build time by icedos.nix.
const URL_BASE = "@llamacppUrl@";
const BIN = "icedos";
const PROVIDER = "llamacpp";
const IDLE_MS = Number("@llamacppIdleSeconds@") * 1000;

// A 27B model takes tens of seconds to load, well under this.
const START_TIMEOUT_MS = 180_000;
const PROBE_TIMEOUT_MS = 2_000;
const POLL_MS = 1_000;
const IDLE_CHECK_MS = 15_000;
// Fail fast after a failed start instead of making every request wait out the timeout again.
const FAILURE_BACKOFF_MS = 60_000;
// A marker older than this belongs to an instance that crashed without cleaning up.
const INSTANCE_STALE_MS = 120_000;

// Subagent teardown also emits session_shutdown with reason "quit", so whether
// anyone still uses the model comes from counting live instances, not the reason.
const INSTANCE = `${process.pid}-${randomUUID()}`;
const instanceDir = () =>
  join(
    process.env.PRIME_AGENT_CODING_AGENT_DIR ?? join(homedir(), ".config", "prime-agent"),
    "llamacpp-lifecycle",
    "instances",
  );
const registerInstance = () => {
  try {
    mkdirSync(instanceDir(), { recursive: true });
    writeFileSync(join(instanceDir(), INSTANCE), String(Date.now()));
  } catch {
    // ignore
  }
};
const unregisterInstance = () => {
  try {
    unlinkSync(join(instanceDir(), INSTANCE));
  } catch {
    // ignore
  }
};
const markerAlive = (name: string): boolean => {
  const pid = Number(name.split("-")[0]);
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (e: any) {
    return e?.code === "EPERM"; // exists, owned by another user
  }
};
// Counts other instances on the local model and reaps crashed markers. Only local
// sessions write a marker, so a cloud-only window cannot keep the model in VRAM.
const otherInstances = (): number => {
  let n = 0;
  try {
    for (const f of readdirSync(instanceDir())) {
      if (f === INSTANCE) continue;
      const path = join(instanceDir(), f);
      try {
        // Age alone would count a SIGKILLed window for two minutes with nothing left to free the VRAM.
        if (!markerAlive(f) || Date.now() - statSync(path).mtimeMs > INSTANCE_STALE_MS) {
          unlinkSync(path);
          continue;
        }
      } catch {
        continue;
      }
      n += 1;
    }
  } catch {
    return 0;
  }
  return n;
};

export default function (pi: ExtensionAPI) {
  let enabled = true;
  let idleTimer: ReturnType<typeof setInterval> | null = null;
  let starting: Promise<boolean> | null = null;
  let failedUntil = 0;
  let lastBusyAt = Date.now();

  const get = async (path: string): Promise<Response | null> => {
    try {
      const res = await fetch(`${URL_BASE}${path}`, {
        signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
      });
      return res.ok ? res : null;
    } catch {
      return null;
    }
  };
  const healthy = async () => (await get("/health")) !== null;

  // null means unreachable or an unexpected shape, which callers must not treat as idle.
  const allSlotsIdle = async (): Promise<boolean | null> => {
    const res = await get("/slots");
    if (!res) return null;
    try {
      const slots = await res.json();
      if (!Array.isArray(slots) || slots.length === 0) return null;
      return slots.every((s: any) => s?.is_processing === false);
    } catch {
      return null;
    }
  };

  const run = (args: string[]) => {
    try {
      const child = spawn(BIN, args, { detached: true, stdio: "ignore" });
      child.on("error", () => {});
      child.unref();
    } catch {
      // ignore
    }
  };

  // `starting` is set before the first await, so concurrent hooks share one start.
  const ensureUp = async (notify?: (s: string) => void): Promise<boolean> => {
    if (starting) return starting;
    if (Date.now() < failedUntil) return false;
    const attempt = (async () => {
      if (await healthy()) {
        lastBusyAt = Date.now();
        return true;
      }
      notify?.("llamacpp: loading model…");
      run(["llamacpp", "serve", "--detached"]);
      const deadline = Date.now() + START_TIMEOUT_MS;
      while (Date.now() < deadline) {
        // unref'd so a failing start does not keep the process alive after the user quits.
        await new Promise((r) => {
          const t = setTimeout(r, POLL_MS);
          t.unref?.();
        });
        if (await healthy()) {
          lastBusyAt = Date.now();
          notify?.("llamacpp: ready");
          return true;
        }
      }
      failedUntil = Date.now() + FAILURE_BACKOFF_MS;
      notify?.("llamacpp: failed to start");
      return false;
    })();
    starting = attempt;
    try {
      return await attempt;
    } finally {
      if (starting === attempt) starting = null;
    }
  };

  const startIdleWatch = () => {
    // Register after the guard: with polling off, a marker nobody refreshes would just be reaped.
    if (idleTimer || IDLE_MS <= 0) return;
    registerInstance();
    idleTimer = setInterval(async () => {
      if (!enabled || starting) return;
      const idle = await allSlotsIdle();
      // Unreachable or still loading, so not safe to stop.
      if (idle !== true) {
        if (idle === false) lastBusyAt = Date.now();
        return;
      }
      if (Date.now() - lastBusyAt < IDLE_MS) return;
      run(["llamacpp", "stop"]);
      lastBusyAt = Date.now();
    }, IDLE_CHECK_MS);
    idleTimer.unref?.();
  };
  const stopIdleWatch = () => {
    unregisterInstance();
    if (!idleTimer) return;
    clearInterval(idleTimer);
    idleTimer = null;
  };
  // Last chance to free VRAM once the poller is gone; never stops a busy server.
  const stopIfIdle = async () => {
    if (IDLE_MS <= 0) return;
    if ((await allSlotsIdle()) === true) run(["llamacpp", "stop"]);
  };

  // The payload carries a bare model id, not "provider/id", so the provider comes from the registry.
  const isLocal = (ctx: any, id?: unknown): boolean => {
    if (typeof id === "string" && id) {
      try {
        // find(), not getAll(): built-ins list first, so an id shared with a cloud model would match it.
        if (ctx?.modelRegistry?.find?.(PROVIDER, id)) return true;
      } catch {
        // registry unavailable; fall through to the session's model
      }
    }
    return ctx?.model?.provider === PROVIDER;
  };
  const notifier = (ctx: any) => (s: string) => ctx?.ui?.notify?.(s);

  // Not awaited: set_model's RPC times out before a ~35 s cold load, which would fail the
  // selection. before_provider_request waits on the same promise instead.
  const beginLoad = (ctx: any) => {
    void ensureUp(notifier(ctx)).catch(() => {});
  };
  // Refreshes only an existing marker, so a cloud-only session never registers.
  const heartbeat = setInterval(() => {
    if (idleTimer) registerInstance();
  }, 30_000);
  heartbeat.unref?.();

  pi.on("session_start", async (_e: any, ctx: any) => {
    if (!enabled || !isLocal(ctx)) return;
    startIdleWatch();
    beginLoad(ctx);
  });
  pi.on("model_select", async (event: any, ctx: any) => {
    if (!enabled || !isLocal(ctx)) {
      stopIdleWatch();
      // Only when leaving the local model with no other session on it; cloud-to-cloud switches leave it alone.
      if (
        enabled &&
        event?.previousModel?.provider === PROVIDER &&
        otherInstances() === 0
      ) {
        await stopIfIdle();
      }
      return;
    }
    startIdleWatch();
    beginLoad(ctx);
  });
  // Blocks on purpose: provider retries give up long before a ~35 s cold load finishes.
  pi.on("before_provider_request", async (event: any, ctx: any) => {
    if (!enabled || !isLocal(ctx, event?.payload?.model)) return;
    lastBusyAt = Date.now();
    startIdleWatch();
    await ensureUp(notifier(ctx));
  });

  // Also fires on reload/new/fork, which leave old closures' timers running, so always clear the poller.
  pi.on("session_shutdown", async (event: any) => {
    stopIdleWatch();
    clearInterval(heartbeat);
    unregisterInstance();
    // Subagents also report "quit"; stopping on the reason alone would reload the model after every Task.
    if (enabled && event?.reason === "quit" && otherInstances() === 0) {
      await stopIfIdle();
    }
  });

  pi.registerCommand("unload-llamacpp", {
    description: "Stop the local llama.cpp server and free its VRAM",
    handler: async (_args: any, ctx: any) => {
      run(["llamacpp", "stop"]);
      ctx?.ui?.notify?.("llamacpp: stopped");
    },
  });
  pi.registerCommand("load-llamacpp", {
    description: "Start the local llama.cpp server",
    handler: async (_args: any, ctx: any) => {
      failedUntil = 0;
      const ok = await ensureUp(notifier(ctx));
      if (!ok) ctx?.ui?.notify?.("llamacpp: failed to start");
    },
  });
  pi.registerCommand("llamacpp-autostop", {
    description: "Toggle automatic llama.cpp start/stop",
    handler: async (_args: any, ctx: any) => {
      enabled = !enabled;
      if (!enabled) stopIdleWatch();
      else if (isLocal(ctx)) startIdleWatch();
      ctx?.ui?.notify?.(`llamacpp autostop: ${enabled ? "on" : "off"}`);
    },
  });
}
