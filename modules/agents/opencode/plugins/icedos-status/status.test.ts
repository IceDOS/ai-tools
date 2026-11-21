// Unit tests for icedos-status pure helpers using node:test.
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_IDLE_GLYPH,
  DEFAULT_WORKING_GLYPHS,
  DEFAULT_FALLBACK_TITLE,
  createToolTracker,
  recordToolName,
  startTool,
  finishTool,
  clearExecutionTools,
  composeVpnRow,
  getOldestRunningTool,
  formatDuration,
  formatTime,
  composeTurnEnd,
  formatToolDuration,
  shortTool,
  composeToolTimer,
  stripControlChars,
  clipTitle,
  pickGlyph,
  composeTitle,
  composeRlmRow,
  parseChildLog,
  parseRlmChildrenRow,
  readChildLogSample,
  updateToolProgress,
} from "./lib.ts";

test("formatDuration formats seconds under a minute", () => {
  assert.equal(formatDuration(0), "0s");
  assert.equal(formatDuration(1), "1s");
  assert.equal(formatDuration(42), "42s");
  assert.equal(formatDuration(59), "59s");
  assert.equal(formatDuration(42.8), "42s");
  assert.equal(formatDuration(-5), "0s");
});

test("formatDuration formats minutes and seconds under an hour", () => {
  assert.equal(formatDuration(60), "1m");
  assert.equal(formatDuration(77), "1m 17s");
  assert.equal(formatDuration(119), "1m 59s");
  assert.equal(formatDuration(120), "2m");
  assert.equal(formatDuration(3599), "59m 59s");
});

test("formatDuration formats hours and minutes for >= 1 hour", () => {
  assert.equal(formatDuration(3600), "1h");
  assert.equal(formatDuration(3720), "1h 2m");
  assert.equal(formatDuration(3725), "1h 2m");
  assert.equal(formatDuration(7200), "2h");
  assert.equal(formatDuration(7380), "2h 3m");
});

test("formatTime formats 12-hour AM/PM with zero-padded minutes", () => {
  assert.equal(formatTime(new Date(2026, 10, 18, 9, 49)), "9:49 AM");
  assert.equal(formatTime(new Date(2026, 10, 18, 21, 49)), "9:49 PM");
  assert.equal(formatTime(new Date(2026, 10, 18, 0, 0)), "12:00 AM");
  assert.equal(formatTime(new Date(2026, 10, 18, 12, 0)), "12:00 PM");
  assert.equal(formatTime(new Date(2026, 10, 18, 12, 5)), "12:05 PM");
  assert.equal(formatTime(new Date(2026, 10, 18, 23, 59)), "11:59 PM");
});

test("composeTurnEnd builds canonical status message", () => {
  const at = new Date(2026, 10, 18, 21, 49);
  assert.equal(composeTurnEnd(77, at, "▲"), "▲ Worked for 1m 17s · done 9:49 PM");
  assert.equal(composeTurnEnd(42, at, "○"), "○ Worked for 42s · done 9:49 PM");
});

test("formatToolDuration formats ms identical to pi-agent", () => {
  assert.equal(formatToolDuration(4200), "4.2s");
  assert.equal(formatToolDuration(500), "0.5s");
  assert.equal(formatToolDuration(60000), "1m 0s");
  assert.equal(formatToolDuration(90000), "1m 30s");
  assert.equal(formatToolDuration(3661000), "1h 1m 1s");
});

test("shortTool strips mcp prefix", () => {
  assert.equal(shortTool("mcp__server__read_file"), "read_file");
  assert.equal(shortTool("bash"), "bash");
});

test("composeToolTimer formats tool name and duration", () => {
  assert.equal(composeToolTimer("bash", 4200), "⏱ bash 4.2s");
  assert.equal(composeToolTimer("mcp__fs__read", 1500), "⏱ read 1.5s");
});

test("stripControlChars replaces control chars with space", () => {
  assert.equal(stripControlChars("foo\x00bar\x1bbaz"), "foo bar baz");
});

test("clipTitle bounds length and trims on word boundary", () => {
  assert.equal(clipTitle("short title"), "short title");
  const long = clipTitle("hello world this is a very long title that exceeds the maximum character count", 30);
  assert.ok(long.length <= 30);
  assert.ok(long.endsWith("…"));
});

test("pickGlyph cycles working glyphs when running and returns idle when not", () => {
  assert.equal(pickGlyph(false, 0, "▲", ["▖", "▘", "▝", "▗"]), "▲");
  assert.equal(pickGlyph(true, 0, "▲", ["▖", "▘", "▝", "▗"]), "▖");
  assert.equal(pickGlyph(true, 1, "▲", ["▖", "▘", "▝", "▗"]), "▘");
  assert.equal(pickGlyph(true, 4, "▲", ["▖", "▘", "▝", "▗"]), "▖");
});

test("composeTitle combines glyph and title with fallback", () => {
  assert.equal(composeTitle("▲", "My Session"), "▲ My Session");
  assert.equal(composeTitle("▲", "", "OpenCode"), "▲ OpenCode");
  assert.equal(composeTitle("▲", undefined, "OpenCode"), "▲ OpenCode");
  assert.equal(composeTitle("▲", "My Session", undefined, "⏱ bash 12s"), "▲ My Session · ⏱ bash 12s");
  assert.equal(composeTitle("▲", "My Session", "OpenCode", "⏱ bash 1.2s"), "▲ My Session · ⏱ bash 1.2s");
  assert.equal(composeTitle("▲", "", "OpenCode", "⏱ read 0.5s"), "▲ OpenCode · ⏱ read 0.5s");
});

test("composeTitle clips long title keeping tool timer within 80 chars", () => {
  const longTitle = "a".repeat(100);
  const title = composeTitle("▲", longTitle, "OpenCode", "⏱ bash 12s");
  assert.equal(title.length, 80);
  assert.ok(title.endsWith("… · ⏱ bash 12s"));
  assert.ok(title.startsWith("▲ aaaa"));
});

test("turn execution duration calculation", () => {
  const start = 100000;
  const end = 177000;
  const elapsed = Math.max(0, Math.round((end - start) / 1000));
  const at = new Date(2026, 10, 18, 21, 49);
  assert.equal(composeTurnEnd(elapsed, at, "▲"), "▲ Worked for 1m 17s · done 9:49 PM");
});

test("tool tracker: id-keying, name recording, start, finish and oldest lookup", () => {
  const tracker = createToolTracker();
  // 1. Tool input started provides id, name and sessionID
  recordToolName(tracker, "call_1", "bash", "ses_1");
  assert.equal(tracker.names["call_1"], "bash");

  // 2. Tool called starts timer keyed by id
  startTool(tracker, "call_1", "ses_1", 1000);
  assert.equal(tracker.running["call_1"]?.tool, "bash");
  assert.equal(tracker.running["call_1"]?.start, 1000);

  // 3. Second tool with unknown name falls back to "tool"
  startTool(tracker, "call_2", "ses_1", 2000);
  assert.equal(tracker.running["call_2"]?.tool, "tool");

  // 4. Oldest running tool selection
  const oldest = getOldestRunningTool(tracker, "ses_1");
  assert.ok(oldest);
  assert.equal(oldest.id, "call_1");
  assert.equal(oldest.tool, "bash");

  // 5. Tool finished clears by id
  finishTool(tracker, "call_1");
  assert.equal(tracker.running["call_1"], undefined);
  assert.equal(tracker.names["call_1"], undefined);

  // 6. Oldest is now call_2
  const nextOldest = getOldestRunningTool(tracker, "ses_1");
  assert.ok(nextOldest);
  assert.equal(nextOldest.id, "call_2");

  // 7. Tool input started for call_3 but never called before execution end
  recordToolName(tracker, "call_3", "read_file", "ses_1");
  assert.equal(tracker.names["call_3"], "read_file");

  // 8. Execution end clears remaining tools and name map for session
  clearExecutionTools(tracker, "ses_1");
  assert.equal(tracker.running["call_2"], undefined);
  assert.equal(tracker.names["call_3"], undefined);
  assert.equal(getOldestRunningTool(tracker, "ses_1"), undefined);
});

test("composeVpnRow mirrors pi's vpn row", () => {
  assert.equal(composeVpnRow(null, 0), undefined);
  assert.equal(composeVpnRow({ rotating: true }, 0), "vpn rotating…");
  assert.equal(composeVpnRow({ rotating: true, model: "m-free" }, 0), "vpn rotating for m-free…");
  const limited = { allRateLimited: true, refreshIn: 30, at: 1000_000, model: "m-free", mode: "direct" };
  assert.equal(composeVpnRow(limited, 1010), "vpn all rate-limited for m-free (refresh in 20s)");
  assert.equal(composeVpnRow(limited, 1031), "vpn direct");
  assert.equal(
    composeVpnRow({ mode: "tunnel", server: "CH#524", country: "CH", egressIp: "1.2.3.4" }, 0),
    "vpn tunnel CH#524 CH 1.2.3.4",
  );
  assert.equal(composeVpnRow({ mode: "direct", hostIpFlaggedUntil: 100 }, 50), "vpn direct (host ip flagged)");
  assert.equal(composeVpnRow({ mode: "direct", hostIpFlaggedUntil: 100 }, 150), "vpn direct");
});

test("parseChildLog: identifies running, ended, and dead pid child logs", () => {
  const runningLog = JSON.stringify({ type: "rlm_child_start", id: "c1", ts: 1000, pid: 1234 }) + "\n";
  assert.deepEqual(parseChildLog(runningLog, () => true), { running: true, started: 1000, pid: 1234 });

  const endedLog =
    JSON.stringify({ type: "rlm_child_start", id: "c1", ts: 1000, pid: 1234 }) +
    "\n" +
    JSON.stringify({ type: "step_update", text: "step 1" }) +
    "\n" +
    JSON.stringify({ type: "rlm_child_end", id: "c1", ok: true, ts: 1025 }) +
    "\n";
  assert.deepEqual(parseChildLog(endedLog, () => true), { running: false, started: 1000, pid: 1234 });

  const deadPidLog = JSON.stringify({ type: "rlm_child_start", id: "c2", ts: 1000, pid: 99999 }) + "\n";
  assert.deepEqual(parseChildLog(deadPidLog, (pid) => pid !== 99999), { running: false, started: 1000, pid: 99999 });

  assert.deepEqual(parseChildLog(""), { running: false });
});

test("composeRlmRow formats live child status", () => {
  assert.equal(composeRlmRow(0), undefined);
  assert.equal(composeRlmRow(-1), undefined);
  assert.equal(composeRlmRow(1), "rlm 1 running");
  assert.equal(composeRlmRow(1, 15), "rlm 1 running (15s)");
  assert.equal(composeRlmRow(2, 75), "rlm 2 running (1m 15s)");
});

test("parseRlmChildrenRow aggregates running logs with oldest elapsed time", () => {
  const child1 = JSON.stringify({ type: "rlm_child_start", id: "c1", ts: 1000 }) + "\n";
  const child2 = JSON.stringify({ type: "rlm_child_start", id: "c2", ts: 1010 }) + "\n";
  const child3 =
    JSON.stringify({ type: "rlm_child_start", id: "c3", ts: 990 }) +
    "\n" +
    JSON.stringify({ type: "rlm_child_end", id: "c3", ok: true, ts: 1005 }) +
    "\n";

  const row = parseRlmChildrenRow([child1, child2, child3], 1020);
  assert.equal(row, "rlm 2 running (20s)");

  assert.equal(parseRlmChildrenRow([child3], 1020), undefined);
  assert.equal(parseRlmChildrenRow([], 1020), undefined);
});

test("parseChildLog: identifies end record in a large tail after thousands of intermediate lines", () => {
  const start = JSON.stringify({ type: "rlm_child_start", id: "c_large", ts: 1000, pid: 1234 }) + "\n";
  const intermediate = ('{"type":"step_update","text":"' + "x".repeat(100) + '"}\n').repeat(500);
  const end = JSON.stringify({ type: "rlm_child_end", id: "c_large", ok: true, ts: 1060 }) + "\n";

  const fullLog = start + intermediate + end;
  const sampled = start + fullLog.slice(-4096);
  assert.deepEqual(parseChildLog(sampled, () => true), { running: false, started: 1000, pid: 1234 });
});

test("readChildLogSample: reads head and tail bounded from large file (>8 KB)", () => {
  const tmpDir = mkdtempSync(path.join(os.tmpdir(), "icedos-test-"));
  const tmpFile = path.join(tmpDir, "child.jsonl");
  const start = JSON.stringify({ type: "rlm_child_start", id: "c_bounded", ts: 1000, pid: 1234 }) + "\n";
  const intermediate = ('{"type":"step_update","text":"' + "y".repeat(100) + '"}\n').repeat(100);
  const end = JSON.stringify({ type: "rlm_child_end", id: "c_bounded", ok: true, ts: 1050 }) + "\n";
  const content = start + intermediate + end;
  assert.ok(content.length > 8192);
  writeFileSync(tmpFile, content);
  try {
    const sampled = readChildLogSample(tmpFile);
    assert.ok(sampled.length < content.length);
    assert.ok(sampled.startsWith(start));
    assert.ok(sampled.endsWith(end));
    assert.deepEqual(parseChildLog(sampled, () => true), { running: false, started: 1000, pid: 1234 });
  } finally {
    rmSync(tmpDir, { recursive: true, force: true });
  }
});

test("readChildLogSample: reads start line exceeding 4 KB of escaped unicode chars (>8 KB file)", () => {
  const tmpDir = mkdtempSync(path.join(os.tmpdir(), "icedos-test-"));
  const tmpFile = path.join(tmpDir, "child-unicode.jsonl");
  const start = '{"type":"rlm_child_start","id":"c_uni","ts":1000,"pid":1234,"prompt":"' + "\\u03b1".repeat(1500) + '"}\n';
  const intermediate = ('{"type":"step_update","text":"' + "z".repeat(100) + '"}\n').repeat(50);
  const end = JSON.stringify({ type: "rlm_child_end", id: "c_uni", ok: true, ts: 1060 }) + "\n";
  const content = start + intermediate + end;
  assert.ok(start.length > 4096);
  assert.ok(content.length > 8192);
  writeFileSync(tmpFile, content);
  try {
    const sampled = readChildLogSample(tmpFile);
    assert.ok(sampled.startsWith(start));
    assert.ok(sampled.endsWith(end));
    assert.deepEqual(parseChildLog(sampled, () => true), { running: false, started: 1000, pid: 1234 });
  } finally {
    rmSync(tmpDir, { recursive: true, force: true });
  }
});

test("updateToolProgress: updates tool name for nested CodeMode calls and reverts on completion", () => {
  const tracker = createToolTracker();
  recordToolName(tracker, "call_exec", "execute", "sess_1");
  startTool(tracker, "call_exec", "sess_1", 1000);

  const t1 = getOldestRunningTool(tracker, "sess_1");
  assert.ok(t1);
  assert.equal(t1.tool, "execute");
  assert.equal(composeToolTimer(t1.tool, 12000), "⏱ execute 12.0s");

  // CodeMode tool.before emits progress with running nested tool
  updateToolProgress(tracker, "call_exec", {
    toolCalls: [{ tool: "kernel.ipython", status: "running" }],
  });
  const t2 = getOldestRunningTool(tracker, "sess_1");
  assert.ok(t2);
  assert.equal(t2.tool, "kernel.ipython");
  assert.equal(composeToolTimer(t2.tool, 12000), "⏱ kernel.ipython 12.0s");

  // CodeMode tool.after emits progress with completed nested tool -> reverts to execute
  updateToolProgress(tracker, "call_exec", {
    toolCalls: [{ tool: "kernel.ipython", status: "completed" }],
  });
  const t3 = getOldestRunningTool(tracker, "sess_1");
  assert.ok(t3);
  assert.equal(t3.tool, "execute");
  assert.equal(composeToolTimer(t3.tool, 15000), "⏱ execute 15.0s");

  // finishTool clears the tracker
  finishTool(tracker, "call_exec");
  assert.equal(getOldestRunningTool(tracker, "sess_1"), undefined);
});
