// Session cost + opencode plan usage as a setWidget. A background collector per
// (provider, model) builds the shared state every window renders.

import {
  closeSync,
  mkdirSync,
  openSync,
  readFileSync,
  statSync,
  unlinkSync,
  watch,
  writeFileSync,
  type FSWatcher,
} from "node:fs";
import { dirname, join } from "node:path";
import { spawn } from "node:child_process";
import type { AssistantMessage } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
  FREE_CAP_DEFAULT,
  FREE_POOL_CAPS,
  LOCK_STALE_MS,
  OGO_PROVIDER,
  OGO_WINDOWS,
  RUNNING_TTL_MS,
  SAMPLE_MS,
  collectorScript,
  costDir,
  freePoolOf,
  heartbeatFile,
  keyOf,
  lockBase,
  lockFile,
  rateLimitFile,
  readState,
  stateDir,
  statePidFile,
  writeAtomic,
  type CollectorMode,
  type FreeState,
  type RateLimitEvent,
  type SharedState,
} from "./shared.ts";
import {
  createPowerMeter,
  isMetered,
  isMeteredProvider,
  meteringEnabled,
} from "./power.ts";

export default function (pi: ExtensionAPI) {
  let enabled = true;
  // Electricity for locally-served models. A local model has no per-token
  // price, so without this its row would just read "free".
  const power = createPowerMeter(() => {
    if (lastCtx) refresh(lastCtx);
  });
  let sampler: ReturnType<typeof setInterval> | null = null;
  let repainter: ReturnType<typeof setTimeout> | null = null;
  let lastCtx: any = null;
  let currentKey: string | null = null;
  // Reset time from this window's own 429, shown until the collector's next
  // publish. Keyed by pool: a 429 on any group-A model applies to the shared bucket.
  let localReset: { pool: string; resetAt: number } | null = null;
  // Watch the state dir: the collector's atomic publish repaints every window
  // immediately, so footers converge instead of on independently-phased 30s polls.
  let stateWatcher: FSWatcher | null = null;
  let lastWatchRefresh = 0;

  const stopWatcher = () => {
    if (stateWatcher) {
      stateWatcher.close();
      stateWatcher = null;
    }
  };
  const startWatcher = () => {
    if (stateWatcher) return;
    try {
      mkdirSync(stateDir(), { recursive: true });
      stateWatcher = watch(stateDir(), (_ev, fname) => {
        if (!currentKey || !lastCtx) return;
        if (String(fname ?? "") !== `${currentKey}.json`) return;
        // Coalesce the rename+change pair into one repaint.
        const nowMs = Date.now();
        if (nowMs - lastWatchRefresh < 500) return;
        lastWatchRefresh = nowMs;
        refresh(lastCtx);
      });
    } catch {
      stateWatcher = null; // watcher unavailable: the 30s sampler still covers us
    }
  };

  const sgr = (code: string, s: string) => `\x1b[${code}m${s}\x1b[0m`;
  const dim = (s: string) => sgr("2", s);
  const sev = (p: number) => (p >= 80 ? "31" : p >= 50 ? "33" : "32");
  const BAR_CELLS = 10;
  const bar = (p: number) => {
    const filled = Math.max(0, Math.min(BAR_CELLS, Math.round((p / 100) * BAR_CELLS)));
    return sgr(sev(p), "▰".repeat(filled)) + dim("▱".repeat(BAR_CELLS - filled));
  };
  const fmtTokens = (n: number) =>
    n < 1000
      ? `${n}`
      : n < 1_000_000
        ? `${(n / 1000).toFixed(n < 100_000 ? 1 : 0)}k`
        : `${(n / 1_000_000).toFixed(1)}M`;
  const fmtCost = (n: number) =>
    n > 0 && n < 1 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
  const isFreeModel = (m: any) => {
    if (!m?.cost) return true;
    return !m.cost.input && !m.cost.output && !m.cost.cacheRead && !m.cost.cacheWrite;
  };

  const usage = (ctx: any) => {
    let input = 0, output = 0, cost = 0;
    for (const e of ctx.sessionManager.getBranch()) {
      if (e.type !== "message" || e.message.role !== "assistant") continue;
      const m = e.message as AssistantMessage;
      input += m.usage.input;
      output += m.usage.output;
      cost += m.usage.cost.total;
    }
    return { input, output, cost };
  };

  // The endpoint reports the GO-plan quota of the key's account; the free
  // zen plan's limits are not exposed there, so only opencode-go gets rows.
  const quotaProvider = (p: any) => p === OGO_PROVIDER;

  const modeOf = (m: any): CollectorMode | null => {
    const plan =
      quotaProvider(m?.provider) &&
      (m?.provider !== OGO_PROVIDER || !isFreeModel(m));
    if (plan) return "plan";
    if (m?.provider === "opencode" && m?.id && isFreeModel(m)) return "free";
    return null;
  };

  // -- collector lifecycle --------------------------------------------------
  const readPid = (key: string): number | null => {
    try {
      const pid = Number(readFileSync(statePidFile(key), "utf8"));
      return Number.isInteger(pid) && pid > 0 ? pid : null;
    } catch {
      return null;
    }
  };
  const collectorAlive = (key: string): boolean => {
    const pid = readPid(key);
    if (pid == null) return false;
    let live = false;
    try {
      process.kill(pid, 0);
      live = true;
    } catch (e: any) {
      live = e?.code === "EPERM"; // exists but belongs to another user
    }
    if (!live) return false;
    // Guard against pid reuse: a live pid whose published state is grossly
    // stale belongs to some unrelated process, not our collector.
    const st = readState(key);
    if (st && Date.now() - st.updatedAt > RUNNING_TTL_MS) return false;
    return true;
  };
  const spawnCollector = (provider: string, modelId: string, key: string, mode: CollectorMode) => {
    try {
      mkdirSync(costDir(), { recursive: true });
      const logFile = join(costDir(), `collector-${key}.log`);
      const logFd = openSync(logFile, "a");
      try {
        const child = spawn(
          process.execPath,
          [collectorScript(), provider, modelId, key, mode],
          { detached: true, stdio: ["ignore", logFd, logFd], env: process.env },
        );
        child.unref();
        // Publish the pid before the collector's own startup so a sibling window
        // can't double-spawn; the collector overwrites it anyway.
        if (child.pid) writeAtomic(statePidFile(key), String(child.pid));
      } finally {
        closeSync(logFd);
      }
    } catch {
      // Log unavailable; collector cannot start this tick, will be retried.
    }
  };
  const ensureCollector = (provider: string, modelId: string, mode: CollectorMode) => {
    const key = keyOf(provider, modelId);
    if (collectorAlive(key)) return;
    // Exactly one window may spawn at a time. A crash can leave a stale lock.
    try {
      mkdirSync(lockBase(), { recursive: true });
    } catch {
    }
    const lock = lockFile(key);
    try {
      const fd = openSync(lock, "wx");
      try {
        // Double-check: another window may have spawned while we queued.
        if (collectorAlive(key)) return;
        spawnCollector(provider, modelId, key, mode);
      } finally {
        closeSync(fd);
        try {
          unlinkSync(lock);
        } catch {
        }
      }
    } catch (e: any) {
      if (e?.code === "EEXIST") {
        // The lock exists; if it was left by a crashed spawner we may take it.
        try {
          if (Date.now() - statSync(lock).mtimeMs > LOCK_STALE_MS) {
            unlinkSync(lock);
            ensureCollector(provider, modelId, mode); // retry once
          }
        } catch {
        }
      }
    }
  };

  // -- heartbeat (lets the collector know at least one window is watching) --
  const writeHeartbeat = (key: string) => {
    try {
      mkdirSync(dirname(heartbeatFile(key)), { recursive: true });
      writeFileSync(heartbeatFile(key), String(Date.now()));
    } catch {
    }
  };
  const removeHeartbeat = (key: string) => {
    try {
      unlinkSync(heartbeatFile(key));
    } catch {
    }
  };

  // -- render from shared state (burn-rate helpers read state, not local cache) --
  const fmtDur = (ms: number): string | null => {
    if (!Number.isFinite(ms) || ms < 0) return null;
    const m = Math.round(ms / 60_000);
    if (m < 1) return "<1m";
    if (m < 60) return `${m}m`;
    if (m < 24 * 60) return `${Math.floor(m / 60)}h${m % 60}m`;
    const d = Math.floor(m / 1440);
    const h = Math.floor((m % 1440) / 60);
    return h ? `${d}d${h}h` : `${d}d`;
  };
  const fmtReset = (iso: string | undefined) => {
    const ms = iso ? Date.parse(iso) - Date.now() : NaN;
    if (!Number.isFinite(ms)) return null;
    return ms <= 0 ? "now" : fmtDur(ms);
  };
  // Burn-rate time to 100%, shown only when it lands before the window resets.
  const endsIn = (state: SharedState, windowKey: string): string | null => {
    const hist = state.quota.hist[windowKey];
    const w = state.quota.usage?.[windowKey];
    if (!hist || hist.length < 2 || !w) return null;
    const first = hist[0];
    const last = hist[hist.length - 1];
    const dt = (last.t - first.t) / 1000;
    const dp = last.percent - first.percent;
    if (dt < 60 || dp <= 0) return null;
    const etaMs = ((100 - w.percent) / (dp / dt)) * 1000;
    if (!Number.isFinite(etaMs) || etaMs <= 0) return null;
    const resetMs = w.resetsAt ? Date.parse(w.resetsAt) - Date.now() : Infinity;
    if (etaMs >= resetMs) return null;
    return fmtDur(etaMs);
  };
  const budgetLines = (state: SharedState) =>
    OGO_WINDOWS.map(([key, label]) => {
      const w = state.quota.usage?.[key];
      if (!w) return `${label} ${dim("…")}`;
      const alert = w.status !== "ok" ? dim("!") : "";
      const ends = endsIn(state, key);
      const pct = sgr(sev(w.percent), `${String(Math.round(w.percent)).padStart(3)}%`);
      const endsCell =
        w.status === "rate-limited"
          ? sgr("31", "limited").padEnd(11)
          : ends
            ? `ends ${ends}`.padEnd(11)
            : " ".repeat(11);
      return [
        `${alert}${label}`,
        bar(w.percent),
        pct,
        endsCell,
        `${dim("resets")} ${fmtReset(w.resetsAt) ?? "?"}`,
      ].join("  ");
    });
  // Free row: local per-IP count against the measured cap, plus the reset time
  // from the 429's Retry-After. No countdown after an IP change (fresh bucket).
  const freeRow = (f: FreeState): string => {
    const cap = FREE_POOL_CAPS[f.pool] ?? FREE_CAP_DEFAULT;
    const pct = Math.min(100, Math.round((f.cache / cap) * 100));
    const parts = [
      dim("reqs"),
      `${f.cache}/${cap}`,
      bar(pct),
      sgr(sev(pct), `${String(pct).padStart(3)}%`),
    ];
    if (f.resetAt) {
      const ms = f.resetAt - Date.now();
      parts.push(sgr("31", `resets ${fmtDur(ms) ?? "soon"}`));
    } else if (f.limited) {
      parts.push(sgr("31", "limited"));
    }
    return parts.join("  ");
  };

  const fmtPower = (n: number, cur: string) =>
    n > 0 && n < 0.01 ? `${cur}${n.toFixed(4)}` : `${cur}${n.toFixed(2)}`;

  const costLine = (
    usage: { input: number; output: number; cost: number },
    free: boolean,
    metered: boolean,
  ) => {
    // Electricity is a property of the machine, not of a session: one sensor
    // cannot attribute a shared draw, so the meter reports live draw only.
    const p = metered ? power.snapshot() : null;
    // A metered model has no per-token price to state; the rolling costs below
    // are the answer, so it leads with the bolt instead of "free".
    const parts = p
      ? [sgr("33", "⚡")]
      : [sgr("36", "◆"), free ? dim("free") : sgr("1", fmtCost(usage.cost))];
    parts.push(`↑${fmtTokens(usage.input)}`, `↓${fmtTokens(usage.output)}`);
    if (p) {
      if (p.sampling) parts.push(`${p.watts.toFixed(0)}W`);
      if (p.windows.length) {
        parts.push(dim("·"));
        for (const [label, cost] of p.windows) {
          parts.push(`${dim(label)} ${fmtPower(cost, p.currency)}`);
        }
      }
    }
    return parts.join(" ");
  };

  // -- refresh: render from local usage + shared-state budget rows -----------
  const refresh = (ctx: any) => {
    if (!ctx.hasUI) return;
    lastCtx = ctx;
    if (!enabled) {
      // Release the claim too: leaving it held would keep accruing joules for a
      // meter the user has switched off.
      power.drain();
      ctx.ui.setWidget("cost", undefined);
      if (currentKey) {
        removeHeartbeat(currentKey);
        currentKey = null;
      }
      if (sampler) {
        clearInterval(sampler);
        sampler = null;
      }
      stopWatcher();
      if (repainter) {
        clearTimeout(repainter);
        repainter = null;
      }
      return;
    }
    const { input, output, cost } = usage(ctx);
    const metered = meteringEnabled() && isMetered(ctx);
    const lines = [
      costLine({ input, output, cost }, isFreeModel(ctx.model), metered),
    ];

    const m = ctx.model;
    const mode = modeOf(m);
    const newKey = mode ? keyOf(m.provider, m.id) : null;
    if (newKey !== currentKey) {
      if (currentKey) removeHeartbeat(currentKey);
      currentKey = newKey;
      if (repainter) {
        clearTimeout(repainter);
        repainter = null;
      }
    }
    if (mode && newKey) {
      writeHeartbeat(newKey);
      startWatcher();
      ensureCollector(m.provider, m.id, mode);
      const state = readState(newKey);
      if (state) {
        if (mode === "plan" && state.quota?.usage) lines.push(...budgetLines(state));
        if (mode === "free" && state.free) {
          const f = { ...state.free };
          if (localReset && localReset.pool === f.pool) {
            // Prefer the collector's value once published; drop ours then.
            if (f.resetAt) localReset = null;
            else if (localReset.resetAt > Date.now()) f.resetAt = localReset.resetAt;
            else localReset = null;
          }
          lines.push(freeRow(f));
        }
      } else if (!repainter) {
        // Just spawned: repaint quickly once the first state arrives.
        repainter = setTimeout(() => {
          repainter = null;
          refresh(lastCtx);
        }, 3_000);
      }
    }
    // A metered local model has no collector mode; its rolling windows are
    // written by whichever window holds the sampling claim.
    const wantsPoll = mode !== null || metered;
    if (!wantsPoll && sampler) {
      clearInterval(sampler);
      sampler = null;
    }
    if (!mode) stopWatcher();
    if (wantsPoll && !sampler) {
      sampler = setInterval(() => {
        if (currentKey) writeHeartbeat(currentKey);
        refresh(lastCtx);
      }, SAMPLE_MS);
    }
    ctx.ui.setWidget("cost", lines, { placement: "belowEditor" });
  };

  // -- hooks --
  pi.on("session_start", async (_e, ctx) => refresh(ctx));
  pi.on("turn_end", async (_e, ctx) => {
    // No power.stop() here: turn_end would double-decrement a single request.
    // A claim with no matching message_end is reclaimed by the idle drain.
    refresh(ctx);
  });
  // Metering brackets the whole request, not after_provider_response: headers
  // land before a token, so that hook would time only the prefill.

  // Registered only when the meter can sample: any handler parks the runner's
  // Idempotency-Key reuse, so an inert one would bill every retry twice.
  if (meteringEnabled())
    pi.on("before_provider_request", (event: any, ctx: any) => {
      if (!enabled || !ctx?.hasUI) return;
      lastCtx = ctx;
      if (isMetered(ctx, event?.payload?.model)) {
        power.start(() => ctx?.isIdle?.() === true);
      }
    });
  pi.on("message_end", (event: any, ctx: any) => {
    if (event?.message?.role !== "assistant") return;
    // Metering was started for a metered request, so only a metered reply may
    // release its claim; an unrelated cloud reply must not stop the sampler.
    if (isMeteredProvider(event?.message?.provider)) power.stop();
    refresh(ctx);
  });
  pi.on("model_select", async (_e, ctx) => refresh(ctx));
  pi.on("session_before_switch", async (_e, ctx) => refresh(ctx));
  // The 429 message carries Retry-After (seconds until the per-IP bucket
  // reopens); record it so the collector publishes the real reset time.
  pi.on("after_provider_response", (event: any, ctx: any) => {
    if (event?.status !== 429 || !currentKey || !lastCtx) return;
    const m = lastCtx.model;
    if (!m?.provider || !m?.id || keyOf(m.provider, m.id) !== currentKey) return;
    if (modeOf(m) !== "free") return;
    const raw = event.headers?.["retry-after"];
    const retryAfter =
      raw != null && /^\d+$/.test(String(raw).trim())
        ? Number(String(raw).trim())
        : null;
    const at = Date.now();
    const pool = freePoolOf(m.id);
    try {
      const ev: RateLimitEvent = { at, retryAfter };
      writeAtomic(rateLimitFile(pool), JSON.stringify(ev));
    } catch {
    }
    if (retryAfter != null) localReset = { pool, resetAt: at + retryAfter * 1000 };
    if (repainter) clearTimeout(repainter);
    repainter = setTimeout(() => {
      repainter = null;
      refresh(lastCtx);
    }, 500);
  });

  // Also fires on "reload"/"new"/"fork": the old closures' timers keep running
  // with nothing to invalidate them, so the teardown must be unconditional.
  pi.on("session_shutdown", async () => {
    power.drain();
    power.flushPending();
    if (sampler) {
      clearInterval(sampler);
      sampler = null;
    }
    if (repainter) {
      clearTimeout(repainter);
      repainter = null;
    }
    if (currentKey) {
      removeHeartbeat(currentKey);
      currentKey = null;
    }
    stopWatcher();
    localReset = null;
  });

  pi.registerCommand("cost", {
    description: "Toggle cost widget",
    handler: async (_args: any, ctx: any) => {
      enabled = !enabled;
      refresh(ctx);
    },
  });
}
