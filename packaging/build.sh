#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Builds, into packaging/dist/, every package format this machine has the tools for:
#   karamvt_<ver>_amd64.deb         karamvt-cuda_<ver>_amd64.deb         (Debian/Ubuntu)
#   karamvt-<ver>-1.x86_64.rpm      karamvt-cuda-<ver>-1.x86_64.rpm      (Fedora/RHEL family)
#   karamvt-<ver>-1-x86_64.pkg.tar.*  karamvt-cuda-<ver>-1-x86_64.pkg.tar.*  (Arch Linux,
#                                                          built by packaging/build-arch.sh)
#
# These packages ship NO Python dependencies — they're small. On install,
# their postinst/%post/.install script creates a venv at /opt/karamvt/venv and
# runs `pip install` there, which needs internet access and a C++ toolchain
# (llama-cpp-python has no prebuilt wheel; pip compiles it from source).
# That's a deliberate trade-off for a much simpler build — see the
# project's packaging notes for the alternative (fully self-contained,
# no-internet-at-install-time) approach.
#
# Tools needed on the machine that RUNS this script (not on the machine
# installing the packages) — at least one set:
#   .deb  → dpkg-deb  (Debian/Ubuntu: dpkg-dev)
#   .rpm  → rpmbuild  (the `rpm` package; `rpm-build` on Fedora)
#   Arch  → makepkg   (Arch Linux; must NOT be run as root — makepkg refuses)
# Formats whose tool is missing are skipped, with a note at the end.
# Extra makepkg options can be passed with MAKEPKG_FLAGS (e.g. MAKEPKG_FLAGS=-s to
# install missing dependencies with pacman first). The Arch packages are built by
# packaging/build-arch.sh, which you can also run on its own.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="$PROJECT_ROOT/packaging/work"
OUT_DIR="$PROJECT_ROOT/packaging/dist"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$PROJECT_ROOT/src/karamvt/__init__.py")"
APP_ID="io.github.AbuKaram01.KaramVT"
MAINTAINER="AbuKaram01 <AbuKaram01@users.noreply.github.com>"

[ -n "$VERSION" ] || { echo "Could not read the version from src/karamvt/__init__.py." >&2; exit 1; }

info() { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$1"; }

warn() { printf '\033[1;33m!\033[0m %s\n' "$1"; }

HAVE_DEB=0; HAVE_RPM=0; HAVE_ARCH=0
command -v dpkg-deb >/dev/null 2>&1 && HAVE_DEB=1
command -v rpmbuild >/dev/null 2>&1 && HAVE_RPM=1
if command -v makepkg >/dev/null 2>&1; then
    if [ "$(id -u)" -eq 0 ]; then
        warn "makepkg is installed but refuses to run as root — run build.sh as a normal user to build the Arch packages."
    else
        HAVE_ARCH=1
    fi
fi
if [ $((HAVE_DEB + HAVE_RPM + HAVE_ARCH)) -eq 0 ]; then
    echo "No packaging tool found. Install at least one of: dpkg-deb (dpkg-dev), rpmbuild (rpm / rpm-build)," >&2
    echo "or run on Arch Linux as a normal user with makepkg available." >&2
    exit 1
fi

# Clean only what this script produces, so packages built by build-arch.sh survive.
rm -rf "$WORK_DIR/cpu" "$WORK_DIR/cuda"
mkdir -p "$WORK_DIR" "$OUT_DIR"
rm -f "$OUT_DIR"/karamvt*_amd64.deb "$OUT_DIR"/karamvt*.x86_64.rpm

# ── Shared payload: identical for every variant/format ──────────────────────
# (only the postinst build command and package metadata differ per variant)
build_payload() {
    local payload="$1"
    mkdir -p "$payload/opt/karamvt/src"
    cp -r "$PROJECT_ROOT/src" "$PROJECT_ROOT/pyproject.toml" "$payload/opt/karamvt/src/"
    find "$payload/opt/karamvt/src" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

    mkdir -p "$payload/usr/bin"
    cat > "$payload/usr/bin/karamvt-gui" << 'LAUNCHER'
#!/bin/sh
# Fixed path — always correct, since this package always installs to
# /opt/karamvt. (This is the whole reason the old "Exec= can't find the
# venv" problem doesn't come up here: unlike a git-clone-anywhere install,
# a real package always lands in the same place.)
exec /opt/karamvt/venv/bin/python3 -m karamvt.gui "$@"
LAUNCHER
    chmod 755 "$payload/usr/bin/karamvt-gui"

    mkdir -p "$payload/usr/share/applications"
    cp "$PROJECT_ROOT/data/${APP_ID}.desktop" "$payload/usr/share/applications/"

    mkdir -p "$payload/usr/share/icons"
    cp -r "$PROJECT_ROOT/data/icons/hicolor" "$payload/usr/share/icons/"
}

for variant in cpu cuda; do
    case "$variant" in
        cpu)
            pkg_name="karamvt"
            conflicts_pkg="karamvt-cuda"
            cmake_env=""
            summary_suffix=""
            ;;
        cuda)
            pkg_name="karamvt-cuda"
            conflicts_pkg="karamvt"
            cmake_env='CMAKE_ARGS="-DGGML_CUDA=on" '
            summary_suffix=" (CUDA-accelerated translation)"
            ;;
    esac

    info "Building payload for $pkg_name..."
    variant_dir="$WORK_DIR/$variant"
    payload="$variant_dir/payload"
    build_payload "$payload"

    # ── postinst (deb) / %post (rpm share the same body) ────────────────────
    postinst_body=$(cat << POSTINST
APP_DIR=/opt/karamvt
VENV_DIR="\$APP_DIR/venv"
echo "KaramVT: setting up its Python environment — needs internet access"
echo "and a C/C++ toolchain, and can take several minutes the first time"
echo "(it compiles llama-cpp-python from source; there's no prebuilt wheel)."
if [ ! -d "\$VENV_DIR" ]; then
    python3 -m venv --system-site-packages "\$VENV_DIR"
fi
"\$VENV_DIR/bin/pip" install --upgrade pip --quiet
${cmake_env}"\$VENV_DIR/bin/pip" install --quiet "\$APP_DIR/src"
echo "KaramVT is ready. Find it in your applications menu, or run: karamvt-gui"
POSTINST
)

    # ==== .deb ==============================================================
    if [ "$HAVE_DEB" -eq 1 ]; then
    info "Building ${pkg_name}_${VERSION}_amd64.deb..."
    deb_root="$variant_dir/deb-root"
    rm -rf "$deb_root"
    cp -r "$payload" "$deb_root"
    mkdir -p "$deb_root/DEBIAN"

    cat > "$deb_root/DEBIAN/control" << CONTROL
Package: $pkg_name
Version: $VERSION
Section: utils
Priority: optional
Architecture: amd64
Depends: python3 (>= 3.10), python3-venv, python3-pip, python3-gi, python3-gi-cairo, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.5), build-essential, cmake, tesseract-ocr, tesseract-ocr-osd
Conflicts: $conflicts_pkg
Maintainer: $MAINTAINER
Description: Karam Video Translator${summary_suffix} - offline video, text & OCR translator
 Generate and translate video subtitles fully offline, using local Whisper
 and LLM models. Downloads and compiles its Python dependencies on first
 install (needs internet access then; not needed afterwards to run it).
CONTROL

    { printf '#!/bin/sh\nset -e\n%s\nexit 0\n' "$postinst_body"; } > "$deb_root/DEBIAN/postinst"
    cat > "$deb_root/DEBIAN/postrm" << 'POSTRM'
#!/bin/sh
set -e
if [ "$1" = "purge" ] || [ "$1" = "remove" ]; then
    rm -rf /opt/karamvt/venv
fi
exit 0
POSTRM
    chmod 755 "$deb_root/DEBIAN/postinst" "$deb_root/DEBIAN/postrm"

    dpkg-deb --build --root-owner-group "$deb_root" "$OUT_DIR/${pkg_name}_${VERSION}_amd64.deb" >/dev/null
    ok "${pkg_name}_${VERSION}_amd64.deb"
    fi

    # ==== .rpm ==============================================================
    if [ "$HAVE_RPM" -eq 1 ]; then
    info "Building ${pkg_name}-${VERSION}-1.x86_64.rpm..."
    rpm_topdir="$variant_dir/rpmbuild"
    mkdir -p "$rpm_topdir"/{BUILD,RPMS,SOURCES,SPECS,SRPMS,BUILDROOT}

    spec_file="$rpm_topdir/SPECS/${pkg_name}.spec"
    cat > "$spec_file" << SPEC
Name: $pkg_name
Version: $VERSION
Release: 1
Summary: Karam Video Translator${summary_suffix} - offline video, text & OCR translator
License: AGPL-3.0-or-later
BuildArch: x86_64
Requires: python3 >= 3.10, python3-pip, python3-gobject, gtk4, libadwaita >= 1.5, gcc-c++, cmake, tesseract, tesseract-osd
Conflicts: $conflicts_pkg

%description
Generate and translate video subtitles fully offline, using local Whisper
and LLM models. Downloads and compiles its Python dependencies on first
install (needs internet access then; not needed afterwards to run it).

%install
mkdir -p %{buildroot}
cp -a $payload/. %{buildroot}/

%files
/opt/karamvt
/usr/bin/karamvt-gui
/usr/share/applications/*.desktop
/usr/share/icons/hicolor

%post
$postinst_body

%postun
if [ "\$1" = "0" ]; then
    rm -rf /opt/karamvt/venv
fi

%clean
rm -rf %{buildroot}
SPEC

    rpmbuild --define "_topdir $rpm_topdir" -bb "$spec_file" >/dev/null 2>&1 || \
        rpmbuild --define "_topdir $rpm_topdir" -bb "$spec_file"
    find "$rpm_topdir/RPMS" -name "*.rpm" -exec cp {} "$OUT_DIR/" \;
    ok "${pkg_name}-${VERSION}-1.x86_64.rpm"
    fi
done

# ==== Arch (.pkg.tar.*) =====================================================
# One PKGBUILD produces both packages (karamvt and karamvt-cuda); build-arch.sh makes the
# source tarball from this checkout and runs makepkg, leaving the result in packaging/dist.
if [ "$HAVE_ARCH" -eq 1 ]; then
    info "Building karamvt + karamvt-cuda for Arch (packaging/build-arch.sh)..."
    # shellcheck disable=SC2086  # MAKEPKG_FLAGS is meant to word-split
    "$PROJECT_ROOT/packaging/build-arch.sh" --noconfirm ${MAKEPKG_FLAGS:-}
    for built in "$OUT_DIR"/karamvt*.pkg.tar.*; do ok "$(basename "$built")"; done
fi

echo
ok "Packages built in: $OUT_DIR"
ls -la "$OUT_DIR"
[ "$HAVE_DEB" -eq 1 ]  || warn "Skipped .deb  (dpkg-deb not found)"
[ "$HAVE_RPM" -eq 1 ]  || warn "Skipped .rpm  (rpmbuild not found)"
[ "$HAVE_ARCH" -eq 1 ] || warn "Skipped Arch package (makepkg not found, or running as root)"
exit 0
