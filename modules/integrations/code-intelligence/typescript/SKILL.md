---
name: typescript
description: TypeScript language guidance, tsconfig, typing, and build tooling (tsc, tsgo, node). Use when writing, editing, or type-checking TypeScript code.
---

# TypeScript

Practical guidance for TypeScript development.

## Tooling
- Use `tsgo` or `tsc --noEmit` for type checking. In this repo, `tsgo -p tsconfig.build.json` builds workspace packages.
- Package manager is `npm` (or `uv` is not used for JS). Install: `npm install`; dev deps: `npm install -D <pkg>`.
- Linting: `eslint .` (via `nix-shell -p nodejs eslint --run "eslint ."` if not on PATH).
- Run a package: `npm run build` then `node dist/...`. Type-check before committing: `tsc --noEmit`.

## tsconfig essentials
- `"strict": true` catches the most bugs — keep it on.
- `"target": "es2022"`, `"module": "esnext"` / `"node16"` depending on runtime.
- `"moduleResolution": "bundler"` or `"node16"`; keep it consistent with `module`.
- `"noEmit": true` for type-check-only configs; build configs emit to `dist`.
- `"paths"` + `"baseUrl"` for path aliases (e.g. `@/*`).

## Typing conventions
- Prefer explicit return types on exported functions and interfaces.
- Use `type` for unions/primitives, `interface` for object shapes you may extend.
- Avoid `any`; use `unknown` + narrowing, or precise types. `as const` for literal types.
- `satisfies` (TS 4.9+) validates an expression against a type without widening it.
- Discriminated unions: `type Result = { ok: true; value: T } | { ok: false; error: string }` and narrow on `ok`.
- Readonly: `readonly T[]`, `as const` for immutable object literals.

## Async & errors
- Prefer `async`/`await` over `.then()` chains for readability.
- Handle errors with `try/catch`; in Node, check `err instanceof Error` before reading `.message`.
- Avoid unhandled promise rejections — always await or attach `.catch`.

## Gotchas
- `undefined` vs `null` semantics differ; pick one convention and be consistent.
- Array `sort()` sorts lexicographically by default — pass a comparator for numbers.
- `==`/`!=` coerce types; use strict `===`/`!==`.
- Watch `this` binding in callbacks; use arrow functions or bind.
- `process.env.X` is always `string | undefined`; validate before using.

## Node specifics
- ESM: add `"type": "module"` in package.json; import with explicit `.js` extensions in relative paths.
- CJS vs ESM interop can bite; check `"type"` field.
- Use `node --experimental-*` flags only when a stable option is unavailable.
