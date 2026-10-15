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

PIN="$SCRIPT_DIR/python-sdk.json"
# Dependency order, matching python.nix.
PACKAGES=(httpcore2 httpx2 mcp-types mcp)

# pypi_wheel NAME VERSION — {version, url, hash} of the pure-Python wheel.
pypi_wheel() {
  local name="$1" version="$2" info url sha hash
  info=$(curl -sf "https://pypi.org/pypi/$name/$version/json") || error "$name $version not on PyPI"
  url=$(jq -r '[.urls[] | select(.filename | endswith("py3-none-any.whl"))] | first | .url // ""' <<<"$info")
  sha=$(jq -r '[.urls[] | select(.filename | endswith("py3-none-any.whl"))] | first | .digests.sha256 // ""' <<<"$info")
  hash=$(to_sri "$sha" || echo "")
  require_nonempty "$name" "$version" "$url" "$hash"
  jq -n --arg version "$version" --arg url "$url" --arg hash "$hash" '{version: $version, url: $url, hash: $hash}'
}

main() {
  banner "MCP Python SDK updater"

  local pin="{}" name version current
  for name in "${PACKAGES[@]}"; do
    version=$(curl -sf "https://pypi.org/pypi/$name/json" | jq -r '.info.version // ""')
    [ -n "$version" ] || error "no PyPI release found for $name"
    current=$(read_pin "$PIN" ".\"$name\".version")
    if [ "$version" = "$current" ]; then
      info "  $name: $version (up to date)"
      pin=$(jq --arg n "$name" --slurpfile cur "$PIN" '.[$n] = $cur[0][$n]' <<<"$pin")
    else
      info "  $name: ${current:-none} -> $version"
      pin=$(jq --arg n "$name" --argjson w "$(pypi_wheel "$name" "$version")" '.[$n] = $w' <<<"$pin")
    fi
  done

  write_pin "$PIN" <<<"$pin"
}

main "$@"

echo ""
info "Done. Review changes with: git diff $SCRIPT_DIR"
