import assert from "node:assert";
import * as fs from "node:fs";
import * as path from "node:path";
import { rowText } from "../extensions/vpn-row.ts";

function withState(state: unknown, run: () => void) {
  const dir = fs.mkdtempSync(path.join("/tmp", "vpn-row-test-"));
  const prevRuntime = process.env.XDG_RUNTIME_DIR;
  try {
    process.env.XDG_RUNTIME_DIR = dir;
    fs.writeFileSync(path.join(dir, "vpn-rotate.json"), typeof state === "string" ? state : JSON.stringify(state));
    run();
  } finally {
    if (prevRuntime === undefined) delete process.env.XDG_RUNTIME_DIR;
    else process.env.XDG_RUNTIME_DIR = prevRuntime;
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

function testNoState() {
  const dir = fs.mkdtempSync(path.join("/tmp", "vpn-row-test-"));
  const prevRuntime = process.env.XDG_RUNTIME_DIR;
  try {
    process.env.XDG_RUNTIME_DIR = dir;
    assert.strictEqual(rowText(), undefined, "no state file means no row");
    fs.writeFileSync(path.join(dir, "vpn-rotate.json"), "{ not json");
    assert.strictEqual(rowText(), undefined, "an unreadable state file means no row");
  } finally {
    if (prevRuntime === undefined) delete process.env.XDG_RUNTIME_DIR;
    else process.env.XDG_RUNTIME_DIR = prevRuntime;
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

function testDirect() {
  withState({ at: 1, mode: "direct", server: null, egressIp: "1.2.3.4" }, () => {
    assert.strictEqual(rowText(), "vpn direct");
  });
  // An older state file has no mode field at all.
  withState({ at: 1, server: null }, () => {
    assert.strictEqual(rowText(), "vpn direct");
  });
}

function testTunnel() {
  withState({ at: 1, mode: "tunnel", proxy: "http://127.0.0.1:25345", server: "nl-12", country: "NL", egressIp: "1.2.3.4" }, () => {
    assert.strictEqual(rowText(), "vpn tunnel nl-12 NL 1.2.3.4");
  });
  // Rotation has not reported a server yet.
  withState({ at: 1, mode: "tunnel", proxy: "http://127.0.0.1:25345" }, () => {
    assert.strictEqual(rowText(), "vpn tunnel");
  });
}

function testTransientStates() {
  withState({ at: 1, mode: "direct", rotating: true }, () => {
    assert.strictEqual(rowText(), "vpn rotating…");
  });
  withState({ at: 1, mode: "direct", rotating: true, model: "m-free" }, () => {
    assert.strictEqual(rowText(), "vpn rotating for m-free…");
  });
  withState({ at: Date.now(), mode: "tunnel", server: "nl-12", allRateLimited: true, refreshIn: 42, model: "m-free" }, () => {
    assert.strictEqual(rowText(), "vpn all rate-limited for m-free (refresh in 42s)");
  });
  withState({ at: Date.now() - 60_000, mode: "tunnel", server: "nl-12", allRateLimited: true, refreshIn: 42 }, () => {
    assert.strictEqual(rowText(), "vpn tunnel nl-12", "an expired wait shows the live mode");
  });
}

function testFlaggedHostIp() {
  const now = Math.floor(Date.now() / 1000);
  withState({ at: 1, mode: "direct", hostIpFlaggedUntil: now + 3600 }, () => {
    assert.strictEqual(rowText(), "vpn direct (host ip flagged)");
  });
  withState({ at: 1, mode: "direct", hostIpFlaggedUntil: now - 60 }, () => {
    assert.strictEqual(rowText(), "vpn direct", "an expired flag drops the suffix");
  });
  withState({ at: 1, mode: "direct", hostIpFlaggedUntil: null }, () => {
    assert.strictEqual(rowText(), "vpn direct");
  });
  // A live tunnel wins over the flag, and a rate-limited tunnel reports the wait instead.
  withState(
    { at: 1, mode: "tunnel", hostIpFlaggedUntil: now + 3600, server: "nl-12", country: "NL", egressIp: "1.2.3.4" },
    () => {
      assert.strictEqual(rowText(), "vpn tunnel nl-12 NL 1.2.3.4");
    },
  );
  withState({ at: Date.now(), mode: "direct", hostIpFlaggedUntil: now + 3600, allRateLimited: true, refreshIn: 42 }, () => {
    assert.strictEqual(rowText(), "vpn all rate-limited (refresh in 42s)");
  });
}

function runAll() {
  testNoState();
  testDirect();
  testTunnel();
  testTransientStates();
  testFlaggedHostIp();
  console.log("All vpn-row tests passed successfully.");
}

runAll();
