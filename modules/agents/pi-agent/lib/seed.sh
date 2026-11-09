#!/usr/bin/env bash
# Usage: seed.sh DATA_DIR SETTINGS_SEED MODELS_SEED (needs jq and coreutils on PATH).
set -u

dir=$1
settings_seed=$2
models_seed=$3

is_object() { [ -s "$1" ] && jq -e 'type == "object"' "$1" >/dev/null 2>&1; }

# fsync before mv: under delayed allocation a crash can otherwise leave a 0-byte file.
commit() { chmod 0644 "$1" && sync -d "$1" && mv "$1" "$2"; }

merge() {
  local target=$1 seed=$2 filter=$3
  shift 3
  if [ -e "$target" ] && ! is_object "$target"; then
    mv "$target" "$target.bad-$(date -u +%Y%m%dT%H%M%SZ)"
    echo "pi-agent: $target was not a JSON object, moved aside" >&2
  fi
  if [ -e "$target" ]; then
    local ok=1
    if jq -n --slurpfile a "$target" --slurpfile b "$seed" "$@" "$filter" >"$target.tmp"; then
      commit "$target.tmp" "$target" && ok=0 || echo "pi-agent: failed to write $target" >&2
    else
      echo "pi-agent: failed to merge $target, left unchanged" >&2
    fi
    rm -f "$target.tmp"
    return $ok
  fi
  install -m 0644 "$seed" "$target"
}

# Package entries Nix added last time; pi's own `pi install` entries are never in it.
ledger="$dir/.icedos-packages.json"

# Managed keys win; packages = (current minus last Nix list) plus the Nix list, deduplicated.
settings_filter='
  ($a[0] * ($b[0] | del(.packages))) as $m
  | ((($a[0].packages // []) - $prev) + ($b[0].packages // [])
     | reduce .[] as $x ([]; if any(.[]; . == $x) then . else . + [$x] end)) as $pk
  | if $pk == [] and ($a[0] | has("packages") | not) then $m else $m + {packages: $pk} end'

mkdir -p "$dir"
prev=$(jq -c 'if type == "array" then . else [] end' "$ledger" 2>/dev/null || echo '[]')
if merge "$dir/settings.json" "$settings_seed" "$settings_filter" --argjson prev "$prev"; then
  jq -c '.packages // []' "$settings_seed" >"$ledger.tmp" && commit "$ledger.tmp" "$ledger"
  rm -f "$ledger.tmp"
fi
merge "$dir/models.json" "$models_seed" '$a[0] | .providers = ((.providers // {}) + $b[0].providers)'
exit 0
