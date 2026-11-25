// Restarts pi's auto-retry budget when an extension rewrote a message_end message, so a
// rate-limit hook that rotates the network is not capped by pi's 3 retries.

import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join, sep } from "node:path";

// The message_end branch of _emitExtensionEvent, in both the ESM core and the bundled CLI.
const CALL = /this\._replaceMessageInPlace\(event\.message,\s*normalized\)\s*;?/g;
const MARKER = `// patched: an extension rewrote this message, so the retry budget restarts; see pi-agent/icedos.nix`;

function files(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return files(path);
    return path.endsWith(".js") ? [path] : [];
  });
}

// The CLI runs the bundle, so a patch that only reached the ESM core would change nothing.
const isBundle = (file) => file.includes(`${sep}bundle${sep}`);

let patched = 0;
let bundle = false;
for (const file of files(process.argv[2])) {
  const text = readFileSync(file, "utf8");
  if (text.includes(MARKER)) {
    if (isBundle(file)) bundle = true;
    continue;
  }
  const matches = text.match(CALL) ?? [];
  if (matches.length === 0) continue;
  if (matches.length > 1) {
    console.error(`pi-retry-after-hook: ${file} has ${matches.length} message_end replacements`);
    process.exit(1);
  }
  const out = text.replace(CALL, (call) => `${call.trimEnd().replace(/;$/, "")}; this._retryAttempt = 0;`);
  writeFileSync(file, `${out}\n${MARKER}\n`);
  patched++;
  if (isBundle(file)) bundle = true;
}

if (!bundle) {
  console.error(`pi-retry-after-hook: no bundle copy patched under ${process.argv[2]}`);
  process.exit(1);
}
console.log(`pi-retry-after-hook: patched ${patched} file(s)`);
