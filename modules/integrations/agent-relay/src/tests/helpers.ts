import { Relay } from "../core.ts";

const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");
const os = process.getBuiltinModule("node:os") as typeof import("node:os");
const path = process.getBuiltinModule("node:path") as typeof import("node:path");

export interface TestCtx {
  after: (fn: () => void | Promise<void>) => void;
}

export function tmpRelay(alive: Set<number> = new Set([100, 200, 300, 400]), now?: () => Date, t?: TestCtx) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "relay-"));
  const stateDir = path.join(root, "state");
  t?.after(() => {
    try {
      fs.rmSync(root, { recursive: true, force: true });
    } catch {}
  });
  return { root, stateDir, alive, relay: new Relay({ root, stateDir, isAlive: (p) => alive.has(p), now, ancestorsOf: (p) => [p] }) };
}
