#!/usr/bin/env nix-shell
#! nix-shell -i bash -p curl git jq nix nix-prefetch-git

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
REPO="chrisbray85/opencart-mcp"

main() {
  banner "opencart-mcp updater"

  # Upstream main runs ahead of its tags, so track the default branch.
  info "Finding $REPO HEAD..."
  local rev
  rev=$(git_head "https://github.com/$REPO.git")
  [ -n "$rev" ] || error "could not read HEAD"
  info "  Latest: $rev"

  local current
  current=$(read_pin "$PIN" .rev)
  if [ "$rev" = "$current" ]; then
    info "  Already up to date"
    return
  fi
  info "  Current: ${current:-none}"

  info "  Computing hash..."
  local report hash date base
  report=$(prefetch_git_json "https://github.com/$REPO.git" "$rev" || echo "{}")
  hash=$(jq -r '.hash // ""' <<<"$report")
  date=$(jq -r '.date // ""' <<<"$report" | cut -c1-10)
  base=$(curl -sf "https://raw.githubusercontent.com/$REPO/$rev/pyproject.toml" |
    sed -n 's/^version = "\(.*\)"/\1/p' | head -1)
  require_nonempty opencart-mcp "$rev" "$hash" "$date" "$base"
  local version="$base-unstable-$date"
  info "  Hash: $hash"

  jq -n --arg version "$version" --arg rev "$rev" --arg hash "$hash" \
    '{version: $version, rev: $rev, hash: $hash}' | write_pin "$PIN"

  info "  Updated: $version"
}

main "$@"

echo ""
info "Done. Review changes with: git diff $SCRIPT_DIR"
