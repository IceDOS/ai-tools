---
name: nix
description: Nix / NixOS / nixpkgs language guidance. Use when writing, editing, or evaluating Nix expressions, flakes, derivations, modules, or NixOS/home-manager configs.
---

# Nix

Practical guidance for working with the Nix language and ecosystem.

## Language essentials
- Nix is a pure, lazy, functional language. Values are immutable; use `let ... in` for bindings and `rec` for recursion.
- Function args: positional (`{ a, b }: ...`) or positional-with-defaults (`{ a ? 1, b }: ...`). Use `...` to allow extra attrs: `{ a, ... }:`.
- Lists: `[ 1 2 3 ]` (whitespace-separated, no commas). Attr sets: `{ a = 1; b = 2; }`. Access: `attr.a` or `attr.a.b`.
- String interpolation: `"hello ${name}"`. Multi-line strings: two single quotes `''...''`.
- Conditional: `if cond then a else b`. There is no `else if`; nest conditionals.
- `inherit` copies bindings: `inherit (pkgs) lib;` imports `pkgs.lib` into scope as `lib`.

## Flakes
- `flake.nix` has `inputs` and `outputs`. Standard output: `outputs = { self, nixpkgs }: { packages."<system>".default = ...; };`
- Pin inputs with `inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";`.
- Common system strings: `"x86_64-linux"`, `"aarch64-linux"`, `"aarch64-darwin"`.
- Iterate systems: `lib.genAttrs [ "x86_64-linux" "aarch64-linux" ] (system: ...)`.

## nixpkgs idioms
- `pkgs.callPackage ./foo.nix { }` calls a function with `lib`, `stdenv`, `fetchurl`, etc. auto-injected.
- `mkDerivation` for derivations. Common args: `pname`, `version`, `src`, `buildInputs`, `nativeBuildInputs`, `installPhase`, `meta`.
- Use `pkgs.${name}` attribute access; for dynamic names use `getAttr name pkgs`.
- `lib.mkOption`, `lib.mkIf`, `lib.optionalAttrs`, `lib.optionalString`, `lib.mkMerge` for NixOS module logic.
- `lib.types.str`, `lib.types.int`, `lib.types.bool`, `lib.types.listOf`, `lib.types.attrsOf`, `lib.types.submodule` for options.

## Gotchas
- Paths in strings interpolate as store paths only when typed literally (`./file`); quoted strings do not auto-copy.
- `builtins.fromJSON (builtins.readFile ./x.json)` to read JSON; `importTOML` for TOML.
- Prefer `lib.fileset` or `lib.cleanSource` to avoid including `.git`, build artifacts, and secrets in a source dir.
- `nix flake check` validates; `nix build .#pkg` builds; `nix develop` enters a dev shell from `devShells`.
- Lazy evaluation hides errors until forced; a config that *evaluates* may still fail at *build* time.

## Tooling (available via nix-shell)
- `nix-instantiate --eval`, `nix eval`, `nix repl`, `nix flake show`, `nix flake metadata`.
- Use `nix-shell -p <pkg> --run "<cmd>"` for ad-hoc tools without a dev shell.
