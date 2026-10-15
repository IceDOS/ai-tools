#!/usr/bin/env nix-shell
#! nix-shell -i bash -p curl git jq nix nodejs_24 prefetch-npm-deps

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
CORE="${ICEDOS_CORE:-$REPO_ROOT/.icedos-core}"
[ -d "$CORE" ] || CORE="$REPO_ROOT/../core"
[ -f "$CORE/lib/update-lib.sh" ] || {
  echo "ERROR: core not found; set ICEDOS_CORE=/path/to/IceDOS/core" >&2
  exit 1
}
# shellcheck source=/dev/null
. "$CORE/lib/update-lib.sh"

PIN="$SCRIPT_DIR/source.json"
PACKAGE="rea-agents"

main() {
  banner "$PACKAGE updater"

  info "Finding latest $PACKAGE on npm..."
  local version
  version=$(curl -sf "https://registry.npmjs.org/$PACKAGE/latest" | jq -r '.version // ""')
  [ -n "$version" ] || error "no npm release found"
  info "  Latest: $version"

  local current
  current=$(read_pin "$PIN" .version)
  if [ "$version" = "$current" ]; then
    info "  Already up to date"
    return
  fi
  info "  Current: ${current:-none}"

  local url="https://registry.npmjs.org/$PACKAGE/-/$PACKAGE-$version.tgz"
  info "  Computing hash..."
  local hash
  hash=$(prefetch_unpacked "$url" || echo "")
  require_nonempty $PACKAGE "$version" "$hash"
  info "  Hash: $hash"

  # Same trim as postPatch in package.nix; keep the two jq filters identical.
  info "  Regenerating package-lock.json..."
  local work
  work=$(mktemp -d)
  trap 'rm -rf "$work"' EXIT
  curl -sfL "$url" | tar -xz -C "$work"
  jq 'del(.devDependencies, .optionalDependencies.frida, .scripts.prepare, .scripts.prepack)' \
    "$work/package/package.json" >"$work/package.json"
  (cd "$work" && npm install --package-lock-only --ignore-scripts --no-audit --no-fund >/dev/null)

  local npmDepsHash
  npmDepsHash=$(prefetch-npm-deps "$work/package-lock.json" 2>/dev/null | tail -1)
  require_nonempty $PACKAGE-npm-deps "$npmDepsHash"
  info "  npmDepsHash: $npmDepsHash"

  cp "$work/package-lock.json" "$SCRIPT_DIR/package-lock.json"
  jq -n --arg version "$version" --arg hash "$hash" --arg npmDepsHash "$npmDepsHash" \
    '{version: $version, hash: $hash, npmDepsHash: $npmDepsHash}' | write_pin "$PIN"

  info "  Updated: $version"
}

main "$@"

echo ""
info "Done. Review changes with: git diff $SCRIPT_DIR"
