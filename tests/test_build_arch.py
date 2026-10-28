# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""packaging/build-arch.sh, run with makepkg replaced by a stand-in that only records what it is asked."""

import os
import shutil
import subprocess
import tarfile

import pytest
from packaging_support import APP_ID, ROOT, make_executable, needs_bash

import karamvt


FAKE_MAKEPKG = r"""#!/usr/bin/env bash
# Does the parts of makepkg that matter here: unpack the source, then run check() and each package function.
set -euo pipefail
echo "makepkg $*" >> "$FAKE_LOG"
startdir="$PWD"
srcdir="$startdir/src"
mkdir -p "$srcdir"
source ./PKGBUILD
tar -xzf "$SRCDEST/$pkgbase-$pkgver.tar.gz" -C "$srcdir"
check
all_names=("${pkgname[@]}")
for name in "${all_names[@]}"; do
    pkgname="$name"                                             # makepkg sets it to the package being built
    pkgdir="$startdir/pkg/$name"
    mkdir -p "$pkgdir"
    "package_$name"
    : > "$PKGDEST/$name-$pkgver-$pkgrel-x86_64.pkg.tar.zst"
done
"""


@pytest.fixture
def built(tmp_path):
    """A copy of the project built by build-arch.sh with the stand-in makepkg."""
    if os.geteuid() == 0:
        pytest.skip("makepkg, and so build-arch.sh, refuses to run as root")
    project = tmp_path / "project"
    shutil.copytree(
        ROOT, project, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", "dist", "work"),
    )
    (project / ".git").mkdir()  # Must be left out of the tarball.
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    bin_dir = tmp_path / "stubs"
    bin_dir.mkdir()
    make_executable(bin_dir / "makepkg", FAKE_MAKEPKG)
    log = tmp_path / "makepkg.log"
    log.touch()
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_LOG": str(log)}
    result = subprocess.run(
        ["bash", str(project / "packaging" / "build-arch.sh"), "--noconfirm"],
        env=env, capture_output=True, text=True, cwd=project,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return project, log


@needs_bash
def test_build_arch_makes_a_github_style_tarball_and_both_packages(built):
    project, log = built
    version = karamvt.__version__
    work = project / "packaging" / "work" / "arch"

    names = tarfile.open(work / f"karamvt-{version}.tar.gz").getnames()
    assert all(name.startswith(f"KaramVT-{version}") for name in names)  # One top-level folder.
    assert f"KaramVT-{version}/packaging/arch/PKGBUILD" in names
    assert f"KaramVT-{version}/src/karamvt/__init__.py" in names
    # The .git folder stays out of the tarball; files like .gitignore belong in it.
    assert not any(
        name.endswith("/.git") or "/.git/" in name or "__pycache__" in name or "/packaging/work" in name
        for name in names
    )

    dist = project / "packaging" / "dist"
    assert sorted(path.name for path in dist.iterdir()) == sorted([
        f"karamvt-{version}-1-x86_64.pkg.tar.zst", f"karamvt-cuda-{version}-1-x86_64.pkg.tar.zst",
    ])
    assert "--noconfirm" in log.read_text() and "--cleanbuild" in log.read_text()


@needs_bash
@pytest.mark.parametrize("name, variant", [("karamvt", "cpu"), ("karamvt-cuda", "cuda")])
def test_each_arch_package_contains_the_app_launcher_desktop_files_hook_and_setup_script(built, name, variant):
    project, _log = built
    pkg = project / "packaging" / "work" / "arch" / "pkg" / name

    assert (pkg / "opt/karamvt/variant").read_text() == variant + "\n"
    assert (pkg / "opt/karamvt/src/pyproject.toml").is_file() and (pkg / "opt/karamvt/src/LICENSE").is_file()
    assert (pkg / "opt/karamvt/src/src/karamvt/gui/application.py").is_file()
    assert (pkg / "opt/karamvt/src/src/karamvt/services/downloads.py").is_file()
    assert not list(pkg.rglob("__pycache__"))

    launcher = pkg / "usr/bin/karamvt-gui"
    assert launcher.stat().st_mode & 0o111 and "/opt/karamvt/venv/bin/python3 -m karamvt.gui" in launcher.read_text()
    setup = pkg / "usr/lib/karamvt/setup-venv"
    assert setup.stat().st_mode & 0o111 and setup.read_text().startswith("#!/bin/sh")
    assert (pkg / "usr/share/libalpm/hooks/karamvt-python.hook").is_file()
    assert (pkg / f"usr/share/applications/{APP_ID}.desktop").is_file()
    assert (pkg / f"usr/share/icons/hicolor/scalable/apps/{APP_ID}.svg").is_file()
    assert (pkg / f"usr/share/licenses/{name}/LICENSE").is_file()
    assert not (pkg / "opt/karamvt/src/tests").exists() and not (pkg / "opt/karamvt/src/.git").exists()


@needs_bash
def test_build_arch_refuses_a_pkgbuild_whose_version_differs_from_the_package(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("makepkg, and so build-arch.sh, refuses to run as root")
    project = tmp_path / "project"
    (project / "packaging" / "arch").mkdir(parents=True)
    (project / "src" / "karamvt").mkdir(parents=True)
    shutil.copy(ROOT / "packaging" / "build-arch.sh", project / "packaging")
    (project / "src" / "karamvt" / "__init__.py").write_text('__version__ = "9.9.9"\n')
    (project / "packaging" / "arch" / "PKGBUILD").write_text("pkgver=0.1.0\n")
    result = subprocess.run(["bash", str(project / "packaging" / "build-arch.sh")], capture_output=True, text=True)
    assert result.returncode != 0 and "Version mismatch" in result.stderr
