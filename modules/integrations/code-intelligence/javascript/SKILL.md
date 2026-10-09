---
name: javascript
description: JavaScript / Node.js guidance — npm, ESM/CJS, async, and common runtime patterns. Use when writing, editing, or debugging JavaScript code or Node packages.
---

# JavaScript

Practical guidance for JavaScript and Node.js development.

## Tooling
- Package manager: `npm`. Init: `npm init -y`. Install: `npm install <pkg>`; dev: `npm install -D <pkg>`.
- Run scripts: `npm run <script>` (from package.json `"scripts"`).
- Lint: `eslint .`; format: `prettier --write .` (via nix-shell if not on PATH).
- Execute a file: `node file.js`. Debug: `node --inspect` + Chrome DevTools, or `node --inspect-brk` to pause at start.

## Module systems
- Modern code prefers ESM: add `"type": "module"` in package.json; use `import`/`export`, `.js` extensions in relative imports.
- CJS uses `require`/`module.exports`. Interop between the two can be tricky — keep one convention per package.
- `.mjs` forces ESM, `.cjs` forces CommonJS regardless of the package `"type"` field.

## Async
- Prefer `async`/`await` over callback nesting or `.then()` chains.
- Promise.all for independent parallel work; `Promise.allSettled` when you want results even if some reject.
- Always handle rejection — use try/catch or `.catch()` to avoid unhandled rejections.

## Idioms & gotchas
- `===`/`!==` for comparisons (avoid coercion with `==`).
- `const` by default; `let` only when reassignment is needed; avoid `var`.
- Array: `map`, `filter`, `reduce`, `find`, `some`, `every` — prefer these over manual loops.
- `null`/`undefined` are distinct; check with `=== null` / `=== undefined`, or optional chaining `?.` and nullish coalescing `??`.
- Spread `...` copies arrays/objects shallowly; nested objects still share references.
- `Date` and numeric parsing can be locale-dependent — be explicit about formats.
- Node global fetch (18+) returns `Response`; await `.json()`/`.text()`.

## Node runtime
- `process.env.X` gives env vars (strings). `process.exit(code)` for exit codes.
- Read files: `fs/promises` with `readFile`/`writeFile` (async) — avoid sync in hot paths.
- Use `path` module or `import.meta.dirname` (Node 20+) for reliable path resolution.
- Structurally throw `Error` with descriptive messages; attach `{ cause }` for context.
