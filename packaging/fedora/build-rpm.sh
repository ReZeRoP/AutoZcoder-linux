#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="$ROOT/build/fedora"
SYSTEM_PYTHON="${SYSTEM_PYTHON:-/usr/bin/python3}"
VERSION="$("$SYSTEM_PYTHON" "$ROOT/packaging/fedora/version.py")"
STAGE="$BUILD/autozcoder-$VERSION-fedora"
# Bootstrap is intentionally separate: this step never installs/downloads assets.
if [[ -n "${AUTOZCODER_PYTHON_ROOT:-}" ]]; then
    PYTHON_ROOT="$AUTOZCODER_PYTHON_ROOT"
elif [[ -f "$BUILD/python-root.txt" ]]; then
    read -r PYTHON_ROOT < "$BUILD/python-root.txt"
else
    printf 'Run bash packaging/fedora/bootstrap.sh first.\n' >&2; exit 1
fi
export PATCHELF="${PATCHELF:-$(command -v patchelf || true)}"
[[ -n "$PATCHELF" ]] || PATCHELF="$BUILD/build-tools/bin/patchelf"
[[ -x "$PATCHELF" ]] || { printf 'Missing patchelf; run bootstrap.sh.\n' >&2; exit 1; }
SPEC_VERSION="$(rpmspec --query --srpm --queryformat '%{VERSION}' "$ROOT/packaging/fedora/autozcoder.spec")"
[[ "$VERSION" == "$SPEC_VERSION" ]] || { printf 'App/spec version mismatch: %s / %s\n' "$VERSION" "$SPEC_VERSION" >&2; exit 1; }
"$SYSTEM_PYTHON" "$ROOT/packaging/fedora/check-dependencies.py" "$PYTHON_ROOT"
# Guard against building during a merge and accidental conflict-marker payloads.
if [[ -n "$(git -C "$ROOT" ls-files --unmerged)" ]]; then
    printf 'Resolve the git merge before staging the release.\n' >&2; exit 1
fi
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --format=%ct)}"
export TZ=UTC LC_ALL=C PYTHONHASHSEED=0
mkdir -p "$BUILD/rpmbuild/SOURCES" "$BUILD/rpmbuild/SPECS"
"$SYSTEM_PYTHON" "$ROOT/packaging/fedora/stage-runtime.py" "$ROOT" "$STAGE" "$PYTHON_ROOT"
# Stable order, timestamps, ownership, and gzip headers; host RPM/toolchain can
# still differ, so this is not a promise of bit-for-bit cross-host RPM identity.
tar --sort=name --mtime="@$SOURCE_DATE_EPOCH" --owner=0 --group=0 --numeric-owner \
    -C "$BUILD" -cf - "autozcoder-$VERSION-fedora" | gzip -n > \
    "$BUILD/rpmbuild/SOURCES/autozcoder-$VERSION-fedora.tar.gz"
cp "$ROOT/packaging/fedora/autozcoder.spec" "$BUILD/rpmbuild/SPECS/"
# RPM testing is offline imports/tool versions only, never account workflows.
rpmbuild -bb --define "_topdir $BUILD/rpmbuild" \
    --define 'clamp_mtime_to_source_date_epoch 1' \
    --define 'use_source_date_epoch_as_buildtime 1' \
    "$BUILD/rpmbuild/SPECS/autozcoder.spec"
OUT="$BUILD/release"
mkdir -p "$OUT"
mapfile -t RPMS < <(find "$BUILD/rpmbuild/RPMS" -name "autozcoder-$VERSION-*.rpm" -type f | sort)
[[ ${#RPMS[@]} == 1 ]] || { printf 'Expected one matching RPM, found %s\n' "${#RPMS[@]}" >&2; exit 1; }
cp "${RPMS[0]}" "$OUT/"
cp "$STAGE/RUNTIME-MANIFEST.json" "$OUT/autozcoder-$VERSION-runtime-manifest.json"
(cd "$OUT" && sha256sum "$(basename "${RPMS[0]}")" "autozcoder-$VERSION-runtime-manifest.json" > SHA256SUMS)
printf 'Release artifacts: %s\n' "$OUT"
