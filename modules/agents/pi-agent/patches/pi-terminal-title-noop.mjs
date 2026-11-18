// Blanks pi's updateTerminalTitle so the terminal-title extension is the only title writer.

import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const METHOD = "updateTerminalTitle";
const DEFINITION = new RegExp(`\\b${METHOD}\\s*\\(\\s*\\)\\s*\\{`, "g");
const MARKER = `// pi's terminal title writer is disabled here; see pi-agent/icedos.nix`;

function files(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return files(path);
    return path.endsWith(".js") ? [path] : [];
  });
}

// Walks braces from the body's opening brace to its matching close.
function bodyEnd(text, open) {
  let depth = 0;
  for (let i = open; i < text.length; i++) {
    if (text[i] === "{") depth++;
    else if (text[i] === "}" && --depth === 0) return i;
  }
  return -1;
}

let patched = 0;
let found = 0;
for (const file of files(process.argv[2])) {
  const text = readFileSync(file, "utf8");
  if (text.includes(MARKER)) {
    found++;
    continue;
  }
  let out = text;
  for (const match of [...text.matchAll(DEFINITION)].reverse()) {
    const open = match.index + match[0].length - 1;
    const close = bodyEnd(text, open);
    if (close === -1) continue;
    out = `${out.slice(0, open + 1)}${out.slice(close)}`;
    found++;
    patched++;
  }
  if (out !== text) writeFileSync(file, `${out}\n${MARKER}\n`);
}

if (found === 0) {
  console.error(`pi-terminal-title-noop: no ${METHOD} definition under ${process.argv[2]}`);
  process.exit(1);
}
console.log(`pi-terminal-title-noop: blanked ${patched} ${METHOD} body(s)`);
