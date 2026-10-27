#!/usr/bin/env bash
# Populate build-local caches only. No root, live accounts, or cloud commands.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="$ROOT/build/fedora"
SYSTEM_PYTHON="${SYSTEM_PYTHON:-/usr/bin/python3}"
UV="${UV:-$(command -v uv || true)}"
[[ -n "$UV" ]] || UV="$HOME/.local/bin/uv"
[[ -x "$UV" ]] || { printf 'Install uv or set UV to its executable.\n' >&2; exit 1; }
for command in node npm rpmbuild desktop-file-validate tar gzip; do
    command -v "$command" >/dev/null || { printf 'Missing build prerequisite: %s\n' "$command" >&2; exit 1; }
done
[[ "$(uname -m)" == x86_64 ]] || { printf 'This package targets x86_64 only.\n' >&2; exit 1; }
"$SYSTEM_PYTHON" -c 'import sys; assert sys.version_info[:2] == (3, 14), "Fedora Python 3.14 required"'
node -e 'if (Number(process.versions.node.split(".")[0]) < 22) throw Error("Node 22+ required")'
mkdir -p "$BUILD"
# An optional cache source is read-only. cp never hardlinks files to that source.
if [[ -n "${AUTOZCODER_CACHE_FROM:-}" ]]; then
    SOURCE="$(realpath "$AUTOZCODER_CACHE_FROM")"
    [[ "$SOURCE" != "$(realpath "$BUILD")" ]] || { printf 'Cache source must differ from destination.\n' >&2; exit 1; }
    for name in python generator-packages browsers node-tools; do
        if [[ ! -e "$BUILD/$name" && -d "$SOURCE/$name" ]]; then
            printf 'Copying cache %s (reflinks where supported)\n' "$name"
            cp -a --reflink=auto "$SOURCE/$name" "$BUILD/$name"
        fi
    done
fi
# Reuse an explicitly supplied or uv-managed pinned interpreter read-only.
# If absent, install it under build/, never in an older source checkout.
PRIVATE_PYTHON="${AUTOZCODER_PYTHON_ROOT:-}"
if [[ -z "$PRIVATE_PYTHON" ]]; then
    FOUND="$("$UV" python find --managed-python --no-python-downloads 3.12.14 2>/dev/null || true)"
    if [[ -n "$FOUND" ]]; then PRIVATE_PYTHON="$(dirname "$(dirname "$(realpath "$FOUND")")")"; fi
fi
if [[ -z "$PRIVATE_PYTHON" ]]; then
    export UV_PYTHON_INSTALL_DIR="$BUILD/python-install"
    "$UV" python install 3.12.14
    FOUND="$("$UV" python find --managed-python --no-python-downloads 3.12.14)"
    PRIVATE_PYTHON="$(dirname "$(dirname "$(realpath "$FOUND")")")"
fi
[[ -x "$PRIVATE_PYTHON/bin/python3.12" ]] || { printf 'Invalid AUTOZCODER_PYTHON_ROOT\n' >&2; exit 1; }
printf '%s\n' "$PRIVATE_PYTHON" > "$BUILD/python-root.txt"
# Strict sync removes stale packages. Version pins are committed; uv uses its wheel
# cache when available. Copy mode prevents later staging edits touching that cache.
"$UV" pip sync --python "$SYSTEM_PYTHON" --target "$BUILD/python" --link-mode copy \
    "$ROOT/packaging/fedora/requirements.lock"
"$UV" pip sync --python "$PRIVATE_PYTHON/bin/python3.12" --target "$BUILD/generator-packages" --link-mode copy \
    "$ROOT/packaging/fedora/generator-requirements.lock"
if [[ -z "${PATCHELF:-}" ]] && ! command -v patchelf >/dev/null; then
    "$UV" pip sync --python "$PRIVATE_PYTHON/bin/python3.12" --target "$BUILD/build-tools" --link-mode copy \
        "$ROOT/packaging/fedora/build-requirements.lock"
fi
mkdir -p "$BUILD/node-tools"
# npm ci uses the committed integrity-checked lock. Reuse only a matching cache;
# its dependency tree is validated before staging (no npx/latest resolution).
if ! cmp -s "$ROOT/packaging/fedora/node-tools/package-lock.json" "$BUILD/node-tools/package-lock.json" || \
   ! (cd "$BUILD/node-tools" && npm ls --all --offline >/dev/null 2>&1); then
    cp "$ROOT/packaging/fedora/node-tools/"package*.json "$BUILD/node-tools/"
    (cd "$BUILD/node-tools" && npm ci --no-audit --no-fund)
fi
# --no-shell downloads the full pinned Chromium plus FFmpeg, not the separate
# headless shell. Runtime launch selects this executable for headless use too.
if [[ ! -f "$BUILD/browsers/chromium-1248/chrome-linux64/chrome" || \
      ! -f "$BUILD/browsers/ffmpeg-1013/ffmpeg-linux" ]]; then
    PLAYWRIGHT_BROWSERS_PATH="$BUILD/browsers" PYTHONPATH="$BUILD/generator-packages" \
        "$PRIVATE_PYTHON/bin/python3.12" -m playwright install chromium --no-shell
fi
"$SYSTEM_PYTHON" "$ROOT/packaging/fedora/check-dependencies.py" "$PRIVATE_PYTHON"
printf 'Build caches ready. Next: bash packaging/fedora/build-rpm.sh\n'
