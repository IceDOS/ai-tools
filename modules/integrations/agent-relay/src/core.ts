const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");
const path = process.getBuiltinModule("node:path") as typeof import("node:path");
const crypto = process.getBuiltinModule("node:crypto") as typeof import("node:crypto");

export type Kind = "pi" | "claude" | "opencode" | "agy" | "script";

export interface SessionRecord {
  id: string;
  kind: Kind;
  pid: number;
  cwd: string;
  name?: string;
  group?: string;
  hop?: number;
  hopAt?: string;
  hopReset?: string;
  started: string;
  agent?: Record<string, unknown>;
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

export interface RelayOptions {
  root?: string;
  isAlive?: (pid: number) => boolean;
  now?: () => Date;
}

export class RelayError extends Error {}

export const ID_RE = /^[0-9a-f]{5}$/;

export function defaultRoot(env: Record<string, string | undefined> = process.env): string {
  if (env.AGENT_RELAY_ROOT) return env.AGENT_RELAY_ROOT;
  return path.join(env.XDG_RUNTIME_DIR ?? `/run/user/${process.getuid?.() ?? 0}`, "agent-relay");
}

export function readConfig(env: Record<string, string | undefined> = process.env): { hopLimit: number } {
  const base = env.XDG_CONFIG_HOME ?? path.join(env.HOME ?? "", ".config");
  try {
    const cfg = JSON.parse(fs.readFileSync(path.join(base, "agent-relay", "config.json"), "utf8"));
    if (Number.isInteger(cfg.hopLimit) && cfg.hopLimit > 0) return { hopLimit: cfg.hopLimit };
  } catch {}
  return { hopLimit: 10 };
}

export function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return (e as { code?: string }).code === "EPERM";
  }
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

export interface Message {
  v: 1;
  id: string;
  from: { id: string; kind: Kind; name?: string };
  to: string;
  group?: string;
  hop: number;
  ts: string;
  text: string;
  reply?: boolean;
}

export interface Watcher {
  close(): void;
  drain(): Promise<void>;
}

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

export function formatMessage(m: Message): string {
  if (m.from.kind === "script") {
    const who = m.from.name ? `"${m.from.name}"` : m.from.id;
    const reply = m.reply ? `\n(reply with relay_send to="${m.from.id}")` : "";
    return `${RELAY_PREFIX}script ${who}]\n${m.text}${reply}`;
  }
  const name = m.from.name ? ` "${m.from.name}"` : "";
  return `${RELAY_PREFIX}${m.from.kind} ${m.from.id}${name}, hop ${m.hop}]\n${m.text}\n(reply with relay_send to="${m.from.id}")`;
}

export function nextHop(r: SessionRecord): number {
  if (r.hop === undefined || !r.hopAt) return 1;
  if (r.hopReset && r.hopReset >= r.hopAt) return 1;
  return r.hop + 1;
}

let seq = 0;
// Lexically sortable: time, then a per-process counter, then randomness.
function messageId(now: Date): string {
  seq = (seq + 1) % 36 ** 4;
  return now.getTime().toString(36).padStart(10, "0") + seq.toString(36).padStart(4, "0") + hex(4);
}

export class Relay {
  readonly root: string;
  protected isAlive: (pid: number) => boolean;
  protected now: () => Date;

  constructor(o: RelayOptions = {}) {
    this.root = o.root ?? defaultRoot();
    this.isAlive = o.isAlive ?? pidAlive;
    this.now = o.now ?? (() => new Date());
    for (const d of ["sessions", "groups", "inbox"]) {
      fs.mkdirSync(path.join(this.root, d), { recursive: true, mode: 0o700 });
    }
  }

  protected p(...parts: string[]): string {
    return path.join(this.root, ...parts);
  }

  protected read<T>(file: string): T | undefined {
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

  protected iso(): string {
    return this.now().toISOString();
  }

  sessions(): SessionRecord[] {
    const out: SessionRecord[] = [];
    for (const f of fs.readdirSync(this.p("sessions"))) {
      if (!f.endsWith(".json")) continue;
      const r = this.read<SessionRecord>(this.p("sessions", f));
      if (!r) continue;
      if (this.isAlive(r.pid)) out.push(r);
      else this.drop(r);
    }
    return out;
  }

  get(id: string): SessionRecord | undefined {
    const r = this.read<SessionRecord>(this.p("sessions", `${id}.json`));
    if (r && !this.isAlive(r.pid)) {
      this.drop(r);
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
    const rec: SessionRecord = {
      id,
      kind: input.kind,
      pid: input.pid,
      cwd: input.cwd,
      started: this.iso(),
      ...(input.name ? { name: input.name } : {}),
      ...(input.agent ? { agent: input.agent } : {}),
    };
    fs.mkdirSync(this.p("inbox", id), { recursive: true, mode: 0o700 });
    this.write(this.p("sessions", `${id}.json`), rec);
    return rec;
  }

  update(id: string, patch: Partial<SessionRecord>): SessionRecord {
    const cur = this.get(id);
    if (!cur) throw new RelayError(`session ${id} is not registered`);
    const next = { ...cur, ...patch, id: cur.id };
    this.write(this.p("sessions", `${id}.json`), next);
    return next;
  }

  unregister(id: string): void {
    const r = this.read<SessionRecord>(this.p("sessions", `${id}.json`));
    if (r) this.drop(r);
  }

  protected drop(r: SessionRecord): void {
    fs.rmSync(this.p("sessions", `${r.id}.json`), { force: true });
    fs.rmSync(this.p("inbox", r.id), { recursive: true, force: true });
    if (r.group) this.removeMember(r.group, r.id);
  }

  group(gid: string): Group | undefined {
    return this.read<Group>(this.p("groups", `${gid}.json`));
  }

  // Raw write without liveness checks; drop() calls this while pruning.
  protected setGroup(id: string, gid: string | undefined): void {
    const file = this.p("sessions", `${id}.json`);
    const r = this.read<SessionRecord>(file);
    if (r) this.write(file, { ...r, group: gid });
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
    const g = this.group(gid);
    if (g) this.saveGroup({ id: gid, members: g.members.filter((m) => m !== id) });
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

  link(selfId: string, peer: string): Group {
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
  }

  unlink(selfId: string): void {
    const me = this.self(selfId);
    if (!me.group) return;
    this.removeMember(me.group, selfId);
    this.setGroup(selfId, undefined);
  }

  members(selfId: string): SessionRecord[] {
    const all = this.sessions();
    const me = all.find((s) => s.id === selfId);
    if (!me?.group) return [];
    return all.filter((s) => s.group === me.group);
  }

  markInjected(id: string, hop: number): void {
    if (hop <= 0) return;
    this.update(id, { hop, hopAt: this.iso() });
  }

  markHuman(id: string): void {
    this.update(id, { hopReset: this.iso() });
  }

  send(fromId: string, to: string, text: string, hopLimit: number): string[] {
    this.self(fromId);
    const all = this.sessions();
    const me = all.find((s) => s.id === fromId);
    if (!me) throw new RelayError(`session ${fromId} is not registered`);
    const hop = nextHop(me);
    if (hop > hopLimit) throw new RelayError("hop limit reached, ask the user before continuing");
    const group = me.group ? all.filter((s) => s.group === me.group && s.id !== me.id) : [];
    let targets: SessionRecord[];
    if (to.trim().toLowerCase() === "all") {
      if (!group.length) throw new RelayError("not linked to any session, use relay_link first");
      targets = group;
    } else {
      targets = this.match(to, group);
      if (targets.length === 0) {
        const awaiting = all.filter((s) => s.id !== me.id && s.kind === "script" && s.agent?.awaiting === me.id);
        const scriptTargets = this.match(to, awaiting);
        if (scriptTargets.length === 1) {
          targets = scriptTargets;
        } else if (scriptTargets.length > 1) {
          throw new RelayError(`"${to}" is ambiguous: ${scriptTargets.map(describe).join(", ")}`);
        } else {
          if (!group.length) throw new RelayError("not linked to any session, use relay_link first");
          const outside = this.match(to, all.filter((s) => s.id !== me.id));
          throw new RelayError(outside.length ? `"${to}" is not in your group, use relay_link first` : `no session matches "${to}"`);
        }
      } else if (targets.length > 1) {
        throw new RelayError(`"${to}" is ambiguous: ${targets.map(describe).join(", ")}`);
      }
    }
    const ts = this.iso();
    const capped = capText(text);
    for (const t of targets) {
      const msg: Message = {
        v: 1,
        id: messageId(this.now()),
        from: { id: me.id, kind: me.kind, ...(me.name ? { name: me.name } : {}) },
        to: t.id,
        ...(me.group ? { group: me.group } : {}),
        hop,
        ts,
        text: capped,
      };
      const dir = this.p("inbox", t.id);
      fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
      this.write(path.join(dir, `${msg.id}.json`), msg);
    }
    return targets.map((t) => t.id);
  }

  sendScript(to: string, text: string, opts?: { fromId?: string; name?: string; reply?: boolean }): Message {
    const all = this.sessions();
    const liveAgents = all.filter((s) => s.kind !== "script");
    const matched = this.match(to, liveAgents);
    if (matched.length === 0) throw new RelayError(`no session matches "${to}"`);
    if (matched.length > 1) throw new RelayError(`"${to}" is ambiguous: ${matched.map(describe).join(", ")}`);
    const target = matched[0];
    const msg: Message = {
      v: 1,
      id: messageId(this.now()),
      from: {
        id: opts?.fromId ?? hex(5),
        kind: "script",
        ...(opts?.name ? { name: opts.name } : {}),
      },
      to: target.id,
      hop: 0,
      ts: this.iso(),
      text: capText(text),
      ...(opts?.reply ? { reply: true } : {}),
    };
    const dir = this.p("inbox", target.id);
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    this.write(path.join(dir, `${msg.id}.json`), msg);
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
          const msg = this.read<Message>(file);
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
          fs.rmSync(file, { force: true });
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
