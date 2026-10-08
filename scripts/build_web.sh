#!/usr/bin/env bash
# Build the web editor and copy it into the Python package so `nanocore serve` finds it.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root/web"

if [ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]; then
  # shellcheck disable=SC1091
  . "${NVM_DIR:-$HOME/.nvm}/nvm.sh"
  nvm use >/dev/null
fi
node -e 'const [maj, min] = process.versions.node.split(".").map(Number);
  if (maj < 22 || (maj === 22 && min < 12)) { console.error("Node >= 22.12 is required, found " + process.version); process.exit(1); }'

npm ci --no-audit --no-fund
npm test
npm run build

rm -rf "$root/src/nanocore_controller/web_dist"
cp -r dist "$root/src/nanocore_controller/web_dist"
echo "web editor copied to src/nanocore_controller/web_dist"
