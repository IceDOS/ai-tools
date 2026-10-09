// Shared cost-footer data model + file-backed state. One collector per
// (provider, model) publishes files every open window reads, keeping sync.

import {
  closeSync,
  fsyncSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  writeSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

export const OGO_PROVIDER = "opencode-go";
// opencode Zen "go" plan quota endpoint; usage keys per window.
export const OGO_USAGE_URL = "https://opencode.ai/zen/go/v1/usage";
export const OGO_WINDOWS: [string, string][] = [
  ["rolling", "5h"],
  ["weekly", "wk"],
  ["monthly", "mo"],
];
// opencode Zen free models ("opencode" provider): measured 2026-08-31, cap
// per (IP x model x opencode-UA-class) over 5h; the count is local, not authoritative.
export const FREE_WINDOW_MS = 5 * 3600 * 1000;
// Burned 2026-08-31: each list below shares ONE ~1000 req / 5h bucket per
// (IP x opencode-UA-class); the group-A burn rate-limited all five together.
export const FREE_MODEL_POOLS: Record<string, string[]> = {
  shared: [
    "big-pickle",
    "deepseek-v4-flash-free",
    "mimo-v2.5-free",
    "ling-3.0-flash-fin-free",
    "muse-spark-1.2-contributor-free",
  ],
  nemotron: ["nemotron-3-ultra-free", "nemotron-3.5-lightning-free"],
};
export const freePoolOf = (model: string): string =>
  Object.entries(FREE_MODEL_POOLS).find(([, ms]) => ms.includes(model))?.[0] ??
  model;
export const FREE_POOL_MODELS = (pool: string): string[] =>
  FREE_MODEL_POOLS[pool] ?? [pool];
// Measured caps per pool; laguna's tiny pool has no Retry-After and 503s when
// upstream is out of seats, so its number is approximate.
export const FREE_POOL_CAPS: Record<string, number> = {
  shared: 1000,
  nemotron: 1000,
  "laguna-s-2.1-free": 30,
};
export const FREE_CAP_DEFAULT = 1000;
// Provider percentages are rounded, so polling each event buys nothing.
export const USAGE_TTL_MS = 60_000;
// Burn-rate estimates use percent samples from the last 15 minutes.
export const HIST_MS = 15 * 60_000;
export const SAMPLE_MS = 30_000;
// A collector exits when no window's heartbeat is fresher than this.
export const IDLE_TTL_MS = 2 * 60_000;
// A live pid whose published state is older than this is a reused-pid ghost.
export const RUNNING_TTL_MS = 3 * 60_000;
// A spawn lock older than this was left by a crashed spawner and may be reclaimed.
export const LOCK_STALE_MS = 15_000;

export type WindowUsage = { percent: number; status: string; resetsAt?: string };
export type Sample = { t: number; percent: number };
export type QuotaState = {
  usage: Record<string, WindowUsage> | null;
  hist: Record<string, Sample[]>;
  fetchedAt: number;
};
export type FreeState = {
  // Which bucket this row accounts for ("shared" or the model id).
  pool: string;
  // Requests since `since` (last observed IP change), from local sessions only.
  cache: number;
  since: number;
  limited: boolean;
  // Epoch ms the bucket reopens, taken from the 429's Retry-After header.
  resetAt: number | null;
  ip: string | null;
  fetchedAt: number;
};
// A window records the raw 429 here when the provider rate-limits a free model.
export type RateLimitEvent = { at: number; retryAfter: number | null };
// Last observed egress IP, so a VPN/server change resets the per-IP bucket view.
export type IpState = { ip: string; changedAt: number };
export type CollectorMode = "plan" | "free";
export type SharedState = {
  provider: string;
  model: string;
  quota: QuotaState;
  free: FreeState | null;
  updatedAt: number;
};

export const agentDir = () =>
  process.env.PRIME_AGENT_CODING_AGENT_DIR ?? join(homedir(), ".config", "prime-agent");
export const sessionsDir = () => join(agentDir(), "sessions");
export const costDir = () => join(agentDir(), "cost-footer");
export const stateDir = () => join(costDir(), "state");
export const windowsBase = () => join(costDir(), "windows");
export const lockBase = () => join(costDir(), "locks");

const slug = (s: string) =>
  s.replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^_+|_+$/g, "");
export const keyOf = (provider: string, model: string) =>
  `${slug(provider)}__${slug(model)}`;

export const stateFile = (key: string) => join(stateDir(), `${key}.json`);
export const statePidFile = (key: string) => join(stateDir(), `${key}.pid`);
export const rateLimitFile = (key: string) => join(stateDir(), `${key}.ratelimit.json`);
export const ipStateFile = (key: string) => join(stateDir(), `${key}.ip.json`);
export const lockFile = (key: string) => join(lockBase(), `${key}.lock`);
export const heartbeatFile = (key: string) =>
  join(windowsBase(), key, String(process.pid));
export const collectorScript = () =>
  join(agentDir(), "extensions", "cost-footer", "collector.ts");

export const readState = (key: string): SharedState | null => {
  try {
    return JSON.parse(readFileSync(stateFile(key), "utf8"));
  } catch {
    return null;
  }
};

// tmp + fsync + rename: readers never see a half-written state file. The tmp
// name is per-writer, so concurrent writers cannot collide on one rename.
export const writeAtomic = (path: string, data: string) => {
  mkdirSync(dirname(path), { recursive: true });
  const tmp = `${path}.${process.pid}.${Math.random().toString(36).slice(2, 8)}.tmp`;
  const fd = openSync(tmp, "w");
  try {
    writeSync(fd, data);
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  renameSync(tmp, path);
};
