import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn, execFileSync } from "node:child_process";
import { Relay, capText, formatMessage, type Message } from "../core.ts";
import { resolveTarget, RingBuffer, formatDuration } from "../cli.ts";
import { runHook } from "../hook.ts";
import { peersText } from "../tools.ts";
import { tmpRelay } from "./helpers.ts";

const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");
const path = process.getBuiltinModule("node:path") as typeof import("node:path");

const cliPath = path.resolve(import.meta.dirname, "../cli.ts");

function runCliCmd(args: string[], env: Record<string, string | undefined> = {}, stdinText?: string) {
  try {
    const stdout = execFileSync(process.execPath, [cliPath, ...args], {
      env: { ...process.env, ...env },
      input: stdinText,
      encoding: "utf8",
      stdio: ["pipe", "pipe", "pipe"],
    });
    return { code: 0, stdout, stderr: "" };
  } catch (err: any) {
    return {
      code: err.status ?? (err.signal ? 128 + 15 : 1),
      stdout: err.stdout?.toString() ?? "",
      stderr: err.stderr?.toString() ?? "",
    };
  }
}

test("target resolution order, ambiguous --to, no target exits 1", () => {
  const { relay } = tmpRelay();
  const a = relay.register({ kind: "claude", pid: 100, cwd: "/a", id: "aaaa1", name: "auth" });
  relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "aaaa2", name: "api" });
  const c = relay.register({ kind: "agy", pid: 300, cwd: "/c", id: "cccc3" });
  relay.register({ kind: "script", pid: 400, cwd: "/s", id: "ssss4" });

  assert.equal(resolveTarget(relay, "cccc"), c.id);
  assert.equal(resolveTarget(relay, "auth"), a.id);
  assert.throws(() => resolveTarget(relay, "aaaa"), /"aaaa" is ambiguous/);
  assert.throws(() => resolveTarget(relay, "none"), /no session matches "none"/);
  assert.throws(() => resolveTarget(relay, "ssss"), /no session matches "ssss"/);

  const envWithTarget = { AGENT_RELAY_TARGET: "cccc3" };
  assert.equal(resolveTarget(relay, "auth", envWithTarget), a.id);
  assert.equal(resolveTarget(relay, undefined, envWithTarget), c.id);

  assert.equal(resolveTarget(relay, undefined, {}, 100, (p) => [p]), a.id);
  assert.throws(() => resolveTarget(relay, undefined, {}, 999, (p) => [p]), /no target session found/);
});

test("CLI target lookup via binary exits 1 on ambiguous or missing target", () => {
  const { relay, root, alive } = tmpRelay();
  const p1 = spawn("sleep", ["30"]);
  const p2 = spawn("sleep", ["30"]);
  alive.add(p1.pid!);
  alive.add(p2.pid!);
  relay.register({ kind: "claude", pid: p1.pid!, cwd: "/a", id: "aaaa1" });
  relay.register({ kind: "claude", pid: p2.pid!, cwd: "/b", id: "aaaa2" });

  try {
    const amb = runCliCmd(["id", "--to", "aaaa"], { AGENT_RELAY_ROOT: root });
    assert.equal(amb.code, 1);
    assert.match(amb.stderr, /ambiguous/);

    const missing = runCliCmd(["id", "--to", "zzzz"], { AGENT_RELAY_ROOT: root });
    assert.equal(missing.code, 1);
    assert.match(missing.stderr, /no session matches/);

    const noTarget = runCliCmd(["id"], { AGENT_RELAY_ROOT: root, AGENT_RELAY_TARGET: "" });
    assert.equal(noTarget.code, 1);
    assert.match(noTarget.stderr, /no target session found/);
  } finally {
    p1.kill();
    p2.kill();
  }
});

test("send from args and stdin, message shape, 64 KiB cut", () => {
  const { relay, root, alive } = tmpRelay();
  const p = spawn("sleep", ["30"]);
  alive.add(p.pid!);
  const a = relay.register({ kind: "claude", pid: p.pid!, cwd: "/a", id: "aaaa1" });

  try {
    const res1 = runCliCmd(["send", "--to", "aaaa1", "--name", "my-job", "hello", "world"], { AGENT_RELAY_ROOT: root });
    assert.equal(res1.code, 0);
    assert.equal(res1.stdout.trim(), a.id);

    const inboxA = path.join(root, "inbox", a.id);
    const files1 = fs.readdirSync(inboxA).filter((f) => f.endsWith(".json"));
    assert.equal(files1.length, 1);
    const msg1 = JSON.parse(fs.readFileSync(path.join(inboxA, files1[0]), "utf8")) as Message;
    assert.equal(msg1.v, 1);
    assert.equal(msg1.hop, 0);
    assert.equal(msg1.from.kind, "script");
    assert.equal(msg1.from.name, "my-job");
    assert.equal(msg1.to, a.id);
    assert.equal(msg1.text, "hello world");
    assert.equal(msg1.reply, undefined);
    fs.rmSync(path.join(inboxA, files1[0]));

    const res2 = runCliCmd(["send", "--to", "aaaa1", "-"], { AGENT_RELAY_ROOT: root }, "piped input");
    assert.equal(res2.code, 0);
    const files2 = fs.readdirSync(inboxA).filter((f) => f.endsWith(".json"));
    assert.equal(files2.length, 1);
    const msg2 = JSON.parse(fs.readFileSync(path.join(inboxA, files2[0]), "utf8")) as Message;
    assert.equal(msg2.text, "piped input");
    fs.rmSync(path.join(inboxA, files2[0]));

    const bigText = "x".repeat(70_000);
    const res3 = runCliCmd(["send", "--to", "aaaa1", bigText], { AGENT_RELAY_ROOT: root });
    assert.equal(res3.code, 0);
    const files3 = fs.readdirSync(inboxA).filter((f) => f.endsWith(".json"));
    const msg3 = JSON.parse(fs.readFileSync(path.join(inboxA, files3[0]), "utf8")) as Message;
    assert.match(msg3.text, /\[truncated 4464 bytes\]$/);
    assert.equal(msg3.text.length, 65536 + "\n[truncated 4464 bytes]".length);
  } finally {
    p.kill();
  }
});

test("script message leaves hop/hopAt/hopReset untouched after delivery (markInjected with hop 0)", () => {
  const { relay } = tmpRelay();
  const a = relay.register({ kind: "claude", pid: 100, cwd: "/a", id: "aaaa1" });
  relay.markInjected(a.id, 4);
  const before = relay.get(a.id)!;
  assert.equal(before.hop, 4);
  assert.ok(before.hopAt);

  relay.markInjected(a.id, 0);
  const after0 = relay.get(a.id)!;
  assert.equal(after0.hop, 4);
  assert.equal(after0.hopAt, before.hopAt);

  relay.markInjected(a.id, -1);
  const afterNeg = relay.get(a.id)!;
  assert.equal(afterNeg.hop, 4);
  assert.equal(afterNeg.hopAt, before.hopAt);
});

test("formatMessage with and without reply, and the claude/agy hooks not calling markHuman on it", () => {
  const mNoReply: Message = {
    v: 1,
    id: "m1",
    from: { id: "s1234", kind: "script", name: "backup" },
    to: "aaaa1",
    hop: 0,
    ts: "",
    text: "done dumping db",
  };
  const fmtNoReply = formatMessage(mNoReply);
  assert.equal(fmtNoReply, '[relay from script "backup"]\ndone dumping db');

  const mReply: Message = {
    v: 1,
    id: "m2",
    from: { id: "s1234", kind: "script", name: "asker" },
    to: "aaaa1",
    hop: 0,
    ts: "",
    text: "pick a or b",
    reply: true,
  };
  const fmtReply = formatMessage(mReply);
  assert.equal(fmtReply, '[relay from script "asker"]\npick a or b\n(reply with relay_send to="s1234")');

  const { relay } = tmpRelay();
  const claude = relay.register({ kind: "claude", pid: 200, cwd: "/c" });
  relay.markInjected(claude.id, 3);
  runHook("claude-prompt", JSON.stringify({ prompt: fmtNoReply }), relay, 200, {});
  assert.equal(relay.get(claude.id)!.hopReset, undefined);
  runHook("claude-prompt", JSON.stringify({ prompt: fmtReply }), relay, 200, {});
  assert.equal(relay.get(claude.id)!.hopReset, undefined);

  const agy = relay.register({ kind: "agy", pid: 300, cwd: "/g" });
  relay.markInjected(agy.id, 2);
  runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: fmtNoReply }), relay, 300, {});
  assert.equal(relay.get(agy.id)!.hopReset, undefined);
  runHook("agy-pre-invocation", JSON.stringify({ lastUserInput: fmtReply }), relay, 300, {});
  assert.equal(relay.get(agy.id)!.hopReset, undefined);
});

test("ask: agent relay_send to the script ID works without a group, a third session cannot send to it, timeout exits 124, record removed afterwards", async () => {
  const { relay, root, alive } = tmpRelay();
  const a = relay.register({ kind: "claude", pid: 100, cwd: "/a", id: "aaaa1" });
  const c = relay.register({ kind: "agy", pid: 300, cwd: "/c", id: "cccc3" });

  const script = relay.register({
    kind: "script",
    pid: 200,
    cwd: "/s",
    id: "ssss1",
    agent: { awaiting: a.id },
  });

  const sent = relay.send(a.id, script.id, "approved", 10);
  assert.deepEqual(sent, [script.id]);
  const scriptInbox = path.join(root, "inbox", script.id);
  const files = fs.readdirSync(scriptInbox).filter((f) => f.endsWith(".json"));
  assert.equal(files.length, 1);
  const m = JSON.parse(fs.readFileSync(path.join(scriptInbox, files[0]), "utf8")) as Message;
  assert.equal(m.text, "approved");

  assert.throws(() => relay.send(c.id, script.id, "intruder", 10), /not linked/);

  relay.unregister(script.id);
  assert.equal(relay.get(script.id), undefined);
  assert.ok(!fs.existsSync(scriptInbox));

  const p = spawn("sleep", ["30"]);
  alive.add(p.pid!);
  relay.register({ kind: "claude", pid: p.pid!, cwd: "/live", id: "11111" });

  try {
    const timeoutRes = runCliCmd(["ask", "--to", "11111", "--timeout", "0.05", "quick question"], { AGENT_RELAY_ROOT: root });
    assert.equal(timeoutRes.code, 124);

    const remainingScripts = relay.sessions().filter((s) => s.kind === "script");
    assert.equal(remainingScripts.length, 0);
  } finally {
    p.kill();
  }
});

test("ask: receives reply, prints it, and unregisters cleanly", async () => {
  const { relay, root, alive } = tmpRelay();
  const p = spawn("sleep", ["30"]);
  alive.add(p.pid!);
  const a = relay.register({ kind: "claude", pid: p.pid!, cwd: "/a", id: "aaaa1" });

  const child = spawn(process.execPath, [cliPath, "ask", "--to", "aaaa1", "--timeout", "5", "question"], {
    env: { ...process.env, AGENT_RELAY_ROOT: root },
    stdio: ["pipe", "pipe", "pipe"],
  });
  alive.add(child.pid!);

  try {
    let stdout = "";
    child.stdout.on("data", (d) => (stdout += d.toString()));

    let scriptId = "";
    for (let i = 0; i < 50; i++) {
      const s = relay.sessions().find((rec) => rec.kind === "script" && rec.agent?.awaiting === a.id);
      if (s) {
        scriptId = s.id;
        break;
      }
      await new Promise((r) => setTimeout(r, 20));
    }
    assert.ok(scriptId, "script record should be registered");

    relay.send(a.id, scriptId, "yes proceed", 10);

    const exitCode = await new Promise<number>((resolve) => child.on("close", resolve));
    assert.equal(exitCode, 0);
    assert.equal(stdout.trim(), "yes proceed");

    assert.equal(relay.get(scriptId), undefined);
  } finally {
    p.kill();
    child.kill();
  }
});

test("ask exits 1 when the target agent exits while waiting", async () => {
  const { relay, root, alive } = tmpRelay();
  const agentProc = spawn("sleep", ["30"]);
  alive.add(agentProc.pid!);
  const a = relay.register({ kind: "claude", pid: agentProc.pid!, cwd: "/a", id: "aaaa1" });

  const child = spawn(process.execPath, [cliPath, "ask", "--to", "aaaa1", "question"], {
    env: { ...process.env, AGENT_RELAY_ROOT: root, AGENT_RELAY_POLL_MS: "50" },
    stdio: ["pipe", "pipe", "pipe"],
  });
  alive.add(child.pid!);

  let stderr = "";
  child.stderr.on("data", (d) => (stderr += d.toString()));

  let scriptId = "";
  for (let i = 0; i < 50; i++) {
    const s = relay.sessions().find((rec) => rec.kind === "script" && rec.agent?.awaiting === a.id);
    if (s) {
      scriptId = s.id;
      break;
    }
    await new Promise((r) => setTimeout(r, 20));
  }
  assert.ok(scriptId, "script record should be registered");

  agentProc.kill("SIGKILL");
  alive.delete(agentProc.pid!);

  const exitCode = await new Promise<number>((resolve) => child.on("close", resolve));
  assert.equal(exitCode, 1);
  assert.match(stderr, /agent aaaa1 exited/);
  assert.equal(relay.get(scriptId), undefined);
});

test("run: exit code passthrough, signal code, tail content", () => {
  const { relay, root, alive } = tmpRelay();
  const p = spawn("sleep", ["30"]);
  alive.add(p.pid!);
  const a = relay.register({ kind: "claude", pid: p.pid!, cwd: "/a", id: "aaaa1" });
  const inboxA = path.join(root, "inbox", a.id);

  try {
    const resCode = runCliCmd(
      ["run", "--to", "aaaa1", "--", process.execPath, "-e", "console.log('out line'); process.exit(3);"],
      { AGENT_RELAY_ROOT: root },
    );
    assert.equal(resCode.code, 3);
    let files = fs.readdirSync(inboxA).filter((f) => f.endsWith(".json"));
    assert.equal(files.length, 1);
    let msg = JSON.parse(fs.readFileSync(path.join(inboxA, files[0]), "utf8")) as Message;
    assert.match(msg.text, /exited 3 after/);
    assert.match(msg.text, /out line/);
    fs.rmSync(path.join(inboxA, files[0]));

    const resSig = runCliCmd(
      ["run", "--to", "aaaa1", "--", process.execPath, "-e", "process.kill(process.pid, 'SIGTERM');"],
      { AGENT_RELAY_ROOT: root },
    );
    assert.equal(resSig.code, 128 + 15);
    files = fs.readdirSync(inboxA).filter((f) => f.endsWith(".json"));
    assert.equal(files.length, 1);
    msg = JSON.parse(fs.readFileSync(path.join(inboxA, files[0]), "utf8")) as Message;
    assert.match(msg.text, /exited SIGTERM after/);
    fs.rmSync(path.join(inboxA, files[0]));

    const scriptCode = "for(let i=1;i<=25;i++) console.log('line ' + i);";
    const resTail = runCliCmd(
      ["run", "--to", "aaaa1", "--tail", "5", "--", process.execPath, "-e", scriptCode],
      { AGENT_RELAY_ROOT: root },
    );
    assert.equal(resTail.code, 0);
    files = fs.readdirSync(inboxA).filter((f) => f.endsWith(".json"));
    assert.equal(files.length, 1);
    msg = JSON.parse(fs.readFileSync(path.join(inboxA, files[0]), "utf8")) as Message;
    const lines = msg.text.split("\n");
    assert.equal(lines.length, 6);
    assert.equal(lines[1], "line 21");
    assert.equal(lines[5], "line 25");
  } finally {
    p.kill();
  }
});

test("run sends notification when the wrapper process receives a signal", async () => {
  const { relay, root, alive } = tmpRelay();
  const p = spawn("sleep", ["30"]);
  alive.add(p.pid!);
  const a = relay.register({ kind: "claude", pid: p.pid!, cwd: "/a", id: "aaaa1" });
  const inboxA = path.join(root, "inbox", a.id);

  const wrapper = spawn(
    process.execPath,
    [cliPath, "run", "--to", "aaaa1", "--name", "sigtest", "--", process.execPath, "-e", "console.log('ready'); setInterval(()=>{}, 1000);"],
    {
      env: { ...process.env, AGENT_RELAY_ROOT: root },
      stdio: ["pipe", "pipe", "pipe"],
    },
  );
  alive.add(wrapper.pid!);

  await new Promise<void>((resolve) => {
    wrapper.stdout.on("data", (d) => {
      if (d.toString().includes("ready")) resolve();
    });
  });

  wrapper.kill("SIGTERM");
  const exitCode = await new Promise<number>((resolve) => wrapper.on("close", resolve));
  assert.equal(exitCode, 128 + 15);

  const files = fs.readdirSync(inboxA).filter((f) => f.endsWith(".json"));
  assert.equal(files.length, 1);
  const msg = JSON.parse(fs.readFileSync(path.join(inboxA, files[0]), "utf8")) as Message;
  assert.match(msg.text, /exited SIGTERM after/);
  assert.equal(msg.from.name, "sigtest");

  p.kill();
});

test("link and relay_peers ignore script records; nested send under an ask resolves the agent", () => {
  const { relay } = tmpRelay();
  const a = relay.register({ kind: "claude", pid: 100, cwd: "/a", id: "aaaa1", name: "planner" });
  const s = relay.register({ kind: "script", pid: 200, cwd: "/s", id: "ssss1" });

  assert.throws(() => relay.link(a.id, "ssss"), /no session matches/);
  assert.throws(() => relay.link(a.id, s.id), /no session matches/);

  const peers = peersText(relay, a.id);
  assert.ok(!peers.includes("ssss1"));
  assert.ok(peers.includes("not linked to any session"));

  const targetFromNested = resolveTarget(relay, undefined, {}, 300, () => [300, 200, 100]);
  assert.equal(targetFromNested, a.id);
});

test("RingBuffer edge cases and formatDuration helper", () => {
  const r0 = new RingBuffer(0);
  r0.feed("abc\n");
  assert.equal(r0.finish(), "");

  const r2 = new RingBuffer(2);
  r2.feed("one\ntwo\nthree\n");
  assert.equal(r2.finish(), "two\nthree");

  assert.equal(formatDuration(0), "0s");
  assert.equal(formatDuration(2100), "2s");
  assert.equal(formatDuration(65_000), "1m 5s");
  assert.equal(formatDuration(3600_000), "1h");
});
