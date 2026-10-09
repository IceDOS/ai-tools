---
name: bash
description: Bash / POSIX shell guidance — scripts, quoting, control flow, and common pitfalls. Use when writing, editing, debugging, or understanding shell scripts and one-liners.
---

# Bash / Shell

Practical guidance for Bash and POSIX shell scripting on this system.

## Choosing the shell & invocation
- Scripts: start with `#!/usr/bin/env bash` and run with `bash script.sh`. On NixOS,
  `/bin/sh` is bash, so `sh script.sh` does not enforce POSIX.
- Prefer Bash by default; when portability to `dash`/`/bin/sh` is required, write POSIX
  and check with `dash -n` or `shellcheck -s sh` — never rely on `sh -n`, which only
  invokes bash's parser.
- Syntax-check without running: `bash -n script.sh`; trace execution with `bash -x script.sh`.
- Run one-off tooling ad hoc: `nix-shell -p <pkg> --run "<cmd>"` (see IceDOS `nix-shell` guidance).

## Quoting — the top source of bugs
- Always double-quote expansions: `"$var"`, `"$@"`, `"$(cmd)"`, `"${arr[@]}"`. Unquoted `$var` word-splits and globs.
- Use single quotes for literal strings with no expansion: `'$HOME'` stays literal.
- `$@` inside double quotes expands each arg as one word — the reliable way to forward args.
- Command substitution: `$( ... )` (prefer over legacy backticks). Quote always: unquoted
  `$(cmd)` word-splits and globs; quotes keep embedded whitespace but not trailing newlines.
- Brace expansion happens before other expansions and is suppressed inside quotes:
  `echo {1..5}` expands, `echo "{1..5}"` prints the literal text.

## Control flow & idioms
- `if cmd; then ...; fi` — run `cmd` directly; its exit status is the condition (no `[` needed).
- Use `[[ ]]` for Bash-only tests (supports `=~`, `&&`/`||`, no word-splitting); `[ ]` for POSIX.
- Loops: `for f in *.txt; do ...; done`; `while IFS= read -r line; do ...; done < file`.
- `case` for string dispatch: `case "$x" in a|b) ...;; *) ...;; esac`.
- `set -euo pipefail` for strict mode; beware `set -e` aborting on expected failures — guard with `|| true` or `if`.
- Functions: `name() { ...; }`. Always quote params and avoid globals leaking.
- `&&`/`||` for short chains; assign to a var and check instead of relying on `$?` unless you read it immediately.

## Variables & expansions
- Defaults: `"${var:-default}"` (use default, var unchanged) vs `"${var:=default}"` (also set var).
- Length: `${#var}`; substring: `${var:off:len}`; remove prefix/suffix: `${var#pat}`, `${var%pat}`; replace: `${var//old/new}`.
- `local` inside functions; `export` only what children need. Prefer lowercase names to avoid clashing with `$PATH`, `$HOME`, `$PWD` etc.
- Arrays (`arr=(a b c)`) and associative arrays (`declare -A m`) are Bash-only; expand all elements with `"${arr[@]}"` or `${!arr[@]}` for indices.

## Files, globs & redirections
- Globs don't expand to nothing when no match — a literal pattern is passed. Prefer
  `shopt -s nullglob` so unmatched globs vanish; `shopt -s failglob` errors instead.
- `2>&1` merges stderr into stdout; `&>`/`>... 2>&1` in Bash. `2>/dev/null` silences errors.
- Read a file line-by-line with `while IFS= read -r line; do ...; done < file` (keeps leading/trailing whitespace).
- `find ... -exec ... {} \;` vs `-exec ... +` for batching; never pipe find into a
  loop that mutates the same tree.
- Feed names losslessly: `... -print0 | while IFS= read -r -d '' f; do ...; done`
  — that loop runs in a subshell, so variables set inside are lost after the pipe.

## Gotchas
- `sh script.sh` does NOT make it POSIX-safe if it uses `[[ ]]`, arrays, `${var/...}` — run it under `bash`.
- `[` vs `[[` vs `(( ))` — `(( ))` is arithmetic; results as condition are C-style
  truthiness. Under `set -e`, `((i++))` with `i=0` returns exit 1 and kills the script
  — use `((i++)) || true` or `i=$((i+1))`.
- Command exit codes: `0` = success; check `$?` only immediately after the command (it is overwritten by the next command).
- Trailing-newline stripping in `$( ... )`; and CRLF line endings break shebangs and `read` — watch for `^M`.
- `sudo`/env stripped in some contexts (e.g. `sudo` resets `$HOME`/`$PATH`) — pass explicit flags (`sudo -E`, `sudo env PATH=...`).
- In scripts, use the full path or set `PATH`; a bare `cmd` may not be found in minimal/cron/sudo environments.
- Never parse `ls` output; use globs, `find`, or `compgen -G` when a filtered list is needed.

## Debugging & tooling
- Trace: `bash -x`; syntax-check without executing: `bash -n script.sh`. Add `set -x`
  around the suspect block (never `set -n` mid-script — nothing after it runs).
- No shell LSP ships in this set; `shellcheck` is the analyzer
  (via `nix-shell -p shellcheck --run "shellcheck script.sh"`);
  `nix-shell -p bash-language-server` adds an LSP if an editor wants one.
- `shfmt` (via `nix-shell -p shfmt --run "shfmt -i 2 -w script.sh"`) formats consistently.
- Prove POSIX compatibility with real checkers: `dash -n script` (syntax) and
  `shellcheck -s sh script` (bashism lint); `checkbashisms` for a second pass.
