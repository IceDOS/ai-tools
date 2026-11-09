import { after, test } from "node:test";
import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { lib, type Entry } from "../extensions/rate-limit-hook.ts";

const failed = (errorMessage: string, provider = "opencode") => ({
  role: "assistant",
  stopReason: "error",
  errorMessage,
  provider,
  model: "m",
});
const fresh = () => ({ timeouts: 0, ran: false });
const entry = (over: Partial<Entry> = {}): Entry => ({
  command: ["true"],
  providers: ["opencode"],
  errorKinds: ["rate_limit"],
  timeoutMs: 120000,
  ...over,
});
const ctx = { cwd: "/w", sessionManager: { getSessionId: () => "s1" }, hasUI: false, ui: { notify() {} } };

const tempDirs: string[] = [];
after(() => {
  for (const dir of tempDirs) {
    try { rmSync(dir, { recursive: true, force: true }); } catch {}
  }
});

function script(body: string) {
  const dir = mkdtempSync(join(tmpdir(), "rate-limit-hook-"));
  tempDirs.push(dir);
  const path = join(dir, "hook.sh");
  writeFileSync(path, `#!/bin/sh\n${body}\n`);
  chmodSync(path, 0o755);
  return { dir, path };
}

test("classify sorts errors into kinds", () => {
  assert.equal(lib.classify(failed("429 Too Many Requests"), fresh()), "rate_limit");
  assert.equal(lib.classify(failed("Rate limit exceeded"), fresh()), "rate_limit");
  assert.equal(lib.classify(failed("FreeUsageLimitError: 429"), fresh()), "usage_limit");
  assert.equal(lib.classify(failed("HTTP 404 not found"), fresh()), "not_found");
  assert.equal(lib.classify(failed("request id 14290"), fresh()), null);
  assert.equal(lib.classify({ role: "assistant", stopReason: "stop" }, fresh()), null);
  assert.equal(lib.classify({ role: "user", stopReason: "error", errorMessage: "429" }, fresh()), null);
});

test("only the second timeout in a prompt counts", () => {
  const state = fresh();
  assert.equal(lib.classify(failed("request timed out"), state), null);
  assert.equal(lib.classify(failed("ETIMEDOUT"), state), "timeout");
  assert.equal(lib.classify(failed("request timed out"), state), null);
});

test("entriesFor filters by provider and kind", () => {
  const hooks = [
    entry({ command: ["a"] }),
    entry({ command: ["b"], providers: ["other"] }),
    entry({ command: ["c"], errorKinds: ["usage_limit", "rate_limit"] }),
  ];
  const names = (provider: string, kind: string) =>
    lib.entriesFor(hooks, provider, kind as any).map((e) => e.command[0]);
  assert.deepEqual(names("opencode", "rate_limit"), ["a", "c"]);
  assert.deepEqual(names("opencode", "usage_limit"), ["c"]);
  assert.deepEqual(names("other", "rate_limit"), ["b"]);
  assert.deepEqual(names("other", "not_found"), []);
});

test("payload has the documented keys", () => {
  assert.deepEqual(lib.payload(ctx, failed("429"), "rate_limit"), {
    hook_event_name: "RateLimit",
    notification_type: "",
    cwd: "/w",
    session_id: "s1",
    permission_mode: "",
    provider: "opencode",
    model: "m",
    errorKind: "rate_limit",
    errorMessage: "429",
  });
});

test("runHook passes the payload on stdin and reports exit 0", async () => {
  const { dir, path } = script(`cat > "$(dirname "$0")/stdin"`);
  assert.equal(await lib.runHook(entry({ command: [path] }), '{"x":1}'), true);
  assert.equal(readFileSync(join(dir, "stdin"), "utf8"), '{"x":1}');
});

test("runHook counts a non-zero exit and a missing program as failures", async () => {
  const { path } = script("exit 1");
  assert.equal(await lib.runHook(entry({ command: [path] }), "{}"), false);
  assert.equal(await lib.runHook(entry({ command: ["/nonexistent/hook"] }), "{}"), false);
});

test("runHook kills the whole process group on timeout", async () => {
  const { dir, path } = script(`sleep 30 & echo $! > "$(dirname "$0")/child"; wait`);
  const started = Date.now();
  assert.equal(await lib.runHook(entry({ command: [path], timeoutMs: 300 }), "{}"), false);
  assert.ok(Date.now() - started < 5000);
  await new Promise((resolve) => setTimeout(resolve, 300));
  const child = Number(readFileSync(join(dir, "child"), "utf8"));
  assert.throws(() => process.kill(child, 0));
});

test("runHook kills the whole process group on SIGINT", async () => {
  const { dir, path } = script(`sleep 30 & echo $! > "$(dirname "$0")/child"; wait`);
  const promise = lib.runHook(entry({ command: [path], timeoutMs: 10000 }), "{}");
  await new Promise((resolve) => setTimeout(resolve, 100));
  process.emit("SIGINT");
  assert.equal(await promise, false);
  await new Promise((resolve) => setTimeout(resolve, 300));
  const child = Number(readFileSync(join(dir, "child"), "utf8"));
  assert.throws(() => process.kill(child, 0));
});

test("runHook kills the whole process group on abort signal", async () => {
  const { dir, path } = script(`sleep 30 & echo $! > "$(dirname "$0")/child"; wait`);
  const controller = new AbortController();
  const promise = lib.runHook(entry({ command: [path], timeoutMs: 10000 }), "{}", controller.signal);
  await new Promise((resolve) => setTimeout(resolve, 100));
  controller.abort();
  assert.equal(await promise, false);
  await new Promise((resolve) => setTimeout(resolve, 300));
  const child = Number(readFileSync(join(dir, "child"), "utf8"));
  assert.throws(() => process.kill(child, 0));
});

test("graceful SIGKILL escalation kills descendants that ignore SIGTERM even if leader exits", async () => {
  const { dir, path } = script(`(trap '' TERM; exec sleep 20) & echo $! > "$(dirname "$0")/child"; wait`);
  const promise = lib.runHook(entry({ command: [path], timeoutMs: 10000 }), "{}");
  await new Promise((resolve) => setTimeout(resolve, 100));
  process.emit("SIGINT");
  assert.equal(await promise, false);
  await new Promise((resolve) => setTimeout(resolve, 300));
  const child = Number(readFileSync(join(dir, "child"), "utf8"));
  assert.doesNotThrow(() => process.kill(child, 0));
  await new Promise((resolve) => setTimeout(resolve, 2000));
  assert.throws(() => process.kill(child, 0));
});

test("runHook with already-aborted signal returns false immediately", async () => {
  const controller = new AbortController();
  controller.abort();
  assert.equal(await lib.runHook(entry(), "{}", controller.signal), false);
});

test("SIGINT during hook cancels messageEnd loop without running next hook", async () => {
  const seen: string[] = [];
  const handlers = lib.createHandlers([entry({ command: ["a"] }), entry({ command: ["b"] })], async (e) => {
    seen.push(e.command[0]);
    process.emit("SIGINT");
    return false;
  });
  assert.equal(await handlers.messageEnd({ message: failed("429") }, ctx), undefined);
  assert.deepEqual(seen, ["a"]);
});

test("notification mentions Esc to cancel", async () => {
  const notes: string[] = [];
  const mockCtx = { ...ctx, hasUI: true, ui: { notify(m: string) { notes.push(m); } } };
  const handlers = lib.createHandlers([entry()], async () => true);
  await handlers.messageEnd({ message: failed("429") }, mockCtx);
  assert.ok(notes.some((n) => n.includes("press Esc to cancel")));
});

test("a hook that exits 0 turns an error pi would not retry into one it retries", async () => {
  const { isRetryableAssistantError } = await import(process.env.PI_AI_RETRY!);
  const handlers = lib.createHandlers([entry({ errorKinds: ["usage_limit"] })], async () => true);
  const original = failed("FreeUsageLimitError: free usage exceeded");
  const result = await handlers.messageEnd({ message: original }, ctx);
  assert.equal(result?.message.errorMessage, "rate limit: opencode hook ran (usage_limit), retrying");
  assert.equal(isRetryableAssistantError(original), false);
  assert.equal(isRetryableAssistantError(result!.message), true);
});

test("one hook run per prompt until agent_settled", async () => {
  let runs = 0;
  const handlers = lib.createHandlers([entry()], async () => {
    runs += 1;
    return true;
  });
  assert.ok(await handlers.messageEnd({ message: failed("429") }, ctx));
  assert.equal(await handlers.messageEnd({ message: failed("429") }, ctx), undefined);
  assert.equal(runs, 1);
  handlers.settled();
  assert.ok(await handlers.messageEnd({ message: failed("429") }, ctx));
  assert.equal(runs, 2);
});

test("failed hooks leave the message alone and every matching entry is tried", async () => {
  const seen: string[] = [];
  const handlers = lib.createHandlers([entry({ command: ["a"] }), entry({ command: ["b"] })], async (e) => {
    seen.push(e.command[0]);
    return false;
  });
  assert.equal(await handlers.messageEnd({ message: failed("429") }, ctx), undefined);
  assert.deepEqual(seen, ["a", "b"]);
});

test("other providers, other kinds and successes run nothing", async () => {
  let runs = 0;
  const handlers = lib.createHandlers([entry()], async () => {
    runs += 1;
    return true;
  });
  assert.equal(await handlers.messageEnd({ message: failed("429", "anthropic") }, ctx), undefined);
  assert.equal(await handlers.messageEnd({ message: failed("FreeUsageLimitError") }, ctx), undefined);
  assert.equal(await handlers.messageEnd({ message: { role: "assistant", stopReason: "stop" } }, ctx), undefined);
  assert.equal(runs, 0);
});
