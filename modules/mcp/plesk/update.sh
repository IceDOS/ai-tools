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

PIN="$SCRIPT_DIR/source.json"
PACKAGE="plesk-mcp"

main() {
  banner "$PACKAGE updater"

  # Upstream publishes to PyPI without git tags, so follow the PyPI sdist.
  info "Finding latest $PACKAGE on PyPI..."
  local info version
  info=$(curl -sf "https://pypi.org/pypi/$PACKAGE/json") || error "$PACKAGE not on PyPI"
  version=$(jq -r '.info.version // ""' <<<"$info")
  [ -n "$version" ] || error "no PyPI release found"
  info "  Latest: $version"

  local current
  current=$(read_pin "$PIN" .version)
  if [ "$version" = "$current" ]; then
    info "  Already up to date"
    return
  fi
  info "  Current: ${current:-none}"

  local url sha hash
  url=$(jq -r '[.urls[] | select(.packagetype == "sdist")] | first | .url // ""' <<<"$info")
  sha=$(jq -r '[.urls[] | select(.packagetype == "sdist")] | first | .digests.sha256 // ""' <<<"$info")
  hash=$(to_sri "$sha" || echo "")
  require_nonempty $PACKAGE "$version" "$url" "$hash"
  info "  Hash: $hash"

  jq -n --arg version "$version" --arg url "$url" --arg hash "$hash" \
    '{version: $version, url: $url, hash: $hash}' | write_pin "$PIN"

  info "  Updated: $version"
}

main "$@"

echo ""
info "Done. Review changes with: git diff $SCRIPT_DIR"
