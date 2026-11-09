#!/usr/bin/env bash
# Typechecks every shipped pi extension, plus any .ts paths given as arguments, against pi's types.
set -euo pipefail

root=$(cd "$(dirname "$0")/../../../.." && pwd)
pi=$(nix-build --no-out-link '<nixpkgs>' -A pi-coding-agent)/lib/node_modules/pi-monorepo
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

mkdir -p "$work/node_modules/@earendil-works" "$work/src"
ln -s "$pi" "$work/node_modules/@earendil-works/pi-coding-agent"
for p in "$pi"/node_modules/*; do
  [ "$(basename "$p")" = "@earendil-works" ] || ln -s "$p" "$work/node_modules/"
done
for p in "$pi"/node_modules/@earendil-works/*; do
  ln -s "$p" "$work/node_modules/@earendil-works/"
done

cp -r "$root/modules/agents/pi-agent/extensions/." "$work/src/"
cp "$root/modules/integrations/alimit/pi-agent.ts" "$work/src/alimit.ts"
cp "$root/modules/models/llamacpp/extensions/llamacpp-lifecycle.ts" "$work/src/"
for extra in "$@"; do
  cp "$extra" "$work/src/"
done

cat >"$work/tsconfig.json" <<'EOF'
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "allowImportingTsExtensions": true,
    "noEmit": true,
    "skipLibCheck": true,
    "types": ["node"]
  },
  "include": ["src/**/*.ts"]
}
EOF

cd "$work"
nix-shell -p typescript --run 'tsc -p .'
echo "typecheck: 0 errors"
