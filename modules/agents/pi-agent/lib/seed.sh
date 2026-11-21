#!/usr/bin/env bash
# Usage: seed.sh DATA_DIR SETTINGS_SEED MODELS_SEED [NPM_CMD] (needs jq and coreutils on PATH).
set -u

dir=$1
settings_seed=$2
models_seed=$3
npm_cmd=${4:-npm}

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

packages_ledger="$dir/.icedos-packages.json"
paths_ledger="$dir/.icedos-settings-paths.json"

mkdir -p "$dir"
prev_packages=$(jq -c 'if type == "array" then . else [] end' "$packages_ledger" 2>/dev/null || echo '[]')

# Managed leaf paths exclude .packages (handled separately).
leaf_filter='
  def is_leaf: (type != "object" and type != "array") or ((type == "object" or type == "array") and length == 0);
  del(.packages) | [paths(is_leaf)]'

new_paths=$(jq -c "$leaf_filter" "$settings_seed")

if [ -f "$paths_ledger" ]; then
  removed_paths=$(jq -n --slurpfile prev "$paths_ledger" --argjson cur "$new_paths" '($prev[0] // []) - $cur')
else
  removed_paths='[]'
fi

# Drops removed managed paths, only prunes parents of removed paths that are now {}, then merges seed.
settings_filter='
  ($removed_paths | map(
    . as $p |
    range(1; $p | length) as $len |
    $p[0:$len]
  ) | unique | sort_by(-length)) as $prefixes
  | ($a[0] | delpaths($removed_paths)) as $cleaned
  | (reduce $prefixes[] as $p ($cleaned; if getpath($p) == {} then delpaths([$p]) else . end)) as $base
  | ($base * ($b[0] | del(.packages))) as $m
  | ((($a[0].packages // []) - $prev) + ($b[0].packages // [])
     | reduce .[] as $x ([]; if any(.[]; . == $x) then . else . + [$x] end)) as $pk
  | if $pk == [] and ($a[0] | has("packages") | not) then $m else $m + {packages: $pk} end'

if merge "$dir/settings.json" "$settings_seed" "$settings_filter" \
  --argjson prev "$prev_packages" \
  --argjson removed_paths "$removed_paths"; then
  jq -c '.packages // []' "$settings_seed" >"$packages_ledger.tmp" && commit "$packages_ledger.tmp" "$packages_ledger"
  echo "$new_paths" >"$paths_ledger.tmp" && commit "$paths_ledger.tmp" "$paths_ledger"
  rm -f "$packages_ledger.tmp" "$paths_ledger.tmp"
fi

# NPM prune: uninstall npm packages removed from config. Local store paths need no uninstall.
if [ -f "$packages_ledger" ] && [ -d "$dir/npm" ]; then
  cur_packages=$(jq -c '.packages // []' "$settings_seed")
  removed_pkgs=$(jq -n --argjson prev "$prev_packages" --argjson cur "$cur_packages" '
    def pkg_name:
      if type == "string" and (startswith("/") or startswith("./") | not) then
        sub("^npm:"; "") | capture("^(?<name>@[^@]+/[^@]+|[^@]+)") | .name
      else empty end;
    (($prev | map(pkg_name)) - ($cur | map(pkg_name)))
  ')
  for pkg_name in $(echo "$removed_pkgs" | jq -r '.[]'); do
    if [ -n "$pkg_name" ]; then
      "$npm_cmd" uninstall "$pkg_name" --prefix "$dir/npm" --offline --legacy-peer-deps --no-audit --no-fund >/dev/null 2>&1 || true
    fi
  done
fi

merge "$dir/models.json" "$models_seed" '$a[0] | .providers = ((.providers // {}) + $b[0].providers)'
exit 0
