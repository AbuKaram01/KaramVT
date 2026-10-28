#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# One-command installer for KaramVT on Arch-based systems (Arch, Manjaro, EndeavourOS...),
# from a git checkout — installs into your own $HOME, like scripts/install-debian.sh.
# Run from anywhere:  ./scripts/install-arch.sh [--cuda]
#
# By default llama-cpp-python (the translation engine) is built for the CPU. Pass --cuda to
# install the `cuda` package and compile it with CUDA support instead; run the script again
# with or without the flag to switch an existing .venv between the two.
#
# Prefer a real package? Build one with packaging/build-arch.sh (see the README).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

APP_ID="io.github.AbuKaram01.KaramVT"
REQUIRED_PYTHON="3.10"
USE_CUDA=0

info() { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$1"; }
fail() { printf '\033[1;31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

for argument in "$@"; do
    case "$argument" in
        --cuda) USE_CUDA=1 ;;
        -h|--help)
            echo "Usage: ./scripts/install-arch.sh [--cuda]"
            echo "  --cuda   install the cuda package and build llama-cpp-python with CUDA support"
            exit 0
            ;;
        *) fail "Unknown option: $argument (try --help)" ;;
    esac
done
VARIANT=cpu
[ "$USE_CUDA" -eq 1 ] && VARIANT=cuda

if ! command -v pacman >/dev/null 2>&1; then
    fail "This script is for Arch-based systems (needs pacman). Not found on this machine — on Debian/Ubuntu use scripts/install-debian.sh."
fi

if [ "$EUID" -eq 0 ]; then
    fail "Don't run this as root — it installs into your own \$HOME. Run it as your normal user; it will ask for sudo only for the system packages."
fi

PACKAGES=(
    python python-pip python-gobject gtk4 libadwaita librsvg adwaita-icon-theme gcc make cmake
    tesseract tesseract-data-eng tesseract-data-osd
    tesseract-data-ara tesseract-data-fra tesseract-data-deu tesseract-data-spa
)
[ "$USE_CUDA" -eq 1 ] && PACKAGES+=(cuda)

info "Installing system packages (GTK4, libadwaita, Python bindings, build tools, OCR)..."
# --needed: skip what is already installed (and don't reinstall it).
# gcc/make/cmake: llama-cpp-python is compiled from source during the pip install below.
sudo pacman -S --needed --noconfirm "${PACKAGES[@]}"
ok "System packages installed."

PYTHON_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [ "$(printf '%s\n' "$REQUIRED_PYTHON" "$PYTHON_VERSION" | sort -V | head -n1)" != "$REQUIRED_PYTHON" ]; then
    fail "Python $REQUIRED_PYTHON+ is required — found $PYTHON_VERSION."
fi
ok "Python $PYTHON_VERSION OK."

# Arch is a rolling release: if Python has moved to a new minor version since .venv was
# created, that .venv no longer works — start it over rather than reuse it.
if [ -d .venv ]; then
    VENV_VERSION="$(sed -n 's/^version[[:space:]]*=[[:space:]]*\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' .venv/pyvenv.cfg 2>/dev/null | head -n 1)"
    if [ "$VENV_VERSION" != "$PYTHON_VERSION" ]; then
        info "The existing .venv was made with Python ${VENV_VERSION:-?}, the system now has $PYTHON_VERSION — recreating it."
        rm -rf .venv
    fi
fi

if [ ! -d .venv ]; then
    info "Creating virtual environment (.venv)..."
    python3 -m venv --system-site-packages .venv
    ok "Virtual environment created."
else
    info "Reusing existing .venv"
fi

info "Installing Python dependencies (faster-whisper / llama-cpp-python are large — this can take a few minutes)..."
.venv/bin/pip install --upgrade pip -q

# pip keeps an installed llama-cpp-python as it is, so a build made for the other variant (CPU
# vs CUDA) has to be replaced explicitly. The variant of the last build is noted in .venv.
if .venv/bin/pip show -q llama-cpp-python >/dev/null 2>&1 && \
        [ "$(cat .venv/karamvt-variant 2>/dev/null || echo cpu)" != "$VARIANT" ]; then
    info "Rebuilding llama-cpp-python for the $VARIANT variant..."
    if [ "$USE_CUDA" -eq 1 ]; then
        CMAKE_ARGS="-DGGML_CUDA=on" .venv/bin/pip install --force-reinstall --no-cache-dir --no-deps -q llama-cpp-python
    else
        .venv/bin/pip install --force-reinstall --no-cache-dir --no-deps -q llama-cpp-python
    fi
fi

if [ "$USE_CUDA" -eq 1 ]; then
    CMAKE_ARGS="-DGGML_CUDA=on" .venv/bin/pip install -e . -q
else
    .venv/bin/pip install -e . -q
fi
echo "$VARIANT" > .venv/karamvt-variant
ok "Python dependencies installed ($VARIANT)."

info "Installing the desktop launcher and app icon for your user..."
mkdir -p ~/.local/share/applications ~/.local/share/icons/hicolor
# The launcher's Exec needs an absolute path: "karamvt-gui" only exists inside .venv/bin,
# not on the system PATH, and a desktop entry whose Exec can't be resolved is silently
# not registered at all.
sed "s|^Exec=karamvt-gui|Exec=${PROJECT_ROOT}/.venv/bin/karamvt-gui|" \
    "data/${APP_ID}.desktop" > ~/.local/share/applications/"${APP_ID}.desktop"
cp -r data/icons/hicolor/* ~/.local/share/icons/hicolor/
gtk-update-icon-cache ~/.local/share/icons/hicolor 2>/dev/null || true
ok "Desktop launcher and icon installed."

echo
ok "KaramVT is installed."
echo
echo "  Run it with:"
echo "    ${PROJECT_ROOT}/.venv/bin/karamvt-gui"
echo
echo "  Or activate the virtual environment first:"
echo "    source ${PROJECT_ROOT}/.venv/bin/activate"
echo "    karamvt-gui"
echo
echo "  It should also now appear in your applications menu as \"KaramVT\"."
echo
echo "First launch: open the menu and choose Download Models to fetch a Whisper"
echo "model and a translation model, or open Preferences (Ctrl+,) and point KaramVT"
echo "at the models you already have."
echo
echo "Note: after an Arch update moves Python to a new minor version, re-run this"
echo "script once to rebuild .venv."
