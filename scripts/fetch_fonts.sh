#!/usr/bin/env bash
# Re-download self-hosted Latin woff2 fonts (Fontsource / OFL) into apps/static/fonts/.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/apps/static/fonts"
mkdir -p "$DEST"
BASE_PJ="https://cdn.jsdelivr.net/fontsource/fonts/plus-jakarta-sans@5.2.8"
BASE_SG="https://cdn.jsdelivr.net/fontsource/fonts/space-grotesk@5.2.8"
declare -A FILES=(
  ["plus-jakarta-sans-latin-400-normal.woff2"]="$BASE_PJ/latin-400-normal.woff2"
  ["plus-jakarta-sans-latin-500-normal.woff2"]="$BASE_PJ/latin-500-normal.woff2"
  ["plus-jakarta-sans-latin-600-normal.woff2"]="$BASE_PJ/latin-600-normal.woff2"
  ["space-grotesk-latin-400-normal.woff2"]="$BASE_SG/latin-400-normal.woff2"
  ["space-grotesk-latin-500-normal.woff2"]="$BASE_SG/latin-500-normal.woff2"
  ["space-grotesk-latin-600-normal.woff2"]="$BASE_SG/latin-600-normal.woff2"
  ["space-grotesk-latin-700-normal.woff2"]="$BASE_SG/latin-700-normal.woff2"
)
for name in "${!FILES[@]}"; do
  url="${FILES[$name]}"
  echo "Fetching $name…"
  curl -fsSL -o "$DEST/$name" "$url"
done
echo "Done → $DEST"
