#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Builds karamvt-<ver>-1-x86_64.pkg.tar.zst and karamvt-cuda-<ver>-1-x86_64.pkg.tar.zst
# into packaging/dist/ from the current checkout.
#
# Run it on Arch Linux (or Manjaro, EndeavourOS, CachyOS, ...) as a normal user:
# makepkg refuses to run as root. Extra arguments are passed to makepkg, for example
# `./packaging/build-arch.sh --syncdeps` to install missing build dependencies first.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="$PROJECT_ROOT/packaging/work/arch"
OUT_DIR="$PROJECT_ROOT/packaging/dist"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$PROJECT_ROOT/src/karamvt/__init__.py")"
PKGBUILD_VERSION="$(sed -n 's/^pkgver=\(.*\)/\1/p' "$PROJECT_ROOT/packaging/arch/PKGBUILD")"

info() { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$1"; }
fail() { printf '\033[1;31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

[ -n "$VERSION" ] || fail "Could not read the version from src/karamvt/__init__.py."
[ "$VERSION" = "$PKGBUILD_VERSION" ] || \
    fail "Version mismatch: src/karamvt/__init__.py says $VERSION but PKGBUILD says $PKGBUILD_VERSION."
command -v makepkg >/dev/null 2>&1 || fail "makepkg not found. Run this on an Arch-based system."
[ "$EUID" -ne 0 ] || fail "Don't run this as root: makepkg refuses to. Use your normal user."

# Clean only what this script produces, so packages built by build.sh survive.
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR" "$OUT_DIR"
rm -f "$OUT_DIR"/karamvt*.pkg.tar.*
cp "$PROJECT_ROOT"/packaging/arch/* "$WORK_DIR/"

# The tarball has the same layout as GitHub's release archive (one top-level folder, named
# after the repository), so the PKGBUILD works unchanged for local builds and for the AUR.
info "Creating the source tarball for $VERSION..."
tar -czf "$WORK_DIR/karamvt-$VERSION.tar.gz" \
    --exclude='./packaging/work' --exclude='./packaging/dist' --exclude='./.git' \
    --exclude='./.venv' --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.ruff_cache' \
    --exclude='*.egg-info' \
    --transform "s,^\./,KaramVT-$VERSION/," --transform "s,^\.$,KaramVT-$VERSION," \
    -C "$PROJECT_ROOT" .

info "Building the packages with makepkg..."
(cd "$WORK_DIR" && SRCDEST="$WORK_DIR" PKGDEST="$OUT_DIR" makepkg --force --clean --cleanbuild "$@")

echo
ok "Packages built in: $OUT_DIR"
ls -la "$OUT_DIR"
echo
echo "Install one with:  sudo pacman -U $OUT_DIR/karamvt-$VERSION-1-x86_64.pkg.tar.*"
echo "Use the karamvt-cuda-* file for the CUDA build instead; the two conflict with each other."
