#!/usr/bin/env bash
# Usage: prune-npm.sh CONFIG_DIR CACHE_DIR MANIFEST_SEED
set -u

config_dir=$1
cache_dir=$2
manifest_seed=$3

ledger="$config_dir/.icedos-npm-plugins.json"
npm_cache="$cache_dir/npm"

mkdir -p "$config_dir"

# Derives the cache key matching OpenCode packages/util/src/npm.ts lines 82-86, 95.
spec_to_key() {
  local s=$1
  if [[ "$s" =~ ^(@[^@/]+/[^@]+)(@.+)?$ ]]; then
    local name="${BASH_REMATCH[1]}"
    local ver="${BASH_REMATCH[2]:-@latest}"
    echo "${name}${ver}"
  elif [[ "$s" =~ ^([^@]+)(@.+)?$ ]]; then
    local name="${BASH_REMATCH[1]}"
    local ver="${BASH_REMATCH[2]:-@latest}"
    echo "${name}${ver}"
  else
    echo "$s"
  fi
}

commit() { chmod 0644 "$1" && sync -d "$1" && mv "$1" "$2"; }

# First run: delete nothing if no previous ledger.
if [ ! -f "$ledger" ]; then
  install -m 0644 "$manifest_seed" "$ledger"
  exit 0
fi

prev_specs=$(jq -c 'if type == "array" then . else [] end' "$ledger" 2>/dev/null || echo '[]')
cur_specs=$(jq -c 'if type == "array" then . else [] end' "$manifest_seed" 2>/dev/null || echo '[]')

removed_specs=$(jq -n --argjson prev "$prev_specs" --argjson cur "$cur_specs" '($prev - $cur)[]' 2>/dev/null || true)

for spec in $removed_specs; do
  spec=$(echo "$spec" | tr -d '"')
  [ -z "$spec" ] && continue

  key=$(spec_to_key "$spec")

  # Guard against empty keys, directory traversal (..), or absolute paths (/)
  if [ -z "$key" ] || [[ "$key" == *".."* ]] || [[ "$key" == /* ]]; then
    continue
  fi

  target_dir="$npm_cache/$key"
  if [ -d "$target_dir" ]; then
    rm -rf "$target_dir"
    case "$key" in
      @*/*)
        scope_dir="$npm_cache/${key%%/*}"
        if [ -d "$scope_dir" ]; then
          rmdir "$scope_dir" 2>/dev/null || true
        fi
        ;;
    esac
  fi
done

install -m 0644 "$manifest_seed" "$ledger"
exit 0
