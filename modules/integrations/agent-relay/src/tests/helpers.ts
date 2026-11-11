import { Relay } from "../core.ts";

const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");
const os = process.getBuiltinModule("node:os") as typeof import("node:os");
const path = process.getBuiltinModule("node:path") as typeof import("node:path");

export function tmpRelay(alive: Set<number> = new Set([100, 200, 300, 400]), now?: () => Date) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "relay-"));
  return { root, alive, relay: new Relay({ root, isAlive: (p) => alive.has(p), now }) };
}
