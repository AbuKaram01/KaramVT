# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Checks on the Linux packaging files: the PKGBUILD, the hook, the desktop entry, and that the
shell scripts parse and read the version from the package."""

import re
import subprocess

import pytest
from packaging_support import APP_ID, ARCH, ROOT, needs_bash

import karamvt


SCRIPTS = [
    ROOT / "packaging" / "build.sh",
    ROOT / "packaging" / "build-arch.sh",
    ROOT / "packaging" / "arch" / "karamvt-setup-venv",
    ROOT / "scripts" / "install-debian.sh",
    ROOT / "scripts" / "install-arch.sh",
]


def pkgbuild_value(name):
    match = re.search(rf"^{name}=(.+)$", (ARCH / "PKGBUILD").read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1).strip("'\"") if match else None


def test_the_pkgbuild_version_matches_the_package_version():
    assert pkgbuild_value("pkgver") == karamvt.__version__


def test_the_pkgbuild_builds_a_cpu_and_a_cuda_package_that_conflict():
    text = (ARCH / "PKGBUILD").read_text(encoding="utf-8")
    assert "pkgname=('karamvt' 'karamvt-cuda')" in text
    assert "conflicts=('karamvt-cuda')" in text and "conflicts=('karamvt')" in text
    assert "depends=(\"${_depends[@]}\" 'cuda')" in text


def test_the_pkgbuild_builds_from_a_release_style_tarball_so_it_can_go_to_the_aur():
    """GitHub names the folder inside a release archive after the repository (KaramVT-0.1.0, not karamvt-0.1.0)."""
    text = (ARCH / "PKGBUILD").read_text(encoding="utf-8")
    assert '_srcdir="KaramVT-$pkgver"' in text
    assert "/archive/v$pkgver.tar.gz" in text and "sha256sums=('SKIP')" in text
    assert "$startdir/../.." not in text  # No reaching into the local checkout.


def test_the_pkgbuild_depends_on_what_the_app_needs_at_run_time():
    text = (ARCH / "PKGBUILD").read_text(encoding="utf-8")
    for package in ("python-gobject", "gtk4", "libadwaita", "librsvg", "adwaita-icon-theme", "python-pip",
                    "gcc", "make", "cmake", "tesseract", "tesseract-data-eng", "tesseract-data-osd"):
        assert f"'{package}'" in text, package


def test_the_install_script_never_aborts_the_transaction_and_points_at_the_download_menu():
    content = (ARCH / "karamvt.install").read_text(encoding="utf-8")
    assert "post_install()" in content and "post_upgrade()" in content and "post_remove()" in content
    assert "if ! /usr/lib/karamvt/setup-venv" in content
    assert "Download Models" in content


def test_the_hook_rebuilds_the_environment_after_a_python_upgrade():
    hook = (ARCH / "karamvt.hook").read_text(encoding="utf-8")
    assert "Target = python" in hook and "Exec = /usr/lib/karamvt/setup-venv --if-python-changed" in hook


@needs_bash
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_every_shell_script_parses(script):
    assert script.exists()
    subprocess.run(["bash", "-n", str(script)], check=True)


@needs_bash
def test_the_pkgbuild_parses_and_defines_both_package_functions():
    result = subprocess.run(
        ["bash", "-c", f'source "{ARCH / "PKGBUILD"}"; declare -F package_karamvt package_karamvt-cuda check'],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.split() == ["package_karamvt", "package_karamvt-cuda", "check"]


def test_the_desktop_entry_points_at_the_installed_launcher_and_icon():
    entry = (ROOT / "data" / f"{APP_ID}.desktop").read_text(encoding="utf-8")
    assert "Exec=karamvt-gui" in entry and f"Icon={APP_ID}" in entry


def test_the_license_file_and_spdx_headers_are_present():
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in (ROOT / "LICENSE").read_text(encoding="utf-8")
    for module in [*(ROOT / "src" / "karamvt").rglob("*.py"), *(ROOT / "tests").glob("*.py")]:
        assert "SPDX-License-Identifier: AGPL-3.0-or-later" in module.read_text(encoding="utf-8"), module


def test_the_build_scripts_read_the_version_from_the_package_and_do_not_delete_each_others_packages():
    """build.sh and build-arch.sh share packaging/dist; neither may wipe the whole folder."""
    for script in ("build.sh", "build-arch.sh"):
        text = (ROOT / "packaging" / script).read_text(encoding="utf-8")
        assert "src/karamvt/__init__.py" in text, script
        assert 'rm -rf "$OUT_DIR"' not in text and 'rm -rf "$WORK_DIR" "$OUT_DIR"' not in text, script
