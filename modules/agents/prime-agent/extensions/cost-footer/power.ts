// GPU electricity metering for locally-served models, shown in the cost
// footer; build-time constants powerCard..powerProviders get substituted.

import {
  closeSync,
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  readdirSync,
  renameSync,
  linkSync,
  statSync,
  unlinkSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { randomUUID } from "node:crypto";
import { dirname, join } from "node:path";
import { costDir, writeAtomic } from "./shared.ts";
import { aiReading, LEASE_DIR, WATTMETER_NOW, type AiReading } from "./wattmeter.ts";

// "" autodetects the first GPU exposing an average-power sensor.
const CARD = "@powerCard@";
const RATE_PER_KWH = Number("@powerRate@");
const IDLE_WATTS = Number("@powerIdle@");
export const CURRENCY = "@powerCurrency@";
const PROVIDERS = "@powerProviders@".split(",").filter(Boolean);

const SAMPLE_MS = 500;
// A request that never reports completion must not integrate forever.
const MAX_REQUEST_MS = 30 * 60_000;
// Energy is bucketed so rolling windows can be summed cheaply; 15 minutes keeps
// a month of history to a few thousand entries.
const BUCKET_MS = 15 * 60_000;
const RETAIN_MS = 32 * 86_400_000;
// Labels like "1s", "15m", "24h", "30d", "2w", "1M"; the build asserts the format.
// M is a calendar month, so its ms is only the longest span, for sizing buckets.
const UNIT_MS: Record<string, number> = {
  s: 1_000,
  m: 60_000,
  h: 3_600_000,
  d: 86_400_000,
  w: 7 * 86_400_000,
  M: 31 * 86_400_000,
};
// [label, longest span in ms, calendar months (0 for fixed spans)]
const WINDOWS: [string, number, number][] = "@powerWindows@"
  .split(",")
  .filter(Boolean)
  .map((l) => {
    const n = Number(l.slice(0, -1));
    const unit = l.slice(-1);
    return [l, n * (UNIT_MS[unit] ?? NaN), unit === "M" ? n : 0] as [string, number, number];
  })
  .filter(([, ms]) => Number.isFinite(ms) && ms > 0);
const monthsAgo = (now: number, n: number) => {
  const d = new Date(now);
  const day = d.getDate();
  d.setDate(1);
  d.setMonth(d.getMonth() - n);
  // Mar 31 minus one month is the end of February, not Mar 3.
  d.setDate(Math.min(day, new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate()));
  return d.getTime();
};
// Short windows get finer buckets, retained only as long as they need, so the
// straddling edge bucket stays at most a quarter of the window where possible.
const FINE_STEPS = [1_000, 60_000];
const resolutionFor = (ms: number) =>
  ms / 4 >= BUCKET_MS ? BUCKET_MS : ([...FINE_STEPS].reverse().find((r) => r <= ms / 4) ?? FINE_STEPS[0]);
const FINE_RETAIN = new Map<number, number>();
for (const [, ms] of WINDOWS) {
  const r = resolutionFor(ms);
  if (r !== BUCKET_MS) FINE_RETAIN.set(r, Math.max(FINE_RETAIN.get(r) ?? 0, ms + r));
}
const COARSE_RETAIN_MS = Math.max(RETAIN_MS, ...WINDOWS.map(([, ms]) => ms + BUCKET_MS));
// Persisted windows only move on a flush, so a short window needs flushes
// while a long request is still running.
const FLUSH_MS = Math.min(60_000, Math.max(1_000, Math.min(...WINDOWS.map(([, ms]) => ms / 4))));
// A writer that died mid-update must not block every later flush.
const LOCK_STALE_MS = 5_000;

// `buckets` stays the 15-minute tier so older builds still read the file;
// `fine` maps a finer bucket size in ms to its buckets.
type Buckets = Record<string, number>;
type State = { buckets: Buckets; fine: Record<string, Buckets>; total: number };

const stateFile = () => join(costDir(), "power", "energy.json");
const lockPath = () => join(costDir(), "power", "energy.lock");
const claimPath = () => join(costDir(), "power", "sampler.claim");

// Every session (main plus each subagent) gets its own module instance, so
// several samplers must not all integrate the same card; one claim arbitrates.
const CLAIM_STALE_MS = 5_000;
const CLAIM_REFRESH_MS = 2_000;

// Subagent runtimes share one OS process, so a pid cannot identify the
// instance. The token pairs the pid (for liveness) with a per-evaluation value.
const tokenPid = (t: string): number | null => {
  const pid = Number(t.split(":")[0]);
  return Number.isInteger(pid) && pid > 0 ? pid : null;
};

const claimOwner = (): string | null => {
  try {
    const t = readFileSync(claimPath(), "utf8").trim();
    return t.length > 0 ? t : null;
  } catch {
    return null;
  }
};
const isOurs = (self: string) => claimOwner() === self;
const ownerAlive = (token: string): boolean => {
  const pid = tokenPid(token);
  if (pid === null) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (e: any) {
    return e?.code === "EPERM"; // exists, owned by another user
  }
};

// link() is atomic and fails with EEXIST on a race, so two processes replacing
// a dead owner's claim cannot both win; an unlink+create pair could.
const linkClaim = (self: string): boolean => {
  const tmp = `${claimPath()}.${self.replace(/[^A-Za-z0-9]/g, "")}.tmp`;
  try {
    writeFileSync(tmp, self);
    linkSync(tmp, claimPath());
    return true;
  } catch {
    return false;
  } finally {
    try {
      unlinkSync(tmp);
    } catch {
    }
  }
};

const sweepTmp = () => {
  try {
    const dir = dirname(claimPath());
    for (const f of readdirSync(dir)) {
      // Only our own: writeAtomic keeps energy.json temporaries here too.
      const ours = f.startsWith("sampler.claim.") && f.endsWith(".tmp");
      // energy.lock.break and its temporaries: debris from a killed breaker.
      const breakDebris = f.startsWith("energy.lock.break");
      if (!ours && !breakDebris) continue;
      const path = join(dir, f);
      try {
        if (Date.now() - statSync(path).mtimeMs > CLAIM_STALE_MS) unlinkSync(path);
      } catch {
      }
    }
  } catch {
  }
};

// Returns true if this instance now owns sampling.
const takeClaim = (self: string): boolean => {
  const path = claimPath();
  try {
    mkdirSync(dirname(path), { recursive: true });
  } catch {
    return false;
  }
  sweepTmp();
  if (linkClaim(self)) return true;
  // read-check-unlink-link is not atomic as a unit: without the lock, two
  // processes can both observe the same dead owner and both believe they won.
  return (
    withLock(() => {
      try {
        const token = claimOwner();
        if (token === self) return true;
        // A live pid is not enough: pids recycle and the owner heartbeats, so a cold
        // file means nobody is really sampling regardless of the recorded pid.
        if (Date.now() - statSync(path).mtimeMs < CLAIM_STALE_MS) {
          if (token === null || ownerAlive(token)) return false;
        }
        unlinkSync(path);
        return linkClaim(self);
      } catch {
        return false;
      }
    }) === true
  );
};
// Both verify ownership first: an instance whose claim was stolen must not
// refresh, and must never unlink, the new owner's file.
const refreshClaim = (self: string) => {
  try {
    if (!isOurs(self)) return false;
    const now = new Date();
    utimesSync(claimPath(), now, now);
    return true;
  } catch {
    return false;
  }
};
const dropClaim = (self: string) => {
  try {
    if (isOurs(self)) unlinkSync(claimPath());
  } catch {
  }
};

// -- sensor -----------------------------------------------------------------
let sensor: string | null | undefined;
const sensorUnder = (card: string): string | null => {
  const base = `/sys/class/drm/${card}/device/hwmon`;
  try {
    for (const h of readdirSync(base)) {
      const p = join(base, h, "power1_average");
      if (existsSync(p)) return p;
    }
  } catch {
  }
  return null;
};
export const resolveSensor = (): string | null => {
  if (sensor !== undefined) return sensor;
  sensor = null;
  if (CARD) {
    sensor = sensorUnder(CARD);
    return sensor;
  }
  try {
    for (const card of readdirSync("/sys/class/drm").sort()) {
      if (!/^card\d+$/.test(card)) continue;
      const found = sensorUnder(card);
      if (found) {
        sensor = found;
        break;
      }
    }
  } catch {
  }
  return sensor;
};
const readWatts = (): number | null => {
  const path = resolveSensor();
  if (!path) return null;
  try {
    const uw = Number(readFileSync(path, "utf8").trim());
    return Number.isFinite(uw) ? uw / 1e6 : null;
  } catch {
    return null;
  }
};

// Fresh only while the wattmeter service runs; cached on mtime, freshness checked per call.
let snapText = "";
let snapMtime = -1;
const wattmeterAi = (): AiReading | null => {
  try {
    const m = statSync(WATTMETER_NOW).mtimeMs;
    if (m !== snapMtime) {
      snapText = readFileSync(WATTMETER_NOW, "utf8");
      snapMtime = m;
    }
  } catch {
    return null;
  }
  return aiReading(snapText, Date.now());
};

// Cached against the file's mtime so repeated renders are free, while an
// external write by another window is still picked up.
let cache: State | null = null;
let cacheMtime = -1;

// null means "state unavailable"; a caller must never write over that, or a
// transient error would erase the history.
const readState = (): State | null => {
  let mtime = -1;
  try {
    mtime = statSync(stateFile()).mtimeMs;
    if (cache && mtime === cacheMtime) return cache;
  } catch {
    // fall through to the read, which will classify the error properly
  }
  try {
    const d = JSON.parse(readFileSync(stateFile(), "utf8"));
    // Keys are bucket indices and values are joules; anything else would
    // survive pruning forever and poison the sums by string-concatenating.
    const clean = (raw: unknown): Buckets => {
      const out: Buckets = {};
      for (const [k, v] of Object.entries(raw && typeof raw === "object" ? raw : {})) {
        if (Number.isFinite(Number(k)) && typeof v === "number" && Number.isFinite(v) && v >= 0) {
          out[k] = v;
        }
      }
      return out;
    };
    const fine: Record<string, Buckets> = {};
    for (const [r, b] of Object.entries(d?.fine && typeof d.fine === "object" ? d.fine : {})) {
      if (FINE_STEPS.includes(Number(r))) fine[r] = clean(b);
    }
    cache = { buckets: clean(d?.buckets), fine, total: Number.isFinite(d?.total) ? d.total : 0 };
    cacheMtime = mtime;
    return cache;
  } catch (e: any) {
    if (e?.code === "ENOENT") {
      cache = { buckets: {}, fine: {}, total: 0 };
      cacheMtime = -1;
      return cache;
    }
    // A real errno (EACCES, EMFILE, EIO) means the file is probably fine and
    // will read next time. Only unparseable content earns a rename.
    if (e?.code) return null;
    try {
      // Keep one copy for inspection rather than accumulating one per incident.
      try {
        for (const f of readdirSync(dirname(stateFile()))) {
          if (f.endsWith(".corrupt")) unlinkSync(join(dirname(stateFile()), f));
        }
      } catch {
      }
      renameSync(stateFile(), `${stateFile()}.${Date.now()}.corrupt`);
      cache = { buckets: {}, fine: {}, total: 0 };
      cacheMtime = -1;
      return cache;
    } catch {
      return null;
    }
  }
};

// The update is read-modify-write, so two prime-agent processes metering at
// once would otherwise lose each other's contributions.
const withLock = <T>(fn: () => T): T | null => {
  const lock = lockPath();
  try {
    mkdirSync(dirname(lock), { recursive: true });
  } catch {
    return null;
  }
  let fd: number;
  try {
    fd = openSync(lock, "wx");
  } catch (e: any) {
    if (e?.code !== "EEXIST") return null;
    // Held by someone live: nothing to break. Checked first so a contended-but-
    // healthy lock never touches the breaker.
    try {
      if (Date.now() - statSync(lock).mtimeMs < LOCK_STALE_MS) return null;
    } catch {
      return null;
    }
    // Breaking a stale lock is itself a race; link() decides which process may
    // replace the dead lock with its own.
    const breaker = `${lock}.break`;
    // A breaker outliving its creator would wedge every future break, and with
    // it all persistence and all claim reclamation, permanently.
    try {
      if (Date.now() - statSync(breaker).mtimeMs > LOCK_STALE_MS) unlinkSync(breaker);
    } catch {
      // absent, or someone else got there first
    }
    const breakTmp = `${breaker}.${process.pid}.${Math.random().toString(36).slice(2)}`;
    let breaking = false;
    try {
      writeFileSync(breakTmp, String(process.pid));
      linkSync(breakTmp, breaker);
      breaking = true;
    } catch {
      breaking = false;
    } finally {
      try {
        unlinkSync(breakTmp);
      } catch {
      }
    }
    if (!breaking) return null;
    try {
      // Re-check under the breaker: the previous holder may have released it
      // between our stat and our claim.
      if (Date.now() - statSync(lock).mtimeMs < LOCK_STALE_MS) return null;
      unlinkSync(lock); // left by a crashed writer
      fd = openSync(lock, "wx");
    } catch {
      return null;
    } finally {
      try {
        unlinkSync(breaker);
      } catch {
      }
    }
  }
  try {
    return fn();
  } finally {
    closeSync(fd);
    try {
      unlinkSync(lock);
    } catch {
    }
  }
};

// Returns false when the energy could not be persisted, so the caller keeps it
// pending rather than dropping it.
const flushEnergy = (joules: number): boolean => {
  if (joules <= 0) return true;
  const done = withLock(() => {
    cacheMtime = -1; // another writer may have moved since our last read
    const s = readState();
    if (!s) return false;
    const now = Date.now();
    const record = (b: Buckets, size: number, retain: number) => {
      const key = String(Math.floor(now / size));
      b[key] = (b[key] ?? 0) + joules;
      const cutoff = Math.floor((now - retain) / size);
      for (const k of Object.keys(b)) {
        if (Number(k) < cutoff) delete b[k];
      }
    };
    record(s.buckets, BUCKET_MS, COARSE_RETAIN_MS);
    // Tiers no configured window reads are dropped rather than kept growing.
    const fine: Record<string, Buckets> = {};
    for (const [size, retain] of FINE_RETAIN) {
      fine[size] = s.fine[size] ?? {};
      record(fine[size], size, retain);
    }
    s.fine = fine;
    s.total += joules;
    try {
      writeAtomic(stateFile(), JSON.stringify(s));
      cacheMtime = -1;
      return true;
    } catch {
      return false;
    }
  });
  return done === true;
};

const windowJoules = (s: State, ms: number, months: number): number => {
  const size = resolutionFor(ms);
  const buckets = size === BUCKET_MS ? s.buckets : (s.fine[size] ?? {});
  const now = Date.now();
  const start = months > 0 ? monthsAgo(now, months) : now - ms;
  // Floored so the bucket straddling the window edge counts whole; dropping it
  // would make "1h" mean anywhere between 45 and 60 minutes.
  const cutoff = Math.floor(start / size);
  let sum = 0;
  for (const [k, v] of Object.entries(buckets)) {
    if (Number(k) >= cutoff) sum += v;
  }
  return sum;
};

// -- public surface ---------------------------------------------------------
const costOf = (joules: number) => (joules / 3.6e6) * RATE_PER_KWH;

// Exact, O(#providers): getAll() lists built-ins before custom models, so a
// shared id would otherwise resolve to the wrong provider and disable metering.
export const isMetered = (ctx: any, id?: unknown): boolean => {
  if (typeof id === "string" && id) {
    try {
      if (PROVIDERS.some((p) => !!ctx?.modelRegistry?.find?.(p, id))) return true;
    } catch {
      // registry unavailable; fall through to the session's model
    }
  }
  const p = ctx?.model?.provider;
  return !!p && PROVIDERS.includes(p);
};
export const isMeteredProvider = (provider?: unknown) =>
  typeof provider === "string" && PROVIDERS.includes(provider);

export type PowerSnapshot = {
  watts: number;
  sampling: boolean;
  currency: string;
  // [label, cost]: wattmeter's figures while it runs, else this meter's own.
  windows: [string, number][];
};

// True when metering is configured at all: without it the footer would take
// the electricity branch on a machine that merely has an AMD card.
export const meteringEnabled = () =>
  PROVIDERS.length > 0 && (resolveSensor() !== null || wattmeterAi() !== null);

export function createPowerMeter(onRepaint: () => void) {
  // Per meter, not per module evaluation: two meters sharing one evaluation
  // would both match a module-level token and both sample the same card.
  const INSTANCE = `${process.pid}:${randomUUID()}`;
  let sampler: ReturnType<typeof setInterval> | null = null;
  let lastSampleAt = 0;
  let startedAt = 0;
  let wattsNow = 0;
  let pendingJoules = 0;
  // One counter, not per-session: the runtime never emits both ends of a
  // subagent request from one session, so per-session keys stay unbalanced.
  let inFlight = 0;
  let idleSamples = 0;
  // What the footer last showed, so a repaint only happens when a visible cell
  // would differ; refresh() walks the entire session branch.
  let shownWatts = -1;
  let owned = false;
  let lastClaimRefresh = 0;
  let lastFlush = 0;
  const lease = join(LEASE_DIR, INSTANCE.replace(/[^A-Za-z0-9]/g, ""));
  let viaWattmeter = false;
  const touchLease = () => {
    const now = new Date();
    try {
      utimesSync(lease, now, now);
    } catch {
      try {
        // Fails while the service is down, because systemd removes /run/wattmeter.
        writeFileSync(lease, "");
      } catch {
      }
    }
  };
  const dropLease = () => {
    try {
      unlinkSync(lease);
    } catch {
    }
  };

  const sample = () => {
    const ai = wattmeterAi();
    viaWattmeter = ai !== null;
    if (ai) {
      // wattmeter integrates this energy; accruing it here as well would count it twice.
      touchLease();
      wattsNow = ai.watts;
      lastSampleAt = Date.now();
      return;
    }
    if (!owned) return;
    const now2 = Date.now();
    if (now2 - lastClaimRefresh >= CLAIM_REFRESH_MS) {
      lastClaimRefresh = now2;
      // Losing the claim mid-sample means another process took over; stop
      // accruing rather than double-counting the same watts with it.
      if (!refreshClaim(INSTANCE)) {
        owned = false;
        return;
      }
    }
    const w = readWatts();
    const now = Date.now();
    if (w != null) {
      // Clamp so a dip below the idle floor cannot subtract energy already
      // accounted for.
      const marginal = Math.max(0, w - IDLE_WATTS);
      wattsNow = marginal;
      const joules = marginal * ((now - lastSampleAt) / 1000);
      pendingJoules += joules;
    }
    lastSampleAt = now;
    if (now - lastFlush >= FLUSH_MS) {
      lastFlush = now;
      if (flushEnergy(pendingJoules)) pendingJoules = 0;
    }
  };
  const settle = () => {
    if (inFlight > 0 || !sampler) return;
    clearInterval(sampler);
    sampler = null;
    sample(); // bank the final partial interval
    dropLease();
    wattsNow = 0;
    shownWatts = -1;
    if (flushEnergy(pendingJoules)) pendingJoules = 0;
    if (owned) {
      owned = false;
      dropClaim(INSTANCE);
    }
  };
  const drain = () => {
    inFlight = 0;
    settle();
  };
  const tick = (isIdle: () => boolean) => {
    try {
      sample();
      // Only the owner has anything that changes at this rate, and only when
      // the rounded figure actually moves.
      const rounded = Math.round(wattsNow);
      if ((owned || viaWattmeter) && rounded !== shownWatts) {
        shownWatts = rounded;
        onRepaint();
      }
      // Catches a request whose completion never reaches us: a side question fires
      // before_provider_request but its message events go elsewhere.
      if (isIdle()) {
        if (++idleSamples >= 2) return drain();
      } else {
        idleSamples = 0;
      }
      if (Date.now() - startedAt > MAX_REQUEST_MS) drain();
    } catch {
      // A ctx whose runner was invalidated throws on property access, and an
      // uncaught throw from a timer would take the TUI down.
      drain();
    }
  };

  return {
    start(isIdle: () => boolean) {
      inFlight += 1;
      if (sampler || (!resolveSensor() && wattmeterAi() === null)) return;
      // Without the claim this session still renders, it just does not
      // double-count the card another session is already measuring.
      owned = takeClaim(INSTANCE);
      lastClaimRefresh = Date.now();
      idleSamples = 0;
      lastSampleAt = Date.now();
      startedAt = lastSampleAt;
      lastFlush = lastSampleAt;
      sampler = setInterval(() => tick(isIdle), SAMPLE_MS);
      sampler.unref?.();
    },
    stop() {
      if (inFlight > 0) inFlight -= 1;
      settle();
    },
    drain,
    flushPending() {
      if (pendingJoules > 0 && flushEnergy(pendingJoules)) pendingJoules = 0;
    },
    snapshot(): PowerSnapshot {
      const ai = wattmeterAi();
      if (ai) {
        return {
          watts: sampler !== null ? ai.watts : 0,
          sampling: sampler !== null,
          currency: ai.currency,
          windows: ai.windows,
        };
      }
      const s = readState();
      return {
        watts: wattsNow,
        sampling: sampler !== null && owned,
        currency: CURRENCY,
        // Persisted only: adding this instance's unflushed joules would make
        // two windows disagree about a machine-wide number.
        windows: s
          ? WINDOWS.map(([label, ms, months]) => [label, costOf(windowJoules(s, ms, months))] as [string, number])
          : [],
      };
    },
  };
}
