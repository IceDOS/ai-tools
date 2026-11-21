// Pure helpers for icedos-status plugin: time formatting, title composition, glyph selection.
import { closeSync, fstatSync, openSync, readSync } from "node:fs";

export const DEFAULT_IDLE_GLYPH = "▣";
export const DEFAULT_WORKING_GLYPHS = ["◰", "◳", "◲", "◱"];
export const DEFAULT_FALLBACK_TITLE = "OpenCode";

export interface RunningTool {
  id: string;
  tool: string;
  sessionID: string;
  start: number;
}

export interface ToolTrackerState {
  names: Record<string, string>;
  toolSession: Record<string, string>;
  running: Record<string, RunningTool>;
}

export function createToolTracker(): ToolTrackerState {
  return { names: {}, toolSession: {}, running: {} };
}

export function recordToolName(
  state: ToolTrackerState,
  id: string,
  name: string,
  sessionID?: string,
): void {
  state.names[id] = name;
  if (sessionID) state.toolSession[id] = sessionID;
}

export function startTool(
  state: ToolTrackerState,
  id: string,
  sessionID: string,
  now = Date.now(),
): RunningTool {
  const toolName = state.names[id] || "tool";
  state.toolSession[id] = sessionID;
  const tool: RunningTool = { id, tool: toolName, sessionID, start: now };
  state.running[id] = tool;
  return tool;
}

export function finishTool(state: ToolTrackerState, id: string): void {
  delete state.running[id];
  delete state.names[id];
  delete state.toolSession[id];
}

export function clearExecutionTools(state: ToolTrackerState, sessionID: string): void {
  for (const [id, tool] of Object.entries(state.running)) {
    if (tool.sessionID === sessionID) {
      delete state.running[id];
      delete state.names[id];
      delete state.toolSession[id];
    }
  }
  for (const [id, sid] of Object.entries(state.toolSession)) {
    if (sid === sessionID) {
      delete state.names[id];
      delete state.toolSession[id];
    }
  }
}

export function getOldestRunningTool(
  state: ToolTrackerState,
  sessionID?: string,
): RunningTool | undefined {
  const list = Object.values(state.running).filter((t) => !sessionID || t.sessionID === sessionID);
  if (list.length === 0) return undefined;
  return list.reduce((oldest, cur) => (cur.start < oldest.start ? cur : oldest));
}

export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  if (total < 60) return `${total}s`;
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return m > 0 ? `${h}h ${m}m` : `${h}h`;
  return s > 0 ? `${m}m ${s}s` : `${m}m`;
}

export function formatTime(date: Date = new Date(Date.now())): string {
  let hours = date.getHours();
  const minutes = date.getMinutes();
  const period = hours >= 12 ? "PM" : "AM";
  hours = hours % 12;
  if (hours === 0) hours = 12;
  const minuteStr = minutes < 10 ? `0${minutes}` : `${minutes}`;
  return `${hours}:${minuteStr} ${period}`;
}

export function composeTurnEnd(
  elapsedSeconds: number,
  date: Date = new Date(Date.now()),
  idleGlyph = DEFAULT_IDLE_GLYPH,
): string {
  return `${idleGlyph} Worked for ${formatDuration(elapsedSeconds)} · done ${formatTime(date)}`;
}

export function formatToolDuration(ms: number): string {
  const seconds = Math.max(0, ms) / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const total = Math.floor(seconds);
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m ${total % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m ${total % 60}s`;
}

export function shortTool(name: string): string {
  return name.startsWith("mcp__") ? name.slice(name.lastIndexOf("__") + 2) : name;
}

export function composeToolTimer(tool: string, elapsedMs: number): string {
  return `⏱ ${shortTool(tool)} ${formatToolDuration(elapsedMs)}`;
}

export function stripControlChars(text: string): string {
  return text.replace(/[\u0000-\u001f\u007f-\u009f]+/g, " ");
}

export function clipTitle(text: string, max = 80): string {
  const flat = text.replace(/\s+/g, " ").trim();
  if (flat.length <= max) return flat;
  const cut = flat.slice(0, max - 1);
  const space = cut.lastIndexOf(" ");
  return `${(space > max / 2 ? cut.slice(0, space) : cut).trimEnd()}…`;
}

export function pickGlyph(
  running: boolean,
  tick: number,
  idleGlyph = DEFAULT_IDLE_GLYPH,
  workingGlyphs = DEFAULT_WORKING_GLYPHS,
): string {
  if (!running || workingGlyphs.length === 0) return idleGlyph;
  return workingGlyphs[tick % workingGlyphs.length] ?? idleGlyph;
}

export function composeTitle(
  glyph: string,
  title?: string,
  fallback = DEFAULT_FALLBACK_TITLE,
): string {
  const cleanTitle = title ? stripControlChars(clipTitle(title)) : "";
  const effective = cleanTitle.length > 0 ? cleanTitle : fallback;
  return `${glyph} ${effective}`.trim();
}

// Status row from the VPN rotation service's state file, as pi's vpn-row shows it.
export function composeVpnRow(s: any, nowSec: number): string | undefined {
  if (!s || typeof s !== "object") return undefined;
  if (s.rotating) return `vpn rotating${s.model ? ` for ${s.model}` : ""}…`;
  // refreshIn counts from the file's write time (ms); once it runs out the row shows the live mode again.
  const left = Math.ceil((s.refreshIn ?? 0) - (nowSec - (s.at ?? 0) / 1000));
  if (s.allRateLimited && left > 0) return `vpn all rate-limited${s.model ? ` for ${s.model}` : ""} (refresh in ${left}s)`;
  if (s.mode === "tunnel") {
    const where = [s.server, s.country].filter((part) => typeof part === "string" && part !== "").join(" ");
    const ip = typeof s.egressIp === "string" && s.egressIp !== "" ? ` ${s.egressIp}` : "";
    return `vpn tunnel${where ? ` ${where}` : ""}${ip}`;
  }
  // A flagged host IP while direct means the next call will likely be rate-limited.
  const flagged = typeof s.hostIpFlaggedUntil === "number" && s.hostIpFlaggedUntil > nowSec;
  return flagged ? "vpn direct (host ip flagged)" : "vpn direct";
}


export interface ChildLogInfo {
  running: boolean;
  started?: number;
  pid?: number;
}

// Parse a JSONL child log; running until rlm_child_end is logged or spawning pid died.
export function parseChildLog(
  content: string,
  isAlive?: (pid: number) => boolean,
): ChildLogInfo {
  const lines = content.split("\n");
  let started: number | undefined;
  let pid: number | undefined;
  let ended = false;

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const rec = JSON.parse(trimmed);
      if (!rec || typeof rec !== "object") continue;
      if (rec.type === "rlm_child_start") {
        if (typeof rec.ts === "number") started = rec.ts;
        if (typeof rec.pid === "number") pid = rec.pid;
      } else if (rec.type === "rlm_child_end") {
        ended = true;
      }
    } catch {
      // Ignore half-written or non-JSON stream lines
    }
  }

  if (ended) {
    return { running: false, started, pid };
  }

  if (started === undefined && pid === undefined && lines.filter((l) => l.trim()).length === 0) {
    return { running: false };
  }

  if (pid !== undefined && isAlive && !isAlive(pid)) {
    return { running: false, started, pid };
  }

  return { running: true, started, pid };
}

export function composeRlmRow(
  runningCount: number,
  oldestElapsedSec?: number,
): string | undefined {
  if (runningCount <= 0) return undefined;
  if (oldestElapsedSec !== undefined && oldestElapsedSec >= 0) {
    return `rlm ${runningCount} running (${formatDuration(oldestElapsedSec)})`;
  }
  return `rlm ${runningCount} running`;
}

export function parseRlmChildrenRow(
  logs: string[],
  nowSec: number = Date.now() / 1000,
  isAlive?: (pid: number) => boolean,
): string | undefined {
  let runningCount = 0;
  let oldestStart: number | undefined;

  for (const log of logs) {
    const info = parseChildLog(log, isAlive);
    if (info.running) {
      runningCount++;
      if (info.started !== undefined) {
        if (oldestStart === undefined || info.started < oldestStart) {
          oldestStart = info.started;
        }
      }
    }
  }

  if (runningCount === 0) return undefined;
  const elapsed = oldestStart !== undefined ? Math.max(0, Math.floor(nowSec - oldestStart)) : undefined;
  return composeRlmRow(runningCount, elapsed);
}



// Bounded read for child logs: read first line (start record, capped at 64 KB) and last 4 KB (end record).
export function readChildLogSample(file: string): string {
  try {
    const fd = openSync(file, "r");
    try {
      const stat = fstatSync(fd);
      if (stat.size === 0) return "";
      if (stat.size <= 8192) {
        const buf = Buffer.alloc(stat.size);
        readSync(fd, buf, 0, stat.size, 0);
        return buf.toString("utf8");
      }
      // Read first line in chunks until newline, capped at 64 KB
      let firstLine = "";
      let pos = 0;
      const chunkSize = 4096;
      const maxHead = Math.min(stat.size, 65536);
      const chunk = Buffer.alloc(chunkSize);
      while (pos < maxHead) {
        const toRead = Math.min(chunkSize, maxHead - pos);
        const n = readSync(fd, chunk, 0, toRead, pos);
        if (n === 0) break;
        const str = chunk.toString("utf8", 0, n);
        const nl = str.indexOf("\n");
        if (nl !== -1) {
          firstLine += str.slice(0, nl + 1);
          break;
        }
        firstLine += str;
        pos += n;
      }
      if (!firstLine.endsWith("\n") && firstLine.length > 0) {
        firstLine += "\n";
      }
      // Read last 4 KB for rlm_child_end
      const tailSize = Math.min(4096, stat.size);
      const tailBuf = Buffer.alloc(tailSize);
      const tailBytes = readSync(fd, tailBuf, 0, tailSize, stat.size - tailSize);
      const tail = tailBuf.toString("utf8", 0, tailBytes);
      return firstLine + tail;
    } finally {
      closeSync(fd);
    }
  } catch {
    return "";
  }
}


// Update running tool name from nested CodeMode calls in session.tool.progress.
export function updateToolProgress(
  state: ToolTrackerState,
  id: string,
  metadata?: any,
): void {
  if (!metadata || typeof metadata !== "object") return;
  const calls = Array.isArray(metadata.toolCalls) ? metadata.toolCalls : undefined;
  if (calls && calls.length > 0) {
    const runningNested = [...calls].reverse().find((c) => c && c.status === "running");
    if (runningNested && typeof runningNested.tool === "string" && runningNested.tool) {
      if (state.running[id]) {
        state.running[id].tool = runningNested.tool;
      } else {
        state.names[id] = runningNested.tool;
      }
      return;
    }
  }
  if (state.running[id]) {
    state.running[id].tool = state.names[id] || "tool";
  }
}
