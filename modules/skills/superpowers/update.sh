#!/usr/bin/env nix-shell
#! nix-shell -i bash -p curl git jq nix
# shellcheck shell=bash

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
REPO="obra/superpowers"

main() {
  banner "superpowers updater"

  info "Finding latest $REPO release..."
  local tag
  tag=$(gh_latest_release "$REPO")
  [ -n "$tag" ] || error "no release found"
  info "  Latest: $tag"

  local version="${tag#v}"
  local rev
  rev=$(git ls-remote "https://github.com/$REPO.git" "refs/tags/$tag^{}" 2>/dev/null | awk '{print $1}' | head -1)
  [ -n "$rev" ] || rev=$(git ls-remote "https://github.com/$REPO.git" "refs/tags/$tag" 2>/dev/null | awk '{print $1}' | head -1)
  [ -n "$rev" ] || error "could not resolve tag $tag commit"
  info "  Commit: $rev"

  local current_version current_rev
  current_version=$(read_pin "$PIN" .version)
  current_rev=$(read_pin "$PIN" .rev)
  if [ "$version" = "$current_version" ] && [ "$rev" = "$current_rev" ]; then
    info "  Already up to date"
    return
  fi
  info "  Current: ${current_version:-none}"

  info "  Computing hash..."
  local hash
  hash=$(prefetch_github "${REPO%%/*}" "${REPO##*/}" "$rev" || echo "")
  require_nonempty superpowers "$version" "$rev" "$hash"
  info "  Hash: $hash"

  jq -n --arg version "$version" --arg rev "$rev" --arg hash "$hash"     '{version: $version, rev: $rev, hash: $hash}' |
    write_pin "$PIN"

  info "  Updated: $version"
}

main "$@"

echo ""
info "Done. Review changes with: git diff $SCRIPT_DIR"
