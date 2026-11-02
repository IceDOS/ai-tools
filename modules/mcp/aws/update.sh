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
PROXY_REPO="aws/mcp-proxy-for-aws"
TOOLKIT_REPO="aws/agent-toolkit-for-aws"

main() {
  banner "aws-mcp updater"

  info "Finding latest $PROXY_REPO release..."
  local tag proxy_report proxy_rev proxy_hash
  tag=$(gh_latest_release "$PROXY_REPO")
  [ -n "$tag" ] || error "no release found"
  proxy_report=$(prefetch_git_json "https://github.com/$PROXY_REPO.git" "refs/tags/$tag" || echo "{}")
  proxy_rev=$(jq -r '.rev // ""' <<<"$proxy_report")
  proxy_hash=$(jq -r '.hash // ""' <<<"$proxy_report")
  require_nonempty mcp-proxy-for-aws "$tag" "$proxy_rev" "$proxy_hash"
  info "  Latest: $tag"

  # The toolkit has no releases, so track the default branch.
  info "Finding $TOOLKIT_REPO HEAD..."
  local rev report hash date
  rev=$(git_head "https://github.com/$TOOLKIT_REPO.git")
  [ -n "$rev" ] || error "could not read HEAD"
  report=$(prefetch_git_json "https://github.com/$TOOLKIT_REPO.git" "$rev" || echo "{}")
  hash=$(jq -r '.hash // ""' <<<"$report")
  date=$(jq -r '.date // ""' <<<"$report" | cut -c1-10)
  require_nonempty agent-toolkit-for-aws "$rev" "$hash" "$date"
  info "  Latest: $rev"

  jq -n \
    --arg proxyVersion "${tag#v}" --arg proxyRev "$proxy_rev" --arg proxyHash "$proxy_hash" \
    --arg version "0-unstable-$date" --arg rev "$rev" --arg hash "$hash" \
    '{
      proxy: {version: $proxyVersion, rev: $proxyRev, hash: $proxyHash},
      toolkit: {version: $version, rev: $rev, hash: $hash}
    }' | write_pin "$PIN"

  info "  Updated: proxy ${tag#v}, toolkit $date"
}

main "$@"

echo ""
info "Done. Review changes with: git diff $SCRIPT_DIR"
