// One background collector per (provider, model) builds all cross-window
// usage (quota, burn history, free counts); it exits when no window wakes it.

import { existsSync, readFileSync, readdirSync, statSync, unlinkSync } from "node:fs";
import { join } from "node:path";
import {
  FREE_WINDOW_MS,
  HIST_MS,
  IDLE_TTL_MS,
  OGO_USAGE_URL,
  OGO_WINDOWS,
  SAMPLE_MS,
  USAGE_TTL_MS,
  FREE_POOL_MODELS,
  agentDir,
  freePoolOf,
  ipStateFile,
  rateLimitFile,
  sessionsDir,
  stateFile,
  statePidFile,
  windowsBase,
  writeAtomic,
  type CollectorMode,
  type IpState,
  type RateLimitEvent,
  type Sample,
  type SharedState,
  type WindowUsage,
} from "./shared.ts";

const [, , providerArg, modelArg, keyArg, modeArg] = process.argv;
const provider = providerArg ?? "";
const model = modelArg ?? "";
const key = keyArg ?? "";
const mode: CollectorMode = modeArg === "free" ? "free" : "plan";
// Bucket identity: group-A free models share one pool, others stand alone.
const pool = freePoolOf(model);

// -- opencode-go plan quota (mode === "plan") --
let usageCache: Record<string, WindowUsage> | null = null;
let usageFetchedAt = 0;
let usageInFlight = false;
const usageHist: Record<string, Sample[]> = {};

// prime-agent's own store only. opencode keeps a separate auth.json that drifts
// from this one, and reading it reports the quota of whatever key opencode holds.
const planApiKey = (): string | null => {
  if (process.env.OPENCODE_API_KEY) return process.env.OPENCODE_API_KEY;
  try {
    return JSON.parse(readFileSync(join(agentDir(), "auth.json"), "utf-8"))[provider]?.key ?? null;
  } catch {
    return null;
  }
};

const fetchUsage = async (force = false): Promise<boolean> => {
  if (usageInFlight || (!force && Date.now() - usageFetchedAt < USAGE_TTL_MS))
    return false;
  const apiKey = planApiKey();
  if (!apiKey) return false;
  usageInFlight = true;
  usageFetchedAt = Date.now();
  try {
    const res = await fetch(OGO_USAGE_URL, {
      headers: { authorization: `Bearer ${apiKey}` },
    });
    if (res.ok) {
      usageCache = ((await res.json()) as any)?.usage ?? null;
      recordSample();
    }
  } catch {
    // Offline: keep the last cache and stay quiet.
  } finally {
    usageInFlight = false;
  }
  return true;
};

// Samples feed the burn rate; a percent drop means the window reset.
const recordSample = () => {
  if (!usageCache) return;
  const now = Date.now();
  for (const [wkey] of OGO_WINDOWS) {
    const w = usageCache[wkey];
    if (!w) continue;
    const hist = (usageHist[wkey] ??= []);
    const prev = hist[hist.length - 1];
    if (prev && w.percent < prev.percent) hist.length = 0;
    hist.push({ t: now, percent: w.percent });
    while (hist.length > 2 && now - hist[0].t > HIST_MS) hist.shift();
  }
};

// -- free "opencode" request quota (mode === "free") --
// Bucket per (IP x model x opencode-UA-class) over ~5h; an IP change resets it.
let freeCache = 0;
let freeFetchedAt = 0;
let freeInFlight = false;
let freeIp: string | null = null;
let freeIpChangedAt = 0;

try {
  const s = JSON.parse(readFileSync(ipStateFile(pool), "utf8")) as IpState;
  if (typeof s?.ip === "string") freeIp = s.ip;
  if (typeof s?.changedAt === "number") freeIpChangedAt = s.changedAt;
} catch {
  // no prior state: count everything in the window until a change is observed
}

const fetchIp = async (): Promise<void> => {
  try {
    const res = await fetch("https://api.ipify.org?format=json", {
      signal: AbortSignal.timeout(5000),
    });
    const ip = ((await res.json()) as any)?.ip;
    if (typeof ip !== "string" || ip === freeIp) return;
    if (freeIp == null) {
      freeIp = ip; // first observation: adopt without marking a change
      return;
    }
    freeIp = ip;
    freeIpChangedAt = Date.now();
    const st: IpState = { ip, changedAt: freeIpChangedAt };
    writeAtomic(ipStateFile(pool), JSON.stringify(st));
  } catch {
    // offline/unreachable: keep the previous view
  }
};

// Count assistant messages of every model in this pool since the 5h floor and
// the last IP change, whichever is later. Each assistant message is one call.
const poolModels = FREE_POOL_MODELS(pool);
const freeRequests = (): number => {
  const floor = Math.max(Date.now() - FREE_WINDOW_MS, freeIpChangedAt);
  try {
    let count = 0;
    for (const f of readdirSync(sessionsDir())) {
      if (!f.endsWith(".jsonl")) continue;
      const file = join(sessionsDir(), f);
      try {
        if (statSync(file).mtimeMs < floor) continue; // untouched within window
      } catch {
        continue;
      }
      for (const line of readFileSync(file, "utf8").split("\n")) {
        if (!line.includes(`"provider":"opencode"`)) continue;
        if (!poolModels.some((mm) => line.includes(`"model":"${mm}"`))) continue;
        try {
          const o = JSON.parse(line);
          const m = o?.message;
          if (
            m?.role === "assistant" &&
            m?.provider === "opencode" &&
            poolModels.includes(m?.model) &&
            Date.parse(o.timestamp) >= floor
          )
            count++;
        } catch {
        }
      }
    }
    return count;
  } catch {
    return -1; // sessions unreadable: keep last cache and stay quiet
  }
};

const fetchFree = async (force = false): Promise<boolean> => {
  if (freeInFlight || (!force && Date.now() - freeFetchedAt < USAGE_TTL_MS))
    return false;
  freeInFlight = true;
  freeFetchedAt = Date.now();
  const c = freeRequests();
  freeInFlight = false;
  if (c >= 0) freeCache = c;
  return true;
};

// Windows record raw 429s (after_provider_response). Only one hit on the
// current egress IP counts; Retry-After gives the exact reopen moment.
const clearRateLimit = () => {
  try {
    unlinkSync(rateLimitFile(pool));
  } catch {
  }
};
const freeLimitState = (): { limited: boolean; resetAt: number | null } => {
  try {
    const rl = JSON.parse(readFileSync(rateLimitFile(pool), "utf8")) as RateLimitEvent;
    const stale =
      !rl ||
      typeof rl.at !== "number" ||
      rl.at < freeIpChangedAt || // hit on a previous IP
      (rl.retryAfter != null && rl.at + rl.retryAfter * 1000 <= Date.now());
    if (stale) {
      clearRateLimit(); // older than the limit's own window: drop the event
      return { limited: false, resetAt: null };
    }
    return {
      limited: true,
      resetAt: rl.retryAfter != null ? rl.at + rl.retryAfter * 1000 : null,
    };
  } catch {
    return { limited: false, resetAt: null };
  }
};

// -- publishing + lifecycle --
const publish = () => {
  const lim = mode === "free" ? freeLimitState() : null;
  const state: SharedState = {
    provider,
    model,
    quota: {
      usage: mode === "plan" ? usageCache : null,
      hist: mode === "plan" ? usageHist : {},
      fetchedAt: usageFetchedAt,
    },
    free:
      mode === "free"
        ? {
            pool,
            cache: freeCache,
            since: freeIpChangedAt,
            limited: lim!.limited,
            resetAt: lim!.resetAt,
            ip: freeIp,
            fetchedAt: freeFetchedAt,
          }
        : null,
    updatedAt: Date.now(),
  };
  writeAtomic(stateFile(key), JSON.stringify(state));
};

// Any window that refreshed a heartbeat within IDLE_TTL counts as a live user.
const pruneStale = () => {
  const dir = join(windowsBase(), key);
  if (!existsSync(dir)) return;
  for (const f of readdirSync(dir)) {
    const p = join(dir, f);
    try {
      if (Date.now() - statSync(p).mtimeMs > IDLE_TTL_MS) unlinkSync(p);
    } catch {
    }
  }
};
const anyLiveWindow = (): boolean => {
  const dir = join(windowsBase(), key);
  if (!existsSync(dir)) return false;
  const now = Date.now();
  for (const f of readdirSync(dir)) {
    try {
      if (now - statSync(join(dir, f)).mtimeMs < IDLE_TTL_MS) return true;
    } catch {
    }
  }
  return false;
};

async function main(): Promise<void> {
  writeAtomic(statePidFile(key), String(process.pid));
  // Only remove the pid file if it is still ours: a window may have replaced
  // us (fresh spawn) after we froze, and the replacement's pid file must live.
  const cleanup = () => {
    try {
      if (readFileSync(statePidFile(key), "utf8") === String(process.pid))
        unlinkSync(statePidFile(key));
    } catch {
    }
  };
  process.on("exit", cleanup);
  process.on("SIGTERM", () => process.exit(0));
  process.on("SIGINT", () => process.exit(0));

  // Publish immediately so a just-spawned collector shows data fast.
  if (mode === "free") await fetchIp();
  if (mode === "plan") await fetchUsage(true);
  if (mode === "free") await fetchFree(true);
  publish();

  setInterval(async () => {
    pruneStale();
    // A window respawned us after a freeze (state went stale while our pid was
    // still alive); the newer collector owns the key now, so bow out.
    try {
      if (readFileSync(statePidFile(key), "utf8") !== String(process.pid))
        return process.exit(0);
    } catch {
      // pid file missing/unreadable: not ours anymore, or still starting up
    }
    if (!anyLiveWindow()) {
      process.exit(0); // nothing needs this provider/model anymore
    }
    if (mode === "plan") {
      if (await fetchUsage()) publish();
    }
    if (mode === "free") {
      await fetchIp();
      if (await fetchFree()) publish();
    }
    // Republish anyway so updatedAt stays fresh while the TTL suppresses fetches.
    publish();
  }, SAMPLE_MS);
}

void main();
