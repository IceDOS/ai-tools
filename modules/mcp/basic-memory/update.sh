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
REPO="basicmachines-co/basic-memory"

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
  banner "basic-memory updater"

  info "Finding latest $REPO release..."
  local tag
  tag=$(gh_latest_release "$REPO")
  [ -n "$tag" ] || error "no release found"
  info "  Latest: $tag"

  local current
  current=$(read_pin "$PIN" .rev)
  if [ "$tag" = "$current" ]; then
    info "  Already up to date"
    return
  fi
  info "  Current: ${current:-none}"

  local version="${tag#v}"
  info "  Computing hash..."
  local hash
  hash=$(prefetch_github "${REPO%%/*}" "${REPO##*/}" "$tag" || echo "")
  require_nonempty basic-memory "$version" "$tag" "$hash"
  info "  Hash: $hash"

  # Upstream pins FastMCP exactly (fastmcp==X, fastmcp-slim==X as a uv constraint).
  info "  Resolving the FastMCP pin..."
  local pyproject fastmcp slim
  pyproject=$(curl -sf "https://raw.githubusercontent.com/$REPO/$tag/pyproject.toml")
  fastmcp=$(grep -oP '"fastmcp==\K[^"]+' <<<"$pyproject" | head -1 || true)
  slim=$(grep -oP '"fastmcp-slim==\K[^"]+' <<<"$pyproject" | head -1 || true)
  [ -n "$fastmcp" ] || error "pyproject no longer pins fastmcp exactly; update package.nix by hand"
  slim="${slim:-$fastmcp}"
  info "  fastmcp: $fastmcp, fastmcp-slim: $slim"

  local fastmcpWheel slimWheel
  fastmcpWheel=$(pypi_wheel fastmcp "$fastmcp")
  slimWheel=$(pypi_wheel fastmcp-slim "$slim")

  jq -n --arg version "$version" --arg rev "$tag" --arg hash "$hash" \
    --argjson fastmcp "$fastmcpWheel" --argjson slim "$slimWheel" \
    '{version: $version, rev: $rev, hash: $hash, "fastmcp-slim": $slim, fastmcp: $fastmcp}' |
    write_pin "$PIN"

  info "  Updated: $version"
}

main "$@"

echo ""
info "Done. Review changes with: git diff $SCRIPT_DIR"
