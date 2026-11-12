import { closeSync, existsSync, fstatSync, mkdirSync, openSync, readdirSync, readSync, statSync } from "node:fs";
import { join } from "node:path";
import { type ExtensionAPI, type ExtensionContext, getAgentDir, type Theme } from "@earendil-works/pi-coding-agent";
import { matchesKey, type TUI, truncateToWidth, wrapTextWithAnsi } from "@earendil-works/pi-tui";

// pi-rlm-child-log.patch makes rlm() write each child's JSONL stream into this dir.
const WIDGET = "rlm-children";
const POLL_MS = 1000;
const LINGER_MS = 8000;
const MAX_ENTRIES = 2000;
const MAX_TEXT = 4000;

type EntryKind = "prompt" | "text" | "tool" | "result" | "error" | "info";
interface Entry {
  kind: EntryKind;
  text: string;
}

interface Child {
  file: string;
  offset: number;
  partial: string;
  seq: number;
  id: string;
  parent: string | null;
  depth: number;
  prompt: string;
  model: string | null;
  pid: number;
  startedAt: number;
  endedAt?: number;
  ok?: boolean;
  error?: string | null;
  lost?: boolean;
  turns: number;
  tokens: number;
  cost: number;
  tool?: string;
  streaming: string;
  entries: Entry[];
}

const clip = (s: string, n = MAX_TEXT) => (s.length > n ? `${s.slice(0, n)}…` : s);
const oneLine = (s: string) => s.replace(/\s+/g, " ").trim();

function argSummary(args: unknown): string {
  if (!args || typeof args !== "object") return "";
  const a = args as Record<string, unknown>;
  const v = a.command ?? a.code ?? a.path ?? a.pattern ?? a.query ?? a.url ?? Object.values(a)[0];
  return typeof v === "string" ? oneLine(v) : oneLine(JSON.stringify(v ?? ""));
}

function resultText(result: unknown): string {
  const content = (result as { content?: Array<{ type?: string; text?: string }> } | undefined)?.content;
  return (content ?? [])
    .filter((p) => p?.type === "text" && typeof p.text === "string")
    .map((p) => p.text)
    .join("\n");
}

function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return (e as NodeJS.ErrnoException).code === "EPERM";
  }
}

function lastWrite(file: string): number | undefined {
  try {
    return statSync(file).mtimeMs;
  } catch {
    return undefined;
  }
}

function formatDuration(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  return s >= 3600
    ? `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`
    : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function formatTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

class ChildStore {
  children = new Map<string, Child>();
  private seq = 0;

  constructor(public dir: string) {}

  private push(c: Child, kind: EntryKind, text: string) {
    if (!text.trim()) return;
    c.entries.push({ kind, text: clip(text) });
    if (c.entries.length > MAX_ENTRIES) c.entries.splice(0, c.entries.length - MAX_ENTRIES);
  }

  private apply(c: Child, line: string) {
    let r: any;
    try {
      r = JSON.parse(line);
    } catch {
      return;
    }
    switch (r?.type) {
      case "rlm_child_start":
        Object.assign(c, {
          id: r.id,
          parent: r.parent ?? null,
          depth: r.depth ?? 1,
          prompt: r.prompt ?? "",
          model: r.model ?? null,
          pid: r.pid ?? 0,
          startedAt: r.startedAt ?? Date.now(),
        });
        this.push(c, "prompt", c.prompt);
        break;
      case "message_update":
        if (r.assistantMessageEvent?.type === "text_delta") c.streaming += r.assistantMessageEvent.delta ?? "";
        break;
      case "message_end": {
        const m = r.message;
        if (m?.role !== "assistant") break;
        c.streaming = "";
        c.turns++;
        c.tokens += m.usage?.totalTokens ?? 0;
        c.cost += m.usage?.cost?.total ?? 0;
        if (m.model && !c.model) c.model = m.model;
        for (const part of m.content ?? []) {
          if (part?.type === "text") this.push(c, "text", part.text ?? "");
          else if (part?.type === "toolCall") this.push(c, "tool", `${part.name}  ${argSummary(part.arguments)}`);
        }
        if (m.errorMessage) this.push(c, "error", String(m.errorMessage));
        break;
      }
      case "tool_execution_start":
        c.tool = `${r.toolName} ${argSummary(r.args)}`;
        break;
      case "tool_execution_end":
        c.tool = undefined;
        this.push(c, r.isError ? "error" : "result", resultText(r.result));
        break;
      case "rlm_child_end":
        c.endedAt = r.endedAt ?? Date.now();
        c.ok = r.ok;
        c.error = r.error;
        c.tool = undefined;
        c.streaming = "";
        if (r.usage) {
          c.tokens = r.usage.totalTokens ?? c.tokens;
          c.cost = r.usage.cost?.total ?? c.cost;
        }
        if (r.aborted) this.push(c, "info", "cancelled");
        else if (r.error) this.push(c, "error", String(r.error));
        break;
    }
  }

  private readNew(c: Child) {
    let fd: number | undefined;
    try {
      fd = openSync(c.file, "r");
      const size = fstatSync(fd).size;
      if (size <= c.offset) return;
      const buf = Buffer.alloc(size - c.offset);
      readSync(fd, buf, 0, buf.length, c.offset);
      c.offset = size;
      const lines = (c.partial + buf.toString("utf8")).split("\n");
      c.partial = lines.pop() ?? "";
      for (const line of lines) if (line.trim()) this.apply(c, line);
    } catch {
    } finally {
      if (fd !== undefined) closeSync(fd);
    }
  }

  refresh(): void {
    if (!existsSync(this.dir)) return;
    let names: string[] = [];
    try {
      names = readdirSync(this.dir).filter((n) => n.endsWith(".jsonl")).sort();
    } catch {
      return;
    }
    for (const name of names) {
      const file = join(this.dir, name);
      let c = this.children.get(file);
      if (!c) {
        c = {
          file,
          offset: 0,
          partial: "",
          seq: ++this.seq,
          id: name.replace(/\.jsonl$/, ""),
          parent: null,
          depth: 1,
          prompt: "",
          model: null,
          pid: 0,
          startedAt: Date.now(),
          turns: 0,
          tokens: 0,
          cost: 0,
          streaming: "",
          entries: [],
        };
        this.children.set(file, c);
      }
      if (c.endedAt === undefined) this.readNew(c);
      // A child whose spawning process died never writes its end record.
      if (c.endedAt === undefined && c.pid && !pidAlive(c.pid)) {
        c.lost = true;
        c.endedAt = lastWrite(c.file) ?? Date.now();
      }
    }
  }

  // Parents first, each followed by its descendants in start order.
  ordered(): Child[] {
    const all = [...this.children.values()].sort((a, b) => a.seq - b.seq);
    const ids = new Set(all.map((c) => c.id));
    const out: Child[] = [];
    const walk = (parent: string | null) => {
      for (const c of all) {
        if ((c.parent && ids.has(c.parent) ? c.parent : null) === parent) {
          out.push(c);
          walk(c.id);
        }
      }
    };
    walk(null);
    return out;
  }

  running(): Child[] {
    return this.ordered().filter((c) => c.endedAt === undefined);
  }
}

function statusIcon(c: Child, th: Theme): string {
  if (c.endedAt === undefined) return th.fg("accent", "●");
  if (c.lost) return th.fg("warning", "?");
  return c.ok ? th.fg("success", "✓") : th.fg("error", "✗");
}

function childRow(c: Child, th: Theme, now: number): string {
  const indent = "  ".repeat(Math.max(0, c.depth - 1));
  const dur = formatDuration((c.endedAt ?? now) - c.startedAt);
  const activity = c.endedAt === undefined ? (c.tool ? `▸ ${c.tool}` : c.streaming ? "▸ writing" : "▸ thinking") : "";
  const meta = th.fg("dim", `${dur} ${formatTokens(c.tokens)} tok${c.cost ? ` $${c.cost.toFixed(3)}` : ""}`);
  return `${indent}${statusIcon(c, th)} #${c.seq} ${meta} ${th.fg("muted", activity)} ${oneLine(c.prompt)}`;
}

class ChildrenView {
  private selected = 0;
  private open: Child | undefined;
  private scroll = 0;
  private follow = true;

  constructor(
    private store: ChildStore,
    private tui: TUI,
    private theme: Theme,
    private done: () => void,
  ) {}

  private height(): number {
    return Math.max(8, Math.floor(this.tui.terminal.rows * 0.8) - 4);
  }

  handleInput(data: string): void {
    const list = this.store.ordered();
    if (this.open) {
      const page = this.height() - 2;
      const up = matchesKey(data, "up") ? 1 : matchesKey(data, "pageUp") ? page : 0;
      const down = matchesKey(data, "down") ? 1 : matchesKey(data, "pageDown") ? page : 0;
      if (matchesKey(data, "escape") || matchesKey(data, "left")) this.open = undefined;
      else if (up) {
        this.scroll = Math.max(0, this.scroll - up);
        this.follow = false;
      } else if (down) this.scroll += down;
      else if (data === "f" || matchesKey(data, "end")) this.follow = true;
      else if (data === "g" || matchesKey(data, "home")) {
        this.scroll = 0;
        this.follow = false;
      }
    } else if (matchesKey(data, "escape") || data === "q") this.done();
    else if (matchesKey(data, "up")) this.selected = Math.max(0, this.selected - 1);
    else if (matchesKey(data, "down")) this.selected = Math.min(list.length - 1, this.selected + 1);
    else if ((matchesKey(data, "return") || matchesKey(data, "right")) && list[this.selected]) {
      this.open = list[this.selected];
      this.scroll = 0;
      this.follow = true;
    }
    this.tui.requestRender();
  }

  private frame(width: number, title: string, body: string[], hint: string): string[] {
    const th = this.theme;
    const inner = Math.max(10, width - 2);
    const row = (s: string) => th.fg("border", "│") + truncateToWidth(s, inner, "…", true) + th.fg("border", "│");
    return [
      th.fg("border", "╭") + truncateToWidth(th.fg("accent", ` ${title} `), inner, "…", true) + th.fg("border", "╮"),
      ...body.map(row),
      row(th.fg("dim", ` ${hint}`)),
      th.fg("border", `╰${"─".repeat(inner)}╯`),
    ];
  }

  private transcript(c: Child, width: number): string[] {
    const th = this.theme;
    const label: Record<EntryKind, string> = {
      prompt: th.fg("accent", "task"),
      text: th.fg("success", "agent"),
      tool: th.fg("warning", "tool"),
      result: th.fg("dim", "result"),
      error: th.fg("error", "error"),
      info: th.fg("muted", "info"),
    };
    const lines: string[] = [];
    const entries = c.streaming ? [...c.entries, { kind: "text" as const, text: `${c.streaming}▌` }] : c.entries;
    for (const e of entries) {
      lines.push(label[e.kind]);
      const body = e.kind === "result" ? e.text.split("\n").slice(0, 12).join("\n") : e.text;
      for (const l of wrapTextWithAnsi(body, width - 4)) lines.push(`  ${e.kind === "result" ? th.fg("dim", l) : l}`);
    }
    if (c.tool) lines.push(th.fg("muted", `▸ ${c.tool}`));
    return lines;
  }

  render(width: number): string[] {
    const th = this.theme;
    const now = Date.now();
    const h = this.height();
    if (this.open) {
      const c = this.open;
      const lines = this.transcript(c, width - 2);
      const maxScroll = Math.max(0, lines.length - h);
      if (this.follow) this.scroll = maxScroll;
      this.scroll = Math.min(this.scroll, maxScroll);
      const body = lines.slice(this.scroll, this.scroll + h);
      while (body.length < h) body.push("");
      const state = c.endedAt === undefined ? "running" : c.lost ? "lost" : c.ok ? "done" : "failed";
      const title = `rlm child #${c.seq} · depth ${c.depth} · ${state} · ${c.model ?? "default model"}`;
      return this.frame(width, title, body, "↑↓/PgUp/PgDn scroll · f follow · g top · esc back");
    }
    const list = this.store.ordered();
    this.selected = Math.min(this.selected, Math.max(0, list.length - 1));
    const rows = list.length
      ? list.map((c, i) => (i === this.selected ? th.fg("accent", "› ") : "  ") + childRow(c, th, now))
      : [th.fg("dim", "  No rlm() children in this session yet.")];
    const start = Math.max(0, Math.min(this.selected - Math.floor(h / 2), rows.length - h));
    const body = rows.slice(start, start + h);
    while (body.length < h) body.push("");
    const running = list.filter((c) => c.endedAt === undefined).length;
    return this.frame(width, `rlm children · ${running} running · ${list.length} total`, body, "↑↓ select · enter open · esc close");
  }

  invalidate(): void {}
}

export default function (pi: ExtensionAPI) {
  let store: ChildStore | undefined;
  let timer: ReturnType<typeof setInterval> | undefined;
  let lastActive = 0;
  let viewTui: TUI | undefined;

  const updateWidget = (ctx: ExtensionContext) => {
    if (!store || !ctx.hasUI) return;
    store.refresh();
    viewTui?.requestRender();
    const running = store.running();
    const now = Date.now();
    if (running.length) lastActive = now;
    if (!running.length && now - lastActive > LINGER_MS) {
      ctx.ui.setWidget(WIDGET, undefined);
      return;
    }
    const th = ctx.ui.theme;
    const done = store.children.size - running.length;
    const head = th.fg("dim", `rlm ${running.length} running · ${done} finished · /rlm-children to inspect`);
    ctx.ui.setWidget(WIDGET, [head, ...running.slice(0, 4).map((c) => ` ${childRow(c, th, now)}`)], {
      placement: "belowEditor",
    });
  };

  pi.on("session_start", (_event, ctx) => {
    const dir = join(getAgentDir(), "pi-rlm", "children", ctx.sessionManager.getSessionId());
    // Children read this when pi-rlm spawns them; nested children inherit it.
    process.env.PI_RLM_CHILD_LOG_DIR = dir;
    try {
      mkdirSync(dir, { recursive: true });
    } catch {}
    store = new ChildStore(dir);
    store.refresh();
    if (timer) clearInterval(timer);
    if (ctx.hasUI) timer = setInterval(() => updateWidget(ctx), POLL_MS);
  });

  pi.on("session_shutdown", (_event, ctx) => {
    if (timer) clearInterval(timer);
    timer = undefined;
    if (ctx.hasUI) ctx.ui.setWidget(WIDGET, undefined);
    store = undefined;
  });

  pi.registerCommand("rlm-children", {
    description: "Inspect this session's rlm() child agents and their transcripts",
    handler: async (_args, ctx) => {
      if (!store || ctx.mode !== "tui") {
        ctx.ui.notify("rlm-children needs the interactive TUI", "warning");
        return;
      }
      store.refresh();
      const s = store;
      await ctx.ui.custom<void>(
        (tui, theme, _keybindings, done) => {
          viewTui = tui;
          return new ChildrenView(s, tui, theme, () => done(undefined));
        },
        { overlay: true, overlayOptions: { width: "90%", maxHeight: "80%", anchor: "center" } },
      );
      viewTui = undefined;
    },
  });
}
