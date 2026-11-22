#!/bin/bash
set -euo pipefail

package_dir=$(cd "$(dirname "$0")/.." && pwd)
binary=${1:-"$package_dir/.build/release/Tokenwatch"}
app=${2:-"$package_dir/.build/Tokenwatch.app"}

if [[ ! -f "$binary" || ! -x "$binary" || "$app" != *.app ]]; then
    echo 'Usage: bundle.sh [executable] [output.app]' >&2
    exit 1
fi

mkdir -p "$app/Contents/MacOS"
install -m 755 "$binary" "$app/Contents/MacOS/Tokenwatch"
install -m 644 "$package_dir/scripts/Info.plist" "$app/Contents/Info.plist"
codesign --force --sign - "$app"
codesign --verify --strict "$app"
