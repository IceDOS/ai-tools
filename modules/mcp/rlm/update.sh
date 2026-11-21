#!/usr/bin/env nix-shell
#! nix-shell -i bash -p curl git jq nix

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

PIN="$SCRIPT_DIR/rlm-kernel.json"
PACKAGE="@florianguyonnet/pi-rlm"

main() {
  banner "pi-rlm kernel updater"

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

  # package.nix uses fetchurl on the tarball, so hash the file, not the unpacked tree.
  info "  Computing hash..."
  local hash
  hash=$(prefetch_file "https://registry.npmjs.org/$PACKAGE/-/pi-rlm-$version.tgz" || echo "")
  require_nonempty pi-rlm "$version" "$hash"
  info "  Hash: $hash"

  jq -n --arg version "$version" --arg hash "$hash" \
    '{version: $version, hash: $hash}' | write_pin "$PIN"

  info "  Updated: $version"
}

main "$@"

echo ""
info "Done. Review changes with: git diff $SCRIPT_DIR"
