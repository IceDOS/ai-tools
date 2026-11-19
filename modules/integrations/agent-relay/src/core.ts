const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");
const path = process.getBuiltinModule("node:path") as typeof import("node:path");
const crypto = process.getBuiltinModule("node:crypto") as typeof import("node:crypto");

export type Kind = "pi" | "claude" | "agy" | "script";

export interface SessionRecord {
  id: string;
  kind: Kind;
  pid: number;
  cwd: string;
  name?: string;
  group?: string;
  started: string;
  agent?: Record<string, unknown>;
  // Nearest registered agent above this process, e.g. the pi that spawned an RLM child.
  parent?: ParentRef | null;
}

export interface ParentRef {
  id: string;
  kind: Kind;
  name?: string;
}

export interface Group {
  id: string;
  members: string[];
}

export interface RegisterInput {
  kind: Kind;
  pid: number;
  cwd: string;
  id?: string;
  name?: string;
  agent?: Record<string, unknown>;
}

export interface Limits {
  pairLimit: number;
  rateLimit: number;
  windowMs: number;
  resetMs: number;
  dedupeMs: number;
}

export const DEFAULT_LIMITS: Limits = {
  pairLimit: 10,
  rateLimit: 12,
  windowMs: 60_000,
  resetMs: 600_000,
  dedupeMs: 30_000,
};

export interface RelayOptions {
  root?: string;
  isAlive?: (pid: number) => boolean;
  now?: () => Date;
  ancestorsOf?: (pid: number) => number[];
  limits?: Partial<Limits>;
}

export class RelayError extends Error {}

export const ID_RE = /^[0-9a-f]{5}$/;

export function defaultRoot(env: Record<string, string | undefined> = process.env): string {
  if (env.AGENT_RELAY_ROOT) return env.AGENT_RELAY_ROOT;
  return path.join(env.XDG_RUNTIME_DIR ?? `/run/user/${process.getuid?.() ?? 0}`, "agent-relay");
}

function positive(v: unknown, fallback: number): number {
  return typeof v === "number" && Number.isInteger(v) && v > 0 ? v : fallback;
}

export function readConfig(env: Record<string, string | undefined> = process.env): Limits & { hopLimit: number } {
  const base = env.XDG_CONFIG_HOME ?? path.join(env.HOME ?? "", ".config");
  let cfg: Record<string, unknown> = {};
  try {
    const raw: unknown = JSON.parse(fs.readFileSync(path.join(base, "agent-relay", "config.json"), "utf8"));
    // JSON.parse("null") is a legal file and a TypeError in every reader; only objects carry limits.
    if (raw && typeof raw === "object" && !Array.isArray(raw)) cfg = raw as Record<string, unknown>;
  } catch {}
  const pairLimit = positive(cfg.pairLimit ?? cfg.hopLimit, DEFAULT_LIMITS.pairLimit);
  return {
    hopLimit: pairLimit,
    pairLimit,
    rateLimit: positive(cfg.rateLimit, DEFAULT_LIMITS.rateLimit),
    windowMs: positive(cfg.windowMs, DEFAULT_LIMITS.windowMs),
    resetMs: positive(cfg.resetMs, DEFAULT_LIMITS.resetMs),
    dedupeMs: positive(cfg.dedupeMs, DEFAULT_LIMITS.dedupeMs),
  };
}

export function procState(pid: number): string | undefined {
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
    return stat.slice(stat.lastIndexOf(")") + 2, stat.lastIndexOf(")") + 3);
  } catch {
    return undefined;
  }
}

// kill(0) still succeeds for a zombie, so the state has to say whether it exited.
export function pidAlive(pid: number, state: (pid: number) => string | undefined = procState): boolean {
  try {
    process.kill(pid, 0);
  } catch (e) {
    if ((e as { code?: string }).code !== "EPERM") return false;
  }
  return state(pid) !== "Z";
}

// Walks /proc parent links; hooks and MCP servers find their agent's record this way.
export function ancestors(pid: number = process.pid): number[] {
  const out: number[] = [];
  let cur = pid;
  for (let i = 0; i < 32 && cur > 1; i++) {
    out.push(cur);
    try {
      const stat = fs.readFileSync(`/proc/${cur}/stat`, "utf8");
      cur = Number(stat.slice(stat.lastIndexOf(")") + 2).split(" ")[1]);
    } catch {
      break;
    }
  }
  return out;
}

export function describe(s: SessionRecord): string {
  return `${s.kind} ${s.id}${s.name ? ` "${s.name}"` : ""}`;
}

export const hex = (n: number): string => crypto.randomBytes(Math.ceil(n / 2)).toString("hex").slice(0, n);

export const hashText = (text: string): string =>
  crypto.createHash("sha256").update(text, "utf8").digest("hex").slice(0, 16);

export type MsgState = "queued" | "delivered" | "read" | "superseded" | "withheld";

export interface Message {
  v: 1;
  id: string;
  short?: string;
  from: { id: string; kind: Kind; name?: string; parent?: ParentRef | null; onBehalfOf?: ParentRef };
  to: string;
  group?: string;
  hop: number;
  ts: string;
  text: string;
  reply?: boolean;
  state?: MsgState;
  stateAt?: string;
  inReplyTo?: string;
  supersedes?: string[];
  cancels?: string[];
  supersededBy?: string;
  notice?: { reason: string; preview: string; bytes: number };
}

export interface Watcher {
  close(): void;
  drain(): Promise<void>;
}

export interface SentRef {
  to: string;
  id: string;
  short: string;
}

export interface SendOptions {
  inReplyTo?: string;
  supersedes?: string[];
  cancels?: string[];
}

export interface SendResult {
  targets: string[];
  msgs: SentRef[];
  deduped?: SentRef;
  withheld?: { to: string; reason: string }[];
}

export interface StatusRow {
  id: string;
  short: string;
  to: string;
  ts: string;
  state: MsgState;
  stateAt?: string;
  reason?: string;
}

// Sender-side ledger row. Survives removal of the inbox file, so relay_status can answer later.
interface LedgerRow {
  v: 1;
  id: string;
  short: string;
  to: string;
  ts: string;
  state: MsgState;
  stateAt?: string;
  h: string;
  len: number;
  notice?: true;
  reason?: string;
}

interface PairState {
  n: number;
  lastAt: string;
  recent: number[];
}

const LEDGER_MS = 24 * 60 * 60 * 1000;
// A lock older than this belongs to a writer that died; stealing it beats deadlocking.
const LOCK_MS = 2000;
// States only ever advance, so rank decides whether a transition is a rewrite of history.
const STATE_RANK: Record<MsgState, number> = {
  queued: 0,
  withheld: 1,
  superseded: 1,
  delivered: 2,
  read: 3,
};
// States a row can rest in: nothing is waiting on it any more, so 24h of stateAt is enough to forget.
const TERMINAL_STATES = new Set<MsgState>(["read", "superseded", "withheld", "delivered"]);
const SHORT_RE = /^[0-9a-f]{4,6}$/;

export const RELAY_PREFIX = "[relay from ";
export const MAX_TEXT_BYTES = 64 * 1024;

export function capText(text: string, maxBytes = MAX_TEXT_BYTES): string {
  const buf = Buffer.from(text, "utf8");
  if (buf.length <= maxBytes) return text;
  let end = maxBytes;
  while (end > 0 && (buf[end] & 0xc0) === 0x80) end--;
  const cut = buf.subarray(0, end).toString("utf8");
  return `${cut}\n[truncated ${buf.length - Buffer.byteLength(cut, "utf8")} bytes]`;
}

const clock = (ts: string): string => (ts ? ts.slice(11, 19) : "");

export function formatMessage(m: Message): string {
  const stamp = m.short ? `, msg ${m.short}${m.ts ? ` ${clock(m.ts)}` : ""}` : "";
  const flags = [
    m.inReplyTo ? `reply to ${m.inReplyTo}` : "",
    m.supersedes?.length ? `replaces ${m.supersedes.join(" ")}` : "",
    m.cancels?.length ? `cancels ${m.cancels.join(" ")}` : "",
    m.notice ? "withheld" : "",
  ].filter(Boolean);
  const extra = flags.length ? `, ${flags.join(", ")}` : "";
  const body = m.notice ? `${m.text}\n(withheld text: "${m.notice.preview}" ${m.notice.bytes} bytes)` : m.text;
  const back = (to: string) => `\n(reply with relay_send to="${to}"${m.short ? ` in_reply_to="${m.short}"` : ""})`;
  if (m.from.kind === "script") {
    const who = m.from.name ? `"${m.from.name}"` : m.from.id;
    const behalf = m.from.onBehalfOf ? ` on behalf of ${m.from.onBehalfOf.kind} ${m.from.onBehalfOf.id}` : "";
    // A script that asked a question keeps its own id, one acting for an agent answers to that agent.
    const reply = m.reply || m.from.onBehalfOf ? back(m.reply ? m.from.id : (m.from.onBehalfOf?.id ?? m.from.id)) : "";
    return `${RELAY_PREFIX}script ${who}${behalf}${stamp}${extra}]\n${body}${reply}`;
  }
  const name = m.from.name ? ` "${m.from.name}"` : "";
  const p = m.from.parent;
  const parent = p ? `, parent ${p.kind} ${p.id}${p.name ? ` "${p.name}"` : ""}` : "";
  return `${RELAY_PREFIX}${m.from.kind} ${m.from.id}${name}${parent}, hop ${m.hop}${stamp}${extra}]\n${body}${back(m.from.id)}`;
}

let seq = 0;
// Lexically sortable: time, then a per-process counter, then randomness.
function messageId(now: Date): string {
  seq = (seq + 1) % 36 ** 4;
  return now.getTime().toString(36).padStart(10, "0") + seq.toString(36).padStart(4, "0") + hex(4);
}

export class Relay {
  readonly root: string;
  readonly limits: Limits;
  protected isAlive: (pid: number) => boolean;
  protected now: () => Date;
  protected ancestorsOf: (pid: number) => number[];
  protected groupDepth = 0;

  constructor(o: RelayOptions = {}) {
    this.root = o.root ?? defaultRoot();
    this.limits = { ...readConfig(), ...o.limits };
    this.isAlive = o.isAlive ?? pidAlive;
    this.now = o.now ?? (() => new Date());
    this.ancestorsOf = o.ancestorsOf ?? ancestors;
    for (const d of ["sessions", "groups", "inbox", "outbox", "pairs", "shorts"]) {
      fs.mkdirSync(path.join(this.root, d), { recursive: true, mode: 0o700 });
    }
  }

  protected p(...parts: string[]): string {
    return path.join(this.root, ...parts);
  }

  protected readJson<T>(file: string): T | undefined {
    try {
      return JSON.parse(fs.readFileSync(file, "utf8")) as T;
    } catch {
      return undefined;
    }
  }

  // Readers never see a partial file: write a .tmp sibling, then rename.
  protected write(file: string, data: unknown): void {
    const tmp = `${file}.${process.pid}.${hex(6)}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(data), { mode: 0o600 });
    fs.renameSync(tmp, file);
  }

  // Cross-process mutex around one read-modify-write. Gives up rather than fail the caller.
  protected lock<T>(file: string, fn: () => T): T {
    const lk = `${file}.lock`;
    fs.mkdirSync(path.dirname(lk), { recursive: true, mode: 0o700 });
    const start = Date.now();
    let held = false;
    for (;;) {
      try {
        fs.closeSync(fs.openSync(lk, "wx", 0o600));
        held = true;
        break;
      } catch (e) {
        if ((e as { code?: string }).code !== "EEXIST") throw e;
      }
      try {
        if (Date.now() - fs.statSync(lk).mtimeMs > LOCK_MS) fs.rmSync(lk, { force: true });
      } catch {}
      if (Date.now() - start > LOCK_MS) break;
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 1);
    }
    try {
      return fn();
    } finally {
      if (held) fs.rmSync(lk, { force: true });
    }
  }

  protected iso(): string {
    return this.now().toISOString();
  }

  sessions(): SessionRecord[] {
    const out: SessionRecord[] = [];
    for (const f of fs.readdirSync(this.p("sessions"))) {
      if (!f.endsWith(".json")) continue;
      const r = this.readJson<SessionRecord>(this.p("sessions", f));
      if (!r) continue;
      if (this.isAlive(r.pid)) out.push(r);
      else this.forget(r);
    }
    return out;
  }

  get(id: string): SessionRecord | undefined {
    const r = this.readJson<SessionRecord>(this.p("sessions", `${id}.json`));
    if (r && !this.isAlive(r.pid)) {
      this.forget(r);
      return undefined;
    }
    return r;
  }

  findByPid(pids: number[], filter?: ((s: SessionRecord) => boolean) | Kind[]): SessionRecord | undefined {
    const match = typeof filter === "function"
      ? filter
      : Array.isArray(filter)
        ? (s: SessionRecord) => filter.includes(s.kind)
        : undefined;
    const all = this.sessions();
    for (const pid of pids) {
      const r = all.find((s) => s.pid === pid && (!match || match(s)));
      if (r) return r;
    }
    return undefined;
  }

  register(input: RegisterInput): SessionRecord {
    const all = this.sessions();
    const existing = all.find((s) => s.pid === input.pid && s.kind === input.kind);
    if (existing) {
      return this.update(existing.id, {
        cwd: input.cwd,
        ...(input.name ? { name: input.name } : {}),
        ...(input.agent ? { agent: { ...existing.agent, ...input.agent } } : {}),
      });
    }
    const taken = new Set(all.map((s) => s.id));
    let id = input.id && ID_RE.test(input.id) && !taken.has(input.id) ? input.id : "";
    while (!id || taken.has(id)) id = hex(5);
    const above = this.ancestorsOf(input.pid).filter((pid) => pid !== input.pid);
    const p = this.findByPid(above, (s) => s.kind !== "script");
    const rec: SessionRecord = {
      id,
      kind: input.kind,
      pid: input.pid,
      cwd: input.cwd,
      started: this.iso(),
      parent: p ? { id: p.id, kind: p.kind, ...(p.name ? { name: p.name } : {}) } : null,
      ...(input.name ? { name: input.name } : {}),
      ...(input.agent ? { agent: input.agent } : {}),
    };
    fs.mkdirSync(this.p("inbox", id), { recursive: true, mode: 0o700 });
    this.write(this.p("sessions", `${id}.json`), rec);
    return rec;
  }

  update(id: string, patch: Partial<SessionRecord>): SessionRecord {
    this.self(id);
    const file = this.p("sessions", `${id}.json`);
    return this.lock(file, () => {
      const cur = this.readJson<SessionRecord>(file);
      if (!cur) throw new RelayError(`session ${id} is not registered`);
      const next = { ...cur, ...patch, id: cur.id };
      this.write(file, next);
      return next;
    });
  }

  // Merges into agent instead of replacing it, so a hook's busy flag and a watcher's heartbeat
  // land in one record rather than clobbering each other.
  updateAgent(id: string, patch: Record<string, unknown>): SessionRecord {
    this.self(id);
    const file = this.p("sessions", `${id}.json`);
    return this.lock(file, () => {
      const cur = this.readJson<SessionRecord>(file);
      if (!cur) throw new RelayError(`session ${id} is not registered`);
      const next = { ...cur, agent: { ...cur.agent, ...patch } };
      this.write(file, next);
      return next;
    });
  }

  unregister(id: string): void {
    const r = this.readJson<SessionRecord>(this.p("sessions", `${id}.json`));
    if (r) this.forget(r);
  }

  protected forget(r: SessionRecord): void {
    this.abandon(r);
    fs.rmSync(this.p("sessions", `${r.id}.json`), { force: true });
    fs.rmSync(this.p("inbox", r.id), { recursive: true, force: true });
    // The outbox ledger and the burst counters outlive the session: a message it sent may still sit
    // in a live peer's inbox, and whoever settles it later must find the row rather than recreate it.
    if (r.group) this.removeMember(r.group, r.id);
  }

  // Mail still queued for a gone session would read "queued" forever, so the senders learn instead.
  protected abandon(r: SessionRecord): void {
    for (const m of this.pending(r.id)) {
      this.advance(this.ledgerFile(m.from.id, m.id), "withheld", { reason: "receiver exited before delivery" });
    }
  }

  group(gid: string): Group | undefined {
    return this.readJson<Group>(this.p("groups", `${gid}.json`));
  }

  // Raw write without liveness checks; forget() calls this while pruning.
  protected setGroup(id: string, gid: string | undefined): void {
    const file = this.p("sessions", `${id}.json`);
    this.lock(file, () => {
      const r = this.readJson<SessionRecord>(file);
      if (r) this.write(file, { ...r, group: gid });
    });
  }

  protected saveGroup(g: Group): void {
    if (g.members.length < 2) {
      fs.rmSync(this.p("groups", `${g.id}.json`), { force: true });
      for (const m of g.members) this.setGroup(m, undefined);
      return;
    }
    this.write(this.p("groups", `${g.id}.json`), g);
    for (const m of g.members) this.setGroup(m, g.id);
  }

  protected removeMember(gid: string, id: string): void {
    return this.lockGroups(() => {
      const g = this.group(gid);
      if (g) this.saveGroup({ id: gid, members: g.members.filter((m) => m !== id) });
    });
  }

  match(target: string, pool: SessionRecord[]): SessionRecord[] {
    const t = target.trim().toLowerCase();
    if (!t) return [];
    const exact = pool.filter((s) => s.id === t);
    if (exact.length) return exact;
    const byName = pool.filter((s) => s.name?.toLowerCase() === t);
    if (byName.length) return byName;
    return pool.filter((s) => s.id.startsWith(t));
  }

  protected one(target: string, pool: SessionRecord[]): SessionRecord {
    const m = this.match(target, pool);
    if (m.length === 1) return m[0];
    if (m.length === 0) throw new RelayError(`no session matches "${target}"`);
    throw new RelayError(`"${target}" is ambiguous: ${m.map(describe).join(", ")}`);
  }

  protected self(id: string): SessionRecord {
    const r = this.get(id);
    if (!r) throw new RelayError(`session ${id} is not registered`);
    return r;
  }

  // Group membership is read-modify-write over one file, so two joins at once can split a group or
  // drop a member. Every topology change takes this lock first; reentrant inside one process.
  protected lockGroups<T>(fn: () => T): T {
    if (this.groupDepth) {
      this.groupDepth++;
      try {
        return fn();
      } finally {
        this.groupDepth--;
      }
    }
    return this.lock(this.p("groups", "topology.lock"), () => {
      this.groupDepth++;
      try {
        return fn();
      } finally {
        this.groupDepth--;
      }
    });
  }

  link(selfId: string, peer: string): Group {
    return this.lockGroups(() => {
      const all = this.sessions();
      const me = this.self(selfId);
      const other = this.one(peer, all.filter((s) => s.id !== selfId && s.kind !== "script"));
      const ga = me.group ? this.group(me.group) : undefined;
      const gb = other.group ? this.group(other.group) : undefined;
      if (ga && gb && ga.id === gb.id) return ga;
      let g: Group;
      if (ga && gb) {
        g = { id: ga.id, members: [...new Set([...ga.members, ...gb.members])] };
        fs.rmSync(this.p("groups", `${gb.id}.json`), { force: true });
      } else if (ga) {
        g = { id: ga.id, members: [...ga.members, other.id] };
      } else if (gb) {
        g = { id: gb.id, members: [...gb.members, me.id] };
      } else {
        g = { id: `g${hex(3)}`, members: [me.id, other.id] };
      }
      this.saveGroup(g);
      return g;
    });
  }

  unlink(selfId: string): void {
    return this.lockGroups(() => {
      const me = this.self(selfId);
      if (!me.group) return;
      this.removeMember(me.group, selfId);
      this.setGroup(selfId, undefined);
    });
  }

  members(selfId: string): SessionRecord[] {
    const all = this.sessions();
    const me = all.find((s) => s.id === selfId);
    if (!me?.group) return [];
    return all.filter((s) => s.group === me.group);
  }

  // ---- pair counters: one file per sender/receiver, so no process overwrites another's counters ----

  protected pairFile(from: string, to: string): string {
    return this.p("pairs", from, `${to}.json`);
  }

  protected readPair(from: string, to: string): PairState {
    return this.readJson<PairState>(this.pairFile(from, to)) ?? { n: 0, lastAt: "", recent: [] };
  }

  protected writePair(from: string, to: string, st: PairState): void {
    const file = this.pairFile(from, to);
    fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
    this.write(file, st);
  }

  // Reserves the next hop for this pair, or says why the message must be withheld.
  protected takePair(from: string, to: string, limits: Limits): { hop: number; reason?: string } {
    const now = this.now().getTime();
    return this.lock(this.pairFile(from, to), () => {
      const p = this.readPair(from, to);
      const idle = p.lastAt ? now - Date.parse(p.lastAt) : Infinity;
      const n = idle > limits.resetMs ? 0 : p.n;
      const recent = (p.recent ?? []).filter((t) => now - t < limits.windowMs);
      if (n >= limits.pairLimit) return { hop: 0, reason: `${n} messages to you not yet delivered` };
      if (recent.length >= limits.rateLimit) {
        return { hop: 0, reason: `${recent.length} messages to you in ${Math.round(limits.windowMs / 1000)}s` };
      }
      this.writePair(from, to, { n: n + 1, lastAt: new Date(now).toISOString(), recent: [...recent, now] });
      return { hop: n + 1 };
    });
  }

  // ---- ledger ----

  protected ledgerFile(from: string, id: string): string {
    return this.p("outbox", from, `${id}.json`);
  }

  protected writeLedger(from: string, row: LedgerRow): void {
    const file = this.ledgerFile(from, row.id);
    fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
    this.write(file, row);
  }

  // All live ledger rows, dropping rows a terminal state left behind a day ago on the way through.
  protected rows(): { from: string; row: LedgerRow }[] {
    const out: { from: string; row: LedgerRow }[] = [];
    const cutoff = this.now().getTime() - LEDGER_MS;
    let dirs: string[] = [];
    try {
      dirs = fs.readdirSync(this.p("outbox"));
    } catch {
      return out;
    }
    const live = this.liveIds();
    for (const from of dirs) {
      let files: string[] = [];
      try {
        files = fs.readdirSync(this.p("outbox", from));
      } catch {
        continue;
      }
      for (const f of files) {
        if (!f.endsWith(".json")) continue;
        const file = this.p("outbox", from, f);
        const row = this.readJson<LedgerRow>(file);
        if (!row) continue;
        // Only a terminal row is safe to forget: a queued one may still be delivered and settled.
        if (TERMINAL_STATES.has(row.state) && Date.parse(row.stateAt ?? row.ts) < cutoff) {
          fs.rmSync(file, { force: true });
          continue;
        }
        out.push({ from, row });
      }
      if (!live.has(from)) this.rmdirIfEmpty(this.p("outbox", from));
    }
    this.prunePairs(live);
    return out;
  }

  // Session ids with a record on disk; whatever no longer has one is what the sweep reaps.
  protected liveIds(): Set<string> {
    const ids = new Set<string>();
    try {
      for (const f of fs.readdirSync(this.p("sessions"))) if (f.endsWith(".json")) ids.add(f.slice(0, -5));
    } catch {}
    return ids;
  }

  protected rmdirIfEmpty(dir: string): void {
    try {
      if (!fs.readdirSync(dir).length) fs.rmdirSync(dir);
    } catch {}
  }

  // Pair counters outlive the session that wrote them, so a dead pair file is only age-limited garbage.
  protected prunePairs(live: Set<string>): void {
    let froms: string[] = [];
    try {
      froms = fs.readdirSync(this.p("pairs"));
    } catch {
      return;
    }
    for (const from of froms) {
      const dir = this.p("pairs", from);
      let files: string[] = [];
      try {
        files = fs.readdirSync(dir);
      } catch {
        continue;
      }
      for (const f of files) {
        if (!f.endsWith(".json")) continue;
        // Only a delivery resets a counter, so one whose receiver is gone is dead weight.
        if (live.has(f.slice(0, -5))) continue;
        const file = path.join(dir, f);
        try {
          // Wall clock, because these files outlive the fake clocks tests run with.
          if (Date.now() - fs.statSync(file).mtimeMs < this.limits.resetMs) continue;
          fs.rmSync(file, { force: true });
        } catch {}
      }
      if (!live.has(from)) this.rmdirIfEmpty(dir);
    }
  }

  // Shorts are unique across the whole root, so a short in any header names one message.
  protected allocShort(): string {
    const dir = this.p("shorts");
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    this.pruneShorts(dir);
    const used = new Set(this.rows().map((r) => r.row.short));
    for (let width = 4; width <= 6; width++) {
      for (let i = 0; i < 20; i++) {
        const s = hex(width);
        if (!used.has(s) && this.claimShort(s)) return s;
      }
    }
    for (;;) {
      const s = hex(8);
      if (this.claimShort(s)) return s;
    }
  }

  // O_EXCL on a per-short file: the loser of a race gets EEXIST and draws again.
  protected claimShort(s: string): boolean {
    try {
      fs.closeSync(fs.openSync(this.p("shorts", s), "wx", 0o600));
      return true;
    } catch (e) {
      if ((e as { code?: string }).code === "EEXIST") return false;
      throw e;
    }
  }

  // One claim file per message, so mtime is its age; wall clock, because the files outlive fake clocks.
  protected pruneShorts(dir: string): void {
    let files: string[] = [];
    try {
      files = fs.readdirSync(dir);
    } catch {
      return;
    }
    if (files.length < 4096) return;
    const cutoff = Date.now() - LEDGER_MS;
    for (const f of files) {
      try {
        if (fs.statSync(path.join(dir, f)).mtimeMs < cutoff) fs.rmSync(path.join(dir, f), { force: true });
      } catch {}
    }
  }

  protected ledgerRow(m: Message): LedgerRow {
    return {
      v: 1,
      id: m.id,
      short: m.short!,
      to: m.to,
      ts: m.ts,
      state: m.state ?? "queued",
      h: hashText(m.text),
      len: Buffer.byteLength(m.text, "utf8"),
      ...(m.notice ? { notice: true as const, reason: m.notice.reason } : {}),
    };
  }

  protected setState(m: Message, state: MsgState): void {
    this.advance(this.ledgerFile(m.from.id, m.id), state);
  }

  // One row, one transition: a late ack cannot overwrite read, a racing supersede cannot undo delivered.
  protected advance(file: string, state: MsgState, extra: Partial<LedgerRow> = {}): boolean {
    return this.lock(file, () => {
      const row = this.readJson<LedgerRow>(file);
      if (!row || STATE_RANK[state] <= STATE_RANK[row.state]) return false;
      this.write(file, { ...row, ...extra, state, stateAt: this.iso() });
      return true;
    });
  }

  protected enqueue(m: Message): void {
    const dir = this.p("inbox", m.to);
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    this.write(path.join(dir, `${m.id}.json`), m);
  }

  protected fromRef(s: SessionRecord): Message["from"] {
    return {
      id: s.id,
      kind: s.kind,
      ...(s.name ? { name: s.name } : {}),
      parent: s.parent ?? null,
    };
  }

  // ---- delivery state ----

  pending(id: string): Message[] {
    const dir = this.p("inbox", id);
    let files: string[] = [];
    try {
      files = fs.readdirSync(dir).filter((f) => f.endsWith(".json")).sort();
    } catch {
      return [];
    }
    const out: Message[] = [];
    for (const f of files) {
      const m = this.readJson<Message>(path.join(dir, f));
      if (m && (m.state ?? "queued") === "queued") out.push(m);
    }
    return out;
  }

  // Handed to the agent's context: leaves the queue and resets the sender's burst for this pair.
  // Returns false when another process already delivered it, so a second consumer cannot count it twice.
  delivered(m: Message): boolean {
    if (!this.claim(m)) return false;
    this.setState(m, "delivered");
    const from = m.from.onBehalfOf?.id ?? m.from.id;
    // A withheld notice is not a hop: refunding the pair there would undo the refusal it reports.
    if (!m.notice && (m.from.kind !== "script" || m.from.onBehalfOf)) {
      this.lock(this.pairFile(from, m.to), () => {
        this.writePair(from, m.to, { ...this.readPair(from, m.to), n: 0 });
      });
    }
    return true;
  }

  // rename is the cross-process test-and-set: exactly one caller can move the file away.
  protected claim(m: Message): boolean {
    const file = this.p("inbox", m.to, `${m.id}.json`);
    const held = `${file}.${process.pid}.claim`;
    try {
      fs.renameSync(file, held);
    } catch {
      return false;
    }
    fs.rmSync(held, { force: true });
    return true;
  }

  read(m: Message): void {
    fs.rmSync(this.p("inbox", m.to, `${m.id}.json`), { force: true });
    this.setState(m, "read");
  }

  drop(m: Message, state: "superseded" | "withheld"): void {
    fs.rmSync(this.p("inbox", m.to, `${m.id}.json`), { force: true });
    this.setState(m, state);
  }

  unread(id: string): Record<string, number> {
    const out: Record<string, number> = {};
    for (const m of this.pending(id)) {
      const from = m.from.onBehalfOf?.id ?? m.from.id;
      out[from] = (out[from] ?? 0) + 1;
    }
    return out;
  }

  // A human spoke to this session, so every peer's unanswered run against it is over.
  markHuman(id: string): void {
    this.self(id);
    fs.rmSync(this.p("pairs", id), { recursive: true, force: true });
    for (const from of fs.readdirSync(this.p("pairs"))) {
      fs.rmSync(this.p("pairs", from, `${id}.json`), { force: true });
    }
  }

  status(fromId: string, ref?: string): StatusRow[] {
    const rows = this.rows().filter((r) => r.from === fromId);
    const toRow = (row: LedgerRow): StatusRow => ({
      id: row.id,
      short: row.short,
      to: row.to,
      ts: row.ts,
      state: row.state,
      ...(row.stateAt ? { stateAt: row.stateAt } : {}),
      ...(row.reason ? { reason: row.reason } : {}),
    });
    if (!ref?.trim()) {
      return rows
        .sort((a, b) => Date.parse(b.row.ts) - Date.parse(a.row.ts))
        .slice(0, 10)
        .map((r) => toRow(r.row));
    }
    const t = ref.trim().toLowerCase();
    const pool = rows.map((r) => r.row);
    const pick =
      pool.filter((r) => r.short === t) .length ? pool.filter((r) => r.short === t)
      : pool.filter((r) => r.id === t).length ? pool.filter((r) => r.id === t)
      : pool.filter((r) => r.short.startsWith(t)).length ? pool.filter((r) => r.short.startsWith(t))
      : pool.filter((r) => r.id.startsWith(t));
    if (pick.length === 1) return [toRow(pick[0])];
    if (pick.length === 0) throw new RelayError(`no message matches "${ref}"`);
    throw new RelayError(`"${ref}" is ambiguous: ${pick.map((r) => r.short).join(", ")}`);
  }

  // Refs resolve against the sender's own ledger only; a foreign row must never shadow one of ours.
  protected findRow(fromId: string, ref: string): LedgerRow | undefined {
    const t = ref.trim().toLowerCase();
    if (!t) return undefined;
    const rows = this.rows()
      .filter((r) => r.from === fromId)
      .map((r) => r.row);
    const one = (hit: LedgerRow[]): LedgerRow | undefined => {
      if (hit.length > 1) throw new RelayError(`"${ref}" is ambiguous: ${hit.map((r) => r.short).join(", ")}`);
      return hit[0];
    };
    return (
      one(rows.filter((r) => r.short === t)) ??
      one(rows.filter((r) => r.id === t)) ??
      one(rows.filter((r) => r.short.startsWith(t))) ??
      one(rows.filter((r) => r.id.startsWith(t)))
    );
  }

  // Resolves the refs of one send once, so each copy can claim only the victim sent to its receiver.
  protected resolveRefs(fromId: string, refs: string[]): LedgerRow[] {
    const out: LedgerRow[] = [];
    for (const ref of refs) {
      const row = this.findRow(fromId, ref);
      if (row) out.push(row);
    }
    return out;
  }

  // Drops a still-queued victim from the receiver's inbox; a delivered one only keeps the flag.
  protected supersedeOne(fromId: string, row: LedgerRow): string {
    fs.rmSync(this.p("inbox", row.to, `${row.id}.json`), { force: true });
    // Losing this race to a delivery is fine: the guard keeps whichever happened first.
    this.advance(this.ledgerFile(fromId, row.id), "superseded");
    return row.short;
  }

  protected findDuplicate(from: string, to: string, text: string, limits: Limits): LedgerRow | undefined {
    const h = hashText(text);
    const now = this.now().getTime();
    const rows = this.rows()
      .filter((r) => r.from === from && r.row.to === to && r.row.h === h && !r.row.notice)
      .sort((a, b) => Date.parse(b.row.ts) - Date.parse(a.row.ts));
    const last = rows[0]?.row;
    if (!last) return undefined;
    if (last.state === "queued") return last;
    return now - Date.parse(last.ts) <= limits.dedupeMs ? last : undefined;
  }

  protected resolveTargets(me: SessionRecord, all: SessionRecord[], group: SessionRecord[], to: string): SessionRecord[] {
    if (to.trim().toLowerCase() === "all") {
      if (!group.length) throw new RelayError("not linked to any session, use relay_link first");
      return group;
    }
    const inGroup = this.match(to, group);
    if (inGroup.length === 1) return inGroup;
    if (inGroup.length > 1) throw new RelayError(`"${to}" is ambiguous: ${inGroup.map(describe).join(", ")}`);
    const awaiting = all.filter((s) => s.id !== me.id && s.kind === "script" && s.agent?.awaiting === me.id);
    const scripts = this.match(to, awaiting);
    if (scripts.length === 1) return scripts;
    if (scripts.length > 1) throw new RelayError(`"${to}" is ambiguous: ${scripts.map(describe).join(", ")}`);
    if (!group.length) throw new RelayError("not linked to any session, use relay_link first");
    const outside = this.match(to, all.filter((s) => s.id !== me.id));
    throw new RelayError(
      outside.length ? `"${to}" is not in your group, use relay_link first` : `no session matches "${to}"`,
    );
  }

  send(fromId: string, to: string, text: string, hopLimit?: number, opts: SendOptions = {}): SendResult {
    const me = this.self(fromId);
    const all = this.sessions();
    if (!all.some((s) => s.id === fromId)) throw new RelayError(`session ${fromId} is not registered`);
    const limits = hopLimit ? { ...this.limits, pairLimit: hopLimit } : this.limits;
    const group = me.group ? all.filter((s) => s.group === me.group && s.id !== me.id) : [];
    const targets = this.resolveTargets(me, all, group, to);
    const capped = capText(text);
    // Refs are resolved before any copy is written, and a send that replaces something is never a duplicate.
    const victims = opts.supersedes?.length ? this.resolveRefs(fromId, opts.supersedes) : [];
    const revoked = opts.cancels?.length ? this.resolveRefs(fromId, opts.cancels) : [];
    const carriesRefs = victims.length > 0 || revoked.length > 0;
    const result: SendResult = { targets: [], msgs: [] };
    const withheld: { to: string; reason: string }[] = [];

    for (const t of targets) {
      const dup = carriesRefs ? undefined : this.findDuplicate(fromId, t.id, capped, limits);
      if (dup) {
        result.deduped = { to: t.id, id: dup.id, short: dup.short };
        continue;
      }
      const taken = this.takePair(fromId, t.id, limits);
      if (taken.reason) {
        withheld.push({ to: t.id, reason: taken.reason });
        this.withhold(me, t, capped, taken.reason, limits);
        continue;
      }
      const msg = this.compose(me, t, capped, taken.hop, opts, victims, revoked);
      // Row first: the receiver may settle the message the moment it lands.
      this.writeLedger(fromId, this.ledgerRow(msg));
      this.enqueue(msg);
      result.targets.push(t.id);
      result.msgs.push({ to: t.id, id: msg.id, short: msg.short! });
    }

    if (withheld.length) {
      result.withheld = withheld;
      if (!result.msgs.length && !result.deduped) {
        throw new RelayError(`message withheld: ${withheld[0].reason}. Ask the user before continuing`);
      }
    }
    return result;
  }

  protected compose(
    me: SessionRecord,
    t: SessionRecord,
    text: string,
    hop: number,
    opts: SendOptions,
    victims: LedgerRow[],
    revoked: LedgerRow[],
  ): Message {
    // One copy per receiver: only the one whose receiver holds the victim may claim the replacement.
    const supersedes = victims.filter((r) => r.to === t.id).map((r) => this.supersedeOne(me.id, r));
    const cancels = revoked.filter((r) => r.to === t.id).map((r) => this.supersedeOne(me.id, r));
    return {
      v: 1,
      id: messageId(this.now()),
      short: this.allocShort(),
      from: this.fromRef(me),
      to: t.id,
      ...(me.group ? { group: me.group } : {}),
      hop,
      ts: this.iso(),
      text,
      state: "queued",
      ...(opts.inReplyTo ? { inReplyTo: opts.inReplyTo } : {}),
      ...(supersedes.length ? { supersedes } : {}),
      ...(cancels.length ? { cancels } : {}),
    };
  }

  // The sender is refused, but the receiver still learns a message was dropped.
  protected withhold(me: SessionRecord, t: SessionRecord, text: string, reason: string, limits: Limits): void {
    const now = this.now().getTime();
    const recent = this.rows().some(
      (r) => r.from === me.id && r.row.to === t.id && r.row.notice && now - Date.parse(r.row.ts) < limits.windowMs,
    );
    const refused: Message = {
      v: 1,
      id: messageId(this.now()),
      short: this.allocShort(),
      from: this.fromRef(me),
      to: t.id,
      hop: 0,
      ts: this.iso(),
      text: capText(text),
      state: "withheld",
    };
    this.writeLedger(me.id, { ...this.ledgerRow(refused), reason });
    if (recent) return;
    const notice: Message = {
      v: 1,
      id: messageId(this.now()),
      short: this.allocShort(),
      from: this.fromRef(me),
      to: t.id,
      hop: 0,
      ts: this.iso(),
      text: `message withheld: ${reason}. Ask the user before continuing.`,
      state: "queued",
      notice: { reason, preview: refused.text.slice(0, 120), bytes: Buffer.byteLength(refused.text, "utf8") },
    };
    this.writeLedger(me.id, this.ledgerRow(notice));
    this.enqueue(notice);
  }

  sendScript(
    to: string,
    text: string,
    opts?: { fromId?: string; name?: string; reply?: boolean; onBehalfOf?: string },
  ): Message {
    const all = this.sessions();
    const liveAgents = all.filter((s) => s.kind !== "script");
    const matched = this.match(to, liveAgents);
    if (matched.length === 0) throw new RelayError(`no session matches "${to}"`);
    if (matched.length > 1) throw new RelayError(`"${to}" is ambiguous: ${matched.map(describe).join(", ")}`);
    const target = matched[0];
    const behalf = opts?.onBehalfOf ? this.get(opts.onBehalfOf) : undefined;
    // A script acting for an agent shares that agent's pair budget.
    const hop = behalf ? this.takePair(behalf.id, target.id, this.limits) : { hop: 0 };
    if (hop.reason) {
      if (behalf) this.withhold(behalf, target, text, hop.reason, this.limits);
      throw new RelayError(`message withheld: ${hop.reason}. Ask the user before continuing`);
    }
    const msg: Message = {
      v: 1,
      id: messageId(this.now()),
      short: this.allocShort(),
      from: {
        id: opts?.fromId ?? hex(5),
        kind: "script",
        ...(opts?.name ? { name: opts.name } : {}),
        ...(behalf ? { onBehalfOf: { id: behalf.id, kind: behalf.kind, ...(behalf.name ? { name: behalf.name } : {}) } } : {}),
      },
      to: target.id,
      hop: hop.hop,
      ts: this.iso(),
      text: capText(text),
      state: "queued",
      ...(opts?.reply ? { reply: true } : {}),
    };
    this.writeLedger(msg.from.id, this.ledgerRow(msg));
    this.enqueue(msg);
    return msg;
  }

  // deliver returns false to keep the message and stop until the next event or poll.
  watch(id: string, deliver: (m: Message) => boolean | Promise<boolean>, pollMs = 3000): Watcher {
    const dir = this.p("inbox", id);
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    let running: Promise<void> | undefined;
    let again = false;
    let closed = false;

    const pass = async (): Promise<void> => {
      do {
        again = false;
        let files: string[];
        try {
          files = fs.readdirSync(dir).filter((f) => f.endsWith(".json")).sort();
        } catch {
          return;
        }
        for (const f of files) {
          if (closed) return;
          const file = path.join(dir, f);
          const msg = this.readJson<Message>(file);
          if (!msg) {
            fs.rmSync(file, { force: true });
            continue;
          }
          let ok = false;
          try {
            ok = await deliver(msg);
          } catch {
            ok = false;
          }
          if (!ok) return;
          this.delivered(msg);
        }
      } while (again && !closed);
    };

    const drain = (): Promise<void> => {
      if (running) {
        again = true;
        return running;
      }
      running = pass().finally(() => {
        running = undefined;
      });
      return running;
    };

    const fsw = fs.watch(dir, () => void drain());
    const timer = setInterval(() => void drain(), pollMs);
    timer.unref?.();
    return {
      close() {
        closed = true;
        fsw.close();
        clearInterval(timer);
      },
      drain,
    };
  }
}