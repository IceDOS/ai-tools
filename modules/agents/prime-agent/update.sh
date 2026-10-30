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

  info "  Computing source hash..."
  local hash
  hash=$(nix-prefetch-git --quiet "https://github.com/$REPO.git" "refs/tags/$tag" | jq -r .hash || echo "")
  require_nonempty prime-agent "$version" "$tag" "$hash"
  info "  Hash: $hash"

  # fetchCargoVendor is a FOD: build it against a fake hash and read the real one back.
  info "  Computing cargoHash..."
  local cargoHash
  cargoHash=$(nix build --no-link --impure --expr "
    let pkgs = import <nixpkgs> { }; in
    pkgs.rustPlatform.fetchCargoVendor {
      src = pkgs.fetchFromGitHub { owner = \"PrimeIntellect-ai\"; repo = \"prime-agent\"; tag = \"$tag\"; hash = \"$hash\"; };
      hash = pkgs.lib.fakeHash;
    }" 2>&1 | sed -n 's/.*got: *\(sha256-[A-Za-z0-9+/=]*\).*/\1/p' | head -1 || true)
  require_nonempty prime-agent-cargo "$version" "$tag" "$cargoHash"
  info "  cargoHash: $cargoHash"

  info "  Pinning prime-agent-catalog HEAD..."
  local catalogRev catalogHash
  catalogRev=$(git ls-remote "https://github.com/$CATALOG_REPO.git" HEAD | cut -f1)
  catalogHash=$(nix-prefetch-git --quiet "https://github.com/$CATALOG_REPO.git" "$catalogRev" | jq -r .hash)
  require_nonempty prime-agent-catalog "$version" "$catalogRev" "$catalogHash"
  info "  Upstream patches: re-run 'quilt push -a' in prime-agent-src at $tag and refresh any that fail."

  jq -n --arg version "$version" --arg rev "$tag" --arg hash "$hash" --arg cargoHash "$cargoHash" \
    --arg catalogRev "$catalogRev" --arg catalogHash "$catalogHash" \
    '{version: $version, rev: $rev, hash: $hash, cargoHash: $cargoHash, catalogRev: $catalogRev, catalogHash: $catalogHash}' | write_pin "$PIN"

  # Update pi-clm extension
  info "Updating pi-clm extension..."
  clm_pkg_json=$(curl -s "https://registry.npmjs.org/@lolipopshock/pi-clm/latest")
  clm_version=$(echo "$clm_pkg_json" | jq -r ".version")
  clm_tar="https://registry.npmjs.org/@lolipopshock/pi-clm/-/pi-clm-${clm_version}.tgz"
  clm_hash=$(nix-prefetch-url "$clm_tar" 2>/dev/null | tail -1)
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
