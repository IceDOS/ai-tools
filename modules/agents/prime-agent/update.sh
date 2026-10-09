#!/usr/bin/env nix-shell
#! nix-shell -i bash -p curl git jq nix nix-prefetch-git npm-lockfile-fix prefetch-npm-deps

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
REPO="PrimeIntellect-ai/prime-agent"
CATALOG_REPO="PrimeIntellect-ai/prime-agent-catalog"

main() {
  banner "prime-agent updater"

  info "Finding latest $REPO release..."
  local tag
  tag=$(gh_latest_release "$REPO")
  [ -n "$tag" ] || error "no release found"
  info "  Latest: $tag"

  local current
  current=$(read_pin "$PIN" .rev)
  if [ "$tag" = "$current" ]; then
    info "  Already up to date ($tag)"
    return
  fi
  info "  Current: ${current:-none}"

  # Upstream tags `vX.Y.Z`; the derivation's `version` carries no prefix.
  local version="${tag#v}"

  # global on purpose: the EXIT trap fires after main() returns,
  # where a `local` would already be out of scope (set -u)
  tmpdir=$(mktemp -d)
  trap 'rm -rf "$tmpdir"' EXIT

  git clone --depth 1 --branch "$tag" "https://github.com/$REPO.git" "$tmpdir/repo" 2>/dev/null
  # package.nix runs npm-lockfile-fix in fetchFromGitHub's postFetch, so the source
  # hash must cover the fixed tree: raw nix-prefetch-git hashes differ after the fix.
  npm-lockfile-fix "$tmpdir/repo/package-lock.json"
  rm -rf "$tmpdir/repo/.git"

  info "  Computing source hash (tree after npm-lockfile-fix)..."
  local hash
  hash=$(nix hash path "$tmpdir/repo" || echo "")
  require_nonempty prime-agent "$version" "$tag" "$hash"
  info "  Hash: $hash"

  # npmDepsHash from the same fixed lockfile: buildNpmPackage defaults to
  # NPM_FETCHER_VERSION=2 while prefetch-npm-deps defaults to v1, so set it.
  info "  Computing npmDepsHash..."
  local npmDepsHash
  npmDepsHash=$(NPM_FETCHER_VERSION=2 prefetch-npm-deps "$tmpdir/repo/package-lock.json" 2>/dev/null || echo "")
  require_nonempty prime-agent-npm "$version" "$tag" "$npmDepsHash"
  info "  npmDepsHash: $npmDepsHash"

  info "  Pinning prime-agent-catalog HEAD..."
  local catalogRev catalogHash
  catalogRev=$(git ls-remote "https://github.com/$CATALOG_REPO.git" HEAD | cut -f1)
  catalogHash=$(nix-prefetch-git --quiet "https://github.com/$CATALOG_REPO.git" "$catalogRev" | jq -r .hash)
  require_nonempty prime-agent-catalog "$version" "$catalogRev" "$catalogHash"

  jq -n --arg version "$version" --arg rev "$tag" --arg hash "$hash" --arg npmDepsHash "$npmDepsHash" \
    --arg catalogRev "$catalogRev" --arg catalogHash "$catalogHash" \
    '{version: $version, rev: $rev, hash: $hash, npmDepsHash: $npmDepsHash, catalogRev: $catalogRev, catalogHash: $catalogHash}' | write_pin "$PIN"

  # Update pi-clm extension
  info "Updating pi-clm extension..."
  clm_pkg_json=$(curl -s "https://registry.npmjs.org/@lolipopshock/pi-clm/latest")
  clm_version=$(echo "$clm_pkg_json" | jq -r ".version")
  clm_tar="https://registry.npmjs.org/@lolipopshock/pi-clm/-/pi-clm-${clm_version}.tgz"
  clm_hash=$(nix-prefetch-url --unpack "$clm_tar" 2>/dev/null | tail -1 || nix-prefetch-url "$clm_tar" 2>/dev/null)
  if [ -f "$SCRIPT_DIR/extensions/pi-clm.nix" ]; then
    sed -i "s/version = \"[^\"]*\";/version = \"$clm_version\";/" "$SCRIPT_DIR/extensions/pi-clm.nix" 2>/dev/null || true
    sed -i "s|url = \"[^\"]*pi-clm[^\"]*\";|url = \"$clm_tar\";|" "$SCRIPT_DIR/extensions/pi-clm.nix" 2>/dev/null || true
    sed -i "s/sha256 = \"[^\"]*\";/sha256 = \"$clm_hash\";/" "$SCRIPT_DIR/extensions/pi-clm.nix" 2>/dev/null || true
  fi
  info "  pi-clm: $clm_version"

  info "  Updated: $version"
}

main "$@"

echo ""
info "Done. Review changes with: git diff $SCRIPT_DIR"
