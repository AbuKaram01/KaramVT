# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Checks on the installation files: versions agree, scripts parse, the installers do the right steps.

The installers refuse to run as root (as they should), so the tests that run them are skipped for
root. makepkg, pacman and Python's venv are replaced by stand-ins that only record what they are asked.
"""

import os
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tarfile
import zipfile
from pathlib import Path

import pytest

import karamvt

ROOT = Path(__file__).resolve().parent.parent
ARCH = ROOT / "packaging" / "arch"
APP_ID = "io.github.AbuKaram01.KaramVT"
SCRIPTS = [
    ROOT / "packaging" / "build.sh",
    ROOT / "packaging" / "build-arch.sh",
    ROOT / "packaging" / "arch" / "karamvt-setup-venv",
    ROOT / "scripts" / "install-debian.sh",
    ROOT / "scripts" / "install-arch.sh",
]

# The shell scripts and their stubs are POSIX-only (":"-separated PATH, /bin/sh), so they are skipped on Windows.
needs_bash = pytest.mark.skipif(
    shutil.which("bash") is None or sys.platform == "win32", reason="needs bash on a POSIX system",
)
not_root = pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="the installers refuse to run as root",
)
needs_pwsh = pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 (pwsh) is not available")
WINDOWS = ROOT / "packaging" / "windows"
POWERSHELL_SCRIPTS = [
    ROOT / "scripts" / "install-windows.ps1",
    ROOT / "scripts" / "uninstall-windows.ps1",
    ROOT / "packaging" / "build-windows.ps1",
    ROOT / "scripts" / "tesseract-portable.ps1",
    ROOT / "scripts" / "gtk-portable.ps1",
    ROOT / "scripts" / "python-bundle.ps1",
    ROOT / "scripts" / "vcruntime-bundle.ps1",
    ROOT / "packaging" / "windows" / "check-installed.ps1",
    ROOT / "packaging" / "windows" / "run-installer.ps1",
]


def pkgbuild_value(name):
    match = re.search(rf"^{name}=(.+)$", (ARCH / "PKGBUILD").read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1).strip("'\"") if match else None


def make_executable(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


# -- static checks ---------------------------------------------------------------------------------

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
    assert "$startdir/../.." not in text                      # No reaching into the local checkout.


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


# -- the venv setup script, run against a stand-in python ---------------------------------------------

def fake_python(directory: Path, log: Path, version: str = "3.13") -> Path:
    """A python3 that answers the version question and 'creates' a venv whose pip only logs its calls."""
    bin_dir = directory / "bin"
    bin_dir.mkdir(exist_ok=True)
    make_executable(bin_dir / "python3", f"""#!/bin/sh
case "$1" in
    -c) echo {version}; exit 0 ;;
    -m) # python3 -m venv --system-site-packages DIR
        DIR="$4"
        echo "python3 -m venv $DIR" >> "{log}"
        mkdir -p "$DIR/bin"
        printf 'home = /usr/bin\\nversion = {version}.1\\n' > "$DIR/pyvenv.cfg"
        cat > "$DIR/bin/pip" <<'PIP'
#!/bin/sh
echo "CMAKE_ARGS=[$CMAKE_ARGS] pip $*" >> "{log}"
PIP
        chmod +x "$DIR/bin/pip" ;;
esac
""")
    return bin_dir


def run_setup(tmp_path, *arguments, variant=None, version="3.13"):
    log = tmp_path / "calls.log"
    log.write_text("")                                          # Only this run's calls.
    bin_dir = fake_python(tmp_path, log, version)
    app_dir = tmp_path / "opt"
    app_dir.mkdir(exist_ok=True)
    if variant:
        (app_dir / "variant").write_text(variant + "\n")
    env = {**os.environ, "KARAMVT_APP_DIR": str(app_dir), "KARAMVT_PYTHON": str(bin_dir / "python3")}
    env.pop("CMAKE_ARGS", None)
    subprocess.run(["sh", str(ARCH / "karamvt-setup-venv"), *arguments], check=True, env=env, capture_output=True)
    return log.read_text(encoding="utf-8").splitlines(), app_dir


@needs_bash
def test_the_cpu_variant_installs_the_app_without_cuda(tmp_path):
    calls, app_dir = run_setup(tmp_path, variant="cpu")
    assert calls[0] == f"python3 -m venv {app_dir}/venv"
    assert "install --upgrade pip" in calls[1]
    assert calls[2] == f"CMAKE_ARGS=[] pip install --quiet {app_dir}/src" and len(calls) == 3


@needs_bash
def test_the_cuda_variant_builds_llama_cpp_with_cuda(tmp_path):
    calls, app_dir = run_setup(tmp_path, variant="cuda")
    assert calls[-1] == f"CMAKE_ARGS=[-DGGML_CUDA=on] pip install --quiet {app_dir}/src"


@needs_bash
def test_without_a_variant_file_the_cpu_build_is_used(tmp_path):
    calls, app_dir = run_setup(tmp_path)
    assert calls[-1] == f"CMAKE_ARGS=[] pip install --quiet {app_dir}/src"


@needs_bash
def test_an_existing_environment_of_the_same_python_is_reused_not_recreated(tmp_path):
    run_setup(tmp_path, variant="cpu")
    calls, _ = run_setup(tmp_path, variant="cpu")
    assert not any(call.startswith("python3 -m venv") for call in calls)


@needs_bash
def test_an_environment_made_by_another_python_is_recreated(tmp_path):
    run_setup(tmp_path, variant="cpu", version="3.13")
    calls, app_dir = run_setup(tmp_path, variant="cpu", version="3.14")
    assert f"python3 -m venv {app_dir}/venv" in calls
    assert (app_dir / "venv" / "pyvenv.cfg").read_text().count("version = 3.14") == 1


@needs_bash
def test_the_python_hook_does_nothing_without_an_environment_or_when_python_did_not_change(tmp_path):
    calls, _ = run_setup(tmp_path, "--if-python-changed", variant="cpu")
    assert calls == []                                          # No environment yet: nothing to rebuild.

    run_setup(tmp_path, variant="cpu", version="3.13")
    calls, _ = run_setup(tmp_path, "--if-python-changed", variant="cpu", version="3.13")
    assert calls == []                                          # Same minor version.


@needs_bash
def test_the_python_hook_rebuilds_the_environment_after_a_new_minor_version(tmp_path):
    run_setup(tmp_path, variant="cuda", version="3.13")
    calls, app_dir = run_setup(tmp_path, "--if-python-changed", variant="cuda", version="3.14")
    assert f"python3 -m venv {app_dir}/venv" in calls
    assert calls[-1] == f"CMAKE_ARGS=[-DGGML_CUDA=on] pip install --quiet {app_dir}/src"


# -- scripts/install-arch.sh, with pacman and python stood in for ----------------------------------------

class Checkout:
    """A throwaway project folder holding a copy of install-arch.sh and the data files it installs."""

    def __init__(self, tmp_path):
        self.root = tmp_path / "KaramVT"
        (self.root / "scripts").mkdir(parents=True)
        shutil.copy(ROOT / "scripts" / "install-arch.sh", self.root / "scripts")
        shutil.copytree(ROOT / "data", self.root / "data")
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.log = tmp_path / "calls.log"
        self.log.touch()
        self.state = tmp_path / "llama-installed"
        bin_dir = tmp_path / "stubs"
        bin_dir.mkdir()
        make_executable(bin_dir / "pacman", f'#!/bin/sh\necho "pacman $*" >> "{self.log}"\n')
        make_executable(bin_dir / "sudo", '#!/bin/sh\nexec "$@"\n')
        make_executable(bin_dir / "gtk-update-icon-cache", "#!/bin/sh\nexit 0\n")
        make_executable(bin_dir / "python3", f"""#!/bin/sh
case "$1" in
    -c) echo 3.13; exit 0 ;;
    -m) DIR="$4"
        echo "venv $DIR" >> "{self.log}"
        mkdir -p "$DIR/bin"
        printf 'version = 3.13.1\\n' > "$DIR/pyvenv.cfg"
        cat > "$DIR/bin/pip" <<'PIP'
#!/bin/sh
echo "CMAKE_ARGS=[$CMAKE_ARGS] pip $*" >> "{self.log}"
case "$1" in
    show) [ -f "{self.state}" ] ;;
    install) case "$*" in *"-e ."*|*"llama-cpp-python"*) touch "{self.state}" ;; esac ;;
esac
PIP
        chmod +x "$DIR/bin/pip" ;;
esac
""")
        self.env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(self.home)}
        self.env.pop("CMAKE_ARGS", None)

    def run(self, *arguments):
        result = subprocess.run(
            ["bash", str(self.root / "scripts" / "install-arch.sh"), *arguments],
            env=self.env, capture_output=True, text=True, cwd=self.root,
        )
        return result, self.log.read_text(encoding="utf-8").splitlines()


@needs_bash
@not_root
def test_the_cpu_installer_installs_the_system_packages_without_cuda_and_the_app_with_a_cpu_build(tmp_path):
    checkout = Checkout(tmp_path)
    result, calls = checkout.run()
    assert result.returncode == 0, result.stderr

    pacman = next(call for call in calls if call.startswith("pacman"))
    for package in ("python-pip", "python-gobject", "gtk4", "libadwaita", "librsvg", "adwaita-icon-theme",
                    "tesseract-data-osd", "gcc", "cmake"):
        assert re.search(rf"\b{package}\b", pacman), package
    assert not re.search(r"\bcuda\b", pacman)
    assert calls[-1] == "CMAKE_ARGS=[] pip install -e . -q"
    assert not any("force-reinstall" in call for call in calls)
    assert (checkout.root / ".venv" / "karamvt-variant").read_text().strip() == "cpu"


@needs_bash
@not_root
def test_the_cuda_installer_adds_the_cuda_package_and_builds_llama_cpp_with_cuda(tmp_path):
    checkout = Checkout(tmp_path)
    result, calls = checkout.run("--cuda")
    assert result.returncode == 0, result.stderr

    assert re.search(r"\bcuda\b", next(call for call in calls if call.startswith("pacman")))
    assert calls[-1] == "CMAKE_ARGS=[-DGGML_CUDA=on] pip install -e . -q"
    assert (checkout.root / ".venv" / "karamvt-variant").read_text().strip() == "cuda"


@needs_bash
@not_root
def test_switching_an_existing_environment_to_cuda_rebuilds_llama_cpp_once(tmp_path):
    checkout = Checkout(tmp_path)
    checkout.run()                                              # A CPU install first.
    checkout.log.write_text("")

    _result, calls = checkout.run("--cuda")
    assert "CMAKE_ARGS=[-DGGML_CUDA=on] pip install --force-reinstall --no-cache-dir --no-deps -q llama-cpp-python" in calls

    checkout.log.write_text("")
    _result, calls = checkout.run("--cuda")                     # Already CUDA: no second compile.
    assert not any("force-reinstall" in call for call in calls)

    checkout.log.write_text("")
    _result, calls = checkout.run()                             # ...and back to the CPU build.
    assert "CMAKE_ARGS=[] pip install --force-reinstall --no-cache-dir --no-deps -q llama-cpp-python" in calls
    assert (checkout.root / ".venv" / "karamvt-variant").read_text().strip() == "cpu"


@needs_bash
@not_root
def test_the_installer_registers_the_launcher_with_an_absolute_path(tmp_path):
    checkout = Checkout(tmp_path)
    checkout.run()
    entry = (checkout.home / ".local" / "share" / "applications" / f"{APP_ID}.desktop").read_text()
    assert f"Exec={checkout.root}/.venv/bin/karamvt-gui" in entry
    assert (checkout.home / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg").is_file()


@needs_bash
@not_root
def test_an_environment_from_an_older_python_is_started_over(tmp_path):
    checkout = Checkout(tmp_path)
    (checkout.root / ".venv").mkdir()
    (checkout.root / ".venv" / "pyvenv.cfg").write_text("version = 3.12.4\n")
    _result, calls = checkout.run()
    assert any(call.startswith("venv ") for call in calls)


@needs_bash
def test_an_unknown_option_is_refused_and_help_needs_no_pacman(tmp_path):
    script = ROOT / "scripts" / "install-arch.sh"
    refused = subprocess.run(["bash", str(script), "--nope"], capture_output=True, text=True)
    assert refused.returncode != 0 and "Unknown option" in refused.stderr
    helped = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True)
    assert helped.returncode == 0 and "--cuda" in helped.stdout


# -- packaging/build-arch.sh, with makepkg stood in for ----------------------------------------------------

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
    (project / ".git").mkdir()                                  # Must be left out of the tarball.
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
    assert all(name.startswith(f"KaramVT-{version}") for name in names)         # One top-level folder.
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


# -- Windows ------------------------------------------------------------------------------------

def test_the_windows_icon_and_installer_files_exist():
    assert (ROOT / "data" / "windows" / "karamvt.ico").is_file()
    assert (WINDOWS / "karamvt.iss").is_file()
    for script in POWERSHELL_SCRIPTS:
        assert script.is_file(), script


def test_the_installer_script_matches_the_package_version_and_app_id():
    text = (WINDOWS / "karamvt.iss").read_text(encoding="utf-8")
    assert f'#define AppVersion "{karamvt.__version__}"' in text
    assert f'#define AppId "{APP_ID}"' in text
    assert karamvt.APP_ID == APP_ID


def test_the_installer_runs_the_install_script_per_user_and_cleans_up_after_itself():
    text = (WINDOWS / "karamvt.iss").read_text(encoding="utf-8")
    assert "PrivilegesRequired=lowest" in text
    assert "scripts\\install-windows.ps1" in text and "-NoShortcut" in text
    assert "{localappdata}\\karamvt\\gtk" in text and "{app}\\.venv" in text
    assert "-m karamvt.gui" in text
    deletions = [
        line.lower() for line in text.split("[UninstallDelete]")[1].split("[Code]")[0].splitlines()
        if line.strip() and not line.startswith(";")
    ]
    for kept in ("models", "tessdata", "resume", "history", "config", "state"):
        assert not any(kept in line for line in deletions), kept          # User data is never deleted.


def test_pygobject_is_not_requested_from_pypi_on_windows():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "\"PyGObject; sys_platform != 'win32'\"" in text


def test_the_install_script_follows_the_runtime_that_the_application_looks_for():
    text = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    portable = (ROOT / "scripts" / "gtk-portable.ps1").read_text(encoding="utf-8")      # Where Test-GtkRuntime lives.
    assert "'karamvt'" in text and "'gtk'" in text            # %LOCALAPPDATA%\karamvt\gtk, see winsupport.
    assert "KARAMVT_GTK_DIR" in text and "-m karamvt.gui" in text
    assert "Adw-1.typelib" in portable
    assert "*adwaita-1-*.dll" in portable                     # The DLL is adwaita-1-0.dll, not libadwaita-...
    assert "UB-Mannheim.TesseractOCR" in text                 # The OCR program is installed with the rest.


@pytest.mark.parametrize("name", ["install-windows.ps1", "uninstall-windows.ps1"])
def test_the_windows_scripts_refuse_to_run_elsewhere_with_a_clear_message(name):
    text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
    assert "$env:OS -ne 'Windows_NT'" in text and "install-debian.sh" in text
    # The check has to come before anything that needs Windows.
    assert text.index("Windows_NT") < text.index("$env:LOCALAPPDATA")


@needs_pwsh
@pytest.mark.parametrize("script", POWERSHELL_SCRIPTS, ids=lambda path: path.name)
def test_every_powershell_script_parses(script, tmp_path):
    checker = tmp_path / "parse.ps1"
    checker.write_text(
        "param($Path)\n"
        "$errors = $null; $tokens = $null\n"
        "[System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors) | Out-Null\n"
        "if ($errors.Count) { $errors | ForEach-Object { Write-Output $_.Message }; exit 1 }\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(checker), str(script)], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout


def test_the_windows_icon_ships_with_the_sources():
    from karamvt import winsupport

    icon = winsupport.icon_file()
    assert icon is not None and icon.name == "karamvt.ico"
    assert icon.read_bytes()[:4] == b"\x00\x00\x01\x00"      # An ICO header with at least one image.

def test_the_windows_installer_carries_a_portable_tesseract_when_one_was_prepared():
    text = (WINDOWS / "karamvt.iss").read_text(encoding="utf-8")
    assert "#if DirExists(TesseractFolder)" in text and 'DestDir: "{app}\\tesseract"' in text
    assert 'tesseract-portable.ps1"; DestDir: "{app}\\scripts"' in text                # The install script needs it.
    assert "\\tesseract\"" in text.split("[UninstallDelete]")[1]                         # The fetched copy is removed.


def test_the_portable_tesseract_is_pinned_and_checked():
    text = (ROOT / "scripts" / "tesseract-portable.ps1").read_text(encoding="utf-8")
    assert "$script:TesseractVersion = '5.4.0.20240606'" in text
    assert "github.com/UB-Mannheim/tesseract/releases/download/v$($script:TesseractVersion)/" in text
    assert "C885FFF6998E0608BA4BB8AB51436E1C6775C2BAFC2559A19B423E18678B60C9" in text      # Published by winget.
    assert "Get-FileHash" in text and "does not match the published SHA256" in text
    for needed in ("tesseract.exe", "libtesseract-5.dll", "libleptonica-6.dll", "libstdc++-6.dll", "libgcc_s_seh-1.dll"):
        assert f"'{needed}'" in text, needed
    for data in ("eng.traineddata", "osd.traineddata", "pdf.ttf", "configs", "tessconfigs"):
        assert f"'{data}'" in text, data
    assert "'doc'" in text                                      # The license notices travel with the copy.


def test_the_scripts_use_the_portable_tesseract_before_winget():
    install = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    assert "tesseract-portable.ps1" in install
    assert install.index("Get-PortableTesseract") < install.index("winget install --id UB-Mannheim.TesseractOCR")
    assert "'tesseract\\tesseract.exe'" in install                                        # Found beside the program.
    build = (ROOT / "packaging" / "build-windows.ps1").read_text(encoding="utf-8")
    assert "NoTesseract" in build and "Get-PortableTesseract" in build
    assert "/packaging/tesseract/" in (ROOT / ".gitignore").read_text(encoding="utf-8")



# -- the GTK runtime inside the Windows installer ------------------------------------------------------

def test_the_windows_installer_carries_the_gtk_runtime_when_one_was_prepared():
    text = (WINDOWS / "karamvt.iss").read_text(encoding="utf-8")
    assert "#if DirExists(GtkFolder)" in text and 'DestDir: "{app}\\gtk"' in text
    assert 'gtk-portable.ps1"; DestDir: "{app}\\scripts"' in text                       # The install script needs it.
    # An upgrade replaces the runtime as a whole, so no file of an older one stays beside the new DLLs ...
    assert 'Type: filesandordirs; Name: "{app}\\gtk"' in text.split("[InstallDelete]")[1].split("[Icons]")[0]
    # ... and the uninstaller removes it, with the one an older installer downloaded.
    removed = text.split("[UninstallDelete]")[1].split("[Code]")[0]
    assert '"{app}\\gtk"' in removed and '"{localappdata}\\karamvt\\gtk"' in removed


def test_the_build_prepares_the_gtk_runtime_afresh_and_can_pin_it():
    build = (ROOT / "packaging" / "build-windows.ps1").read_text(encoding="utf-8")
    assert "Get-PortableGtk" in build and "gtk-portable.ps1" in build
    for option in ("NoGtk", "GtkTag", "GtkSha256", "GtkZip"):
        assert f"${option}" in build, option
    assert build.index("Remove-Item $gtkBundle") < build.index("Get-PortableGtk -Target")      # Never an old runtime.
    assert "/packaging/gtk/" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_the_install_script_uses_the_runtime_inside_the_installer_before_downloading_one():
    install = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    assert "gtk-portable.ps1" in install and "Resolve-GtkChoice" in install
    assert "function Test-GtkRuntime" not in install              # One definition, in gtk-portable.ps1.
    assert install.index("Resolve-GtkChoice -Explicit") < install.index("Get-PortableGtk -Target $GtkDir")
    assert "$CustomGtk   = $gtkChoice.Custom" in install          # The bundled runtime is found by itself: nothing to save.


def test_the_gtk_download_is_checked_before_it_is_used():
    text = (ROOT / "scripts" / "gtk-portable.ps1").read_text(encoding="utf-8")
    assert "github.com/wingtk/gvsbuild/releases/download/$resolved/$asset" in text
    assert "Get-FileHash" in text and "does not match the expected SHA256" in text
    assert "digest" in text and "gvsbuild-release.txt" in text


GTK_FILES = [
    "bin/gtk-4-1.dll",
    "bin/adwaita-1-0.dll",
    "lib/girepository-1.0/Gtk-4.0.typelib",
    "lib/girepository-1.0/Adw-1.typelib",
    "wheels/PyGObject-3.54.0-cp314-cp314-win_amd64.whl",
    "wheels/pycairo-1.28.0-cp314-cp314-win_amd64.whl",
    "share/glib-2.0/schemas/gschemas.compiled",
]


def make_gtk_zip(path: Path, prefix: str = "", skip: tuple[str, ...] = ()) -> Path:
    """A stand-in for GTK4_Gvsbuild_<tag>_x64.zip: the files the install script looks for, with nothing real in them."""
    with zipfile.ZipFile(path, "w") as archive:
        for name in GTK_FILES:
            if name not in skip:
                archive.writestr(prefix + name, b"x")
    return path


def run_gtk_script(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    script = tmp_path / "case.ps1"
    script.write_text(f". '{ROOT / 'scripts' / 'gtk-portable.ps1'}'\n{body}\n", encoding="utf-8")
    return subprocess.run(["pwsh", "-NoProfile", "-File", str(script)], capture_output=True, text=True, check=False)


@needs_pwsh
@pytest.mark.parametrize("prefix", ["", "GTK4/"], ids=["at the top", "inside one folder"])
def test_a_gtk_zip_becomes_a_complete_runtime_folder(tmp_path, prefix):
    zip_path = make_gtk_zip(tmp_path / "GTK4_Gvsbuild_2026.8.0_x64.zip", prefix)
    target = tmp_path / "out" / "gtk"
    result = run_gtk_script(tmp_path, f"$ok = Get-PortableGtk -Target '{target}' -ZipPath '{zip_path}'\nWrite-Output \"RESULT=$ok\"")
    assert "RESULT=True" in result.stdout, result.stdout + result.stderr
    for name in GTK_FILES:
        assert (target / name).is_file(), name
    note = (target / "gvsbuild-release.txt").read_text(encoding="utf-8-sig")
    assert "GTK4_Gvsbuild_2026.8.0_x64.zip" in note and "SHA256 of the zip:" in note
    assert not (tmp_path / "out" / "gtk.partial").exists()


@needs_pwsh
def test_an_incomplete_gtk_zip_is_refused_and_leaves_nothing_behind(tmp_path):
    zip_path = make_gtk_zip(tmp_path / "gtk.zip", skip=("lib/girepository-1.0/Adw-1.typelib",))
    target = tmp_path / "out" / "gtk"
    result = run_gtk_script(tmp_path, f"$ok = Get-PortableGtk -Target '{target}' -ZipPath '{zip_path}'\nWrite-Output \"RESULT=$ok\"")
    assert "RESULT=False" in result.stdout, result.stdout + result.stderr
    assert "not a complete GTK 4" in result.stdout
    assert not target.exists() and not (tmp_path / "out" / "gtk.partial").exists()


@needs_pwsh
def test_a_gtk_zip_must_match_the_hash_it_is_pinned_to(tmp_path):
    import hashlib

    zip_path = make_gtk_zip(tmp_path / "gtk.zip")
    right = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    wrong = ("0" if right[0] != "0" else "1") + right[1:]
    bad = run_gtk_script(tmp_path, f"$ok = Get-PortableGtk -Target '{tmp_path / 'bad'}' -ZipPath '{zip_path}' -Sha256 '{wrong}'\nWrite-Output \"RESULT=$ok\"")
    assert "RESULT=False" in bad.stdout and "does not match the expected SHA256" in bad.stdout, bad.stdout + bad.stderr
    assert not (tmp_path / "bad").exists()
    good = run_gtk_script(tmp_path, f"$ok = Get-PortableGtk -Target '{tmp_path / 'good'}' -ZipPath '{zip_path}' -Sha256 '{right.upper()}'\nWrite-Output \"RESULT=$ok\"")
    assert "RESULT=True" in good.stdout, good.stdout + good.stderr
    assert right.upper() in (tmp_path / "good" / "gvsbuild-release.txt").read_text(encoding="utf-8-sig").upper()


@needs_pwsh
def test_a_complete_runtime_that_is_already_there_is_left_alone(tmp_path):
    target = tmp_path / "gtk"
    for name in GTK_FILES:
        (target / name).parent.mkdir(parents=True, exist_ok=True)
        (target / name).write_bytes(b"mine")
    result = run_gtk_script(tmp_path, f"$ok = Get-PortableGtk -Target '{target}' -ZipPath '{tmp_path / 'does-not-exist.zip'}'\nWrite-Output \"RESULT=$ok\"")
    assert "RESULT=True" in result.stdout, result.stdout + result.stderr
    assert (target / GTK_FILES[0]).read_bytes() == b"mine"


def make_runtime_folder(folder: Path, complete: bool = True) -> Path:
    for name in GTK_FILES:
        if complete or "Adw-1" not in name:
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(b"x")
    return folder


@needs_pwsh
@pytest.mark.parametrize(
    "explicit, bundled, expected_dir, custom, is_bundled",
    [
        ("mine", "complete", "mine", True, False),               # -GtkDir wins, even over a bundled runtime.
        ("default", "complete", "default", False, False),         # ... and naming the default folder is not "custom".
        (None, "complete", "app/gtk", False, True),                # The runtime inside the installer, found by itself.
        (None, "incomplete", "default", False, False),             # A damaged one is not used: the default folder (a download).
        (None, None, "default", False, False),                     # A checkout has none.
    ],
    ids=["explicit", "explicit default", "bundled", "damaged bundle", "none"],
)
def test_which_gtk_runtime_the_install_script_uses(tmp_path, explicit, bundled, expected_dir, custom, is_bundled):
    folders = {"mine": make_runtime_folder(tmp_path / "mine"), "default": tmp_path / "default"}
    folders["default"].mkdir()
    app = tmp_path / "app"
    app.mkdir()
    if bundled:
        make_runtime_folder(app / "gtk", complete=bundled == "complete")
    explicit_path = str(folders[explicit]) if explicit else ""
    body = (
        f"$c = Resolve-GtkChoice -Explicit '{explicit_path}' -ProjectRoot '{app}' -DefaultDir '{folders['default']}'\n"
        "Write-Output \"DIR=$($c.Dir)\"\nWrite-Output \"CUSTOM=$($c.Custom)\"\nWrite-Output \"BUNDLED=$($c.Bundled)\""
    )
    result = run_gtk_script(tmp_path, body)
    lines = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert result.returncode == 0, result.stdout + result.stderr
    assert Path(lines["DIR"]) == (tmp_path / expected_dir if expected_dir != "app/gtk" else app / "gtk")
    assert lines["CUSTOM"] == str(custom) and lines["BUNDLED"] == str(is_bundled)


# -- which gvsbuild release, and what a failed installation leaves behind ---------------------------------

def test_the_gtk_release_is_a_tested_one_and_not_the_newest():
    portable = (ROOT / "scripts" / "gtk-portable.ps1").read_text(encoding="utf-8")
    # gvsbuild 2026.10.0 builds its PyGObject wheels for Python 3.15; 2026.8.0 has them for 3.14, which is what CI uses.
    assert "$script:GtkDefaultTag = '2026.8.0'" in portable
    assert "python-version: \"3.14\"" in (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    install = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    assert "Get-GtkZipUrls" not in install and "releases/latest" not in install        # No "newest" lookup of its own.
    assert "[string]$GtkTag" in install and "-Tag $GtkTag" in install
    build = (ROOT / "packaging" / "build-windows.ps1").read_text(encoding="utf-8")
    assert "[string]$GtkTag = ''" in build


@needs_pwsh
@pytest.mark.parametrize("given, expected", [("", "2026.8.0"), ("2026.10.0", "2026.10.0"), ("2026.6.0", "2026.6.0")])
def test_the_gtk_release_is_the_tested_one_unless_another_is_named(tmp_path, given, expected):
    result = run_gtk_script(tmp_path, f"Write-Output \"TAG=$(Resolve-GtkTag '{given}')\"")
    assert f"TAG={expected}" in result.stdout, result.stdout + result.stderr


def test_a_wheel_mismatch_says_which_python_the_wheels_are_for():
    install = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    assert "are for Python $($available -join ', ')" in install and "-GtkTag" in install


def test_the_installer_keeps_a_log_of_the_install_script_and_points_to_it():
    install = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    assert "[string]$LogFile" in install and "Start-Transcript" in install
    iss = (WINDOWS / "karamvt.iss").read_text(encoding="utf-8")
    assert "-LogFile" in iss and "karamvt\\install.log" in iss
    assert iss.count("install.log") >= 2                                              # Passed to the script, named in the error.


def test_the_manual_installer_job_cannot_hang_and_shows_the_logs():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = ci.split("windows-installer-run:")[1].split("windows-full-install:")[0]
    assert job.count("run-installer.ps1") == 2 and job.count("timeout-minutes: 35") == 2
    script = (WINDOWS / "run-installer.ps1").read_text(encoding="utf-8")
    assert "WaitForExit($TimeoutMinutes * 60 * 1000)" in script and "taskkill" in script
    assert script.index("finally") < script.index("Get-Content $scriptLog")           # The logs show on failure too.


def test_arabic_letters_in_the_paths_are_checked_in_the_full_install_job_without_a_red_report():
    """Tesseract's own limit is only reported (green); what KaramVT does about it is counted."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = ci.split("windows-full-install:")[1]
    report = job.split("- name: Tesseract on its own, with Arabic letters in each path")[1].split("- name:")[0]
    counted = job.split("- name: What KaramVT does about Arabic letters in the paths")[1].split("- name:")[0]
    assert "throw" not in report and "continue-on-error" not in report              # A report, so never red.
    assert "if ($raw)" in report                                                      # An empty output is not an error.
    assert "throw" in counted and "ocr_arabic_smoke.py" in counted                    # What the program relies on is counted.
    assert "$env:TEMP" in counted and "$env:XDG_DATA_HOME" in counted                 # TEMP and the language folder are Arabic.
    # The checks that follow still run when this one fails.
    suite = job.split("- name: Test suite with the real libraries installed")[1].split("- name:")[0]
    assert "steps.install.outcome == 'success'" in suite


def test_ocr_reaches_pytesseract_only_through_the_helpers_that_know_about_ascii_paths():
    ocr_text = (ROOT / "src" / "karamvt" / "core" / "ocr.py").read_text(encoding="utf-8")
    assert ocr_text.count("pytesseract_module.image_to_") == 3          # One in each of _image_to_string/_data/_osd.
    assert "pytesseract.image_to_" not in ocr_text.replace("pytesseract_module.image_to_", "")


def test_a_failing_native_command_is_never_the_exit_code_of_a_step_that_otherwise_passed():
    """Actions ends a pwsh step with the exit code of the last native command: taskkill fails when part of the process
    tree is already gone, and Tesseract fails on a path it cannot use. Both are results here, not failures."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    launch = ci.split("- name: Launch the application")[1].split("- uses: actions/upload-artifact")[0]
    assert "$global:LASTEXITCODE = 0" in launch.split('cmd /c "taskkill')[1].split("\n")[0]
    checker = (WINDOWS / "check-installed.ps1").read_text(encoding="utf-8")
    assert "$global:LASTEXITCODE = 0" in checker.split('cmd /c "taskkill')[1].split("\n")[0]
    job = ci.split("windows-full-install:")[1]
    for name in ("Tesseract on its own, with Arabic letters in each path", "What KaramVT does about Arabic letters in the paths"):
        step = job.split(f"- name: {name}")[1].split("- name:")[0]
        assert "$global:LASTEXITCODE = 0" in step.split("function Run-Case")[1].split("return $text")[0]
    # The console of a runner is cp1252: the script that prints Arabic paths must not stop on them.
    smoke = job.split("What KaramVT does about Arabic letters in the paths")[1].split("- name:")[0]
    assert 'sys.stdout.reconfigure(errors="backslashreplace")' in smoke


# -- Python's installer and the wheelhouse inside the installer ----------------------------------------------------

BUNDLE = ROOT / "scripts" / "python-bundle.ps1"


def run_bundle_script(tmp_path: Path, body: str, env: dict | None = None) -> subprocess.CompletedProcess:
    script = tmp_path / "bundle-case.ps1"
    # Strict mode like the scripts that use python-bundle.ps1 (an unset variable is an error there, not an empty value).
    script.write_text(f"Set-StrictMode -Version 2.0\n$ErrorActionPreference = 'Stop'\n. '{BUNDLE}'\n{body}\n", encoding="utf-8")
    return subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(script)], capture_output=True, text=True, check=False,
        env={**os.environ, **(env or {})},
    )


# The files these tests make are not signed, and on Windows the signature is checked for real (its own tests below).
NO_SIGNATURE_CHECK = "function Assert-PythonInstallerSignature([string]$File) { }\n"


def make_wheel(folder: Path, name: str, version: str = "1.0", tag: str = "py3-none-any") -> Path:
    """A real (empty) wheel that pip accepts: just its metadata."""
    folder.mkdir(parents=True, exist_ok=True)
    dist = f"{name.replace('-', '_')}-{version}"
    path = folder / f"{dist}-{tag}.whl"
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr(f"{dist}.dist-info/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        wheel.writestr(f"{dist}.dist-info/WHEEL", f"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: {tag}\n")
        wheel.writestr(f"{dist}.dist-info/RECORD", "")
    return path


def this_pythons_tag() -> tuple[str, str]:
    """("3.12", "cp312-cp312-linux_x86_64"): a wheel tag that pip accepts for the Python running the tests."""
    major, minor = sys.version_info[:2]
    platform = sysconfig.get_platform().replace("-", "_").replace(".", "_")
    return f"{major}.{minor}", f"cp{major}{minor}-cp{major}{minor}-{platform}"


def make_wheelhouse_folder(folder: Path, minor: str = "3.14", skip: tuple[str, ...] = (), marker: str | None = None) -> Path:
    """A folder that looks like a built wheelhouse; the marker says for which Python (the default: *minor*)."""
    folder.mkdir(parents=True, exist_ok=True)
    tag = f"cp{minor.replace('.', '')}"
    for name, wheel_tag in (("llama_cpp_python", f"{tag}-{tag}-win_amd64"), ("faster_whisper", "py3-none-any"),
                            ("setuptools", "py3-none-any"), ("wheel", "py3-none-any"), ("langdetect", "py3-none-any")):
        if name not in skip:
            (folder / f"{name}-1.0-{wheel_tag}.whl").write_bytes(b"x")
    (folder / "for-python.txt").write_text((marker or minor) + "\n", encoding="ascii")
    return folder


# -- asking a Python which version it is ---------------------------------------------------------------------------

def test_no_python_snippet_handed_to_a_program_has_a_double_quote():
    """Windows PowerShell 5.1 (it runs the install script) drops the double quotes of an argument that it hands to a
    program: `python -c 'print("%d.%d" % ...)'` was a SyntaxError there, so the environment of the same Python was
    thought to be another one and was recreated on every run, and a Python on PATH was never found."""
    scripts = sorted((ROOT / "scripts").glob("*.ps1")) + sorted((ROOT / "packaging").rglob("*.ps1"))
    assert scripts
    for script in scripts:
        text = script.read_text(encoding="utf-8")
        for snippet in re.findall(r"'-c',\s*'([^']*)'", text):
            assert '"' not in snippet, f"{script.name}: {snippet}"
    assert '"' not in re.search(r"PythonMinorSnippet = '([^']*)'", BUNDLE.read_text(encoding="utf-8")).group(1)


def test_the_version_checks_use_the_one_snippet_and_it_prints_what_they_compare_with():
    install = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    assert install.count("@('-c', $script:PythonMinorSnippet)") == 2                    # The Python on PATH, and the existing environment.
    assert BUNDLE.read_text(encoding="utf-8").count("@('-c', $script:PythonMinorSnippet)") == 2     # The Python that builds the wheelhouse.
    snippet = re.search(r"PythonMinorSnippet = '([^']*)'", BUNDLE.read_text(encoding="utf-8")).group(1)
    run = subprocess.run([sys.executable, "-c", snippet], capture_output=True, text=True, check=True)
    assert run.stdout.strip() == f"{sys.version_info[0]}.{sys.version_info[1]}"          # "3.14", the form the install script compares.


@needs_pwsh
def test_the_python_installer_of_the_right_version_is_found(tmp_path):
    for name in ("python-3.14.9-amd64.exe", "python-3.15.0-amd64.exe", "python-3.1.5-amd64.exe", "python-3.14.9-arm64.exe", "notes.txt"):
        (tmp_path / "python" / name).parent.mkdir(exist_ok=True)
        (tmp_path / "python" / name).write_bytes(b"x")
    found = run_bundle_script(tmp_path, f"Write-Output (\"FOUND=\" + (Find-PythonInstaller '{tmp_path / 'python'}' '3.14'))")
    assert f"FOUND={tmp_path / 'python' / 'python-3.14.9-amd64.exe'}" in found.stdout, found.stdout + found.stderr
    none = run_bundle_script(tmp_path, f"Write-Output (\"FOUND=[\" + (Find-PythonInstaller '{tmp_path / 'python'}' '3.13') + \"]\")")
    assert "FOUND=[]" in none.stdout, none.stdout + none.stderr
    nothing = run_bundle_script(tmp_path, f"Write-Output (\"FOUND=[\" + (Find-PythonInstaller '{tmp_path / 'nowhere'}' '3.14') + \"]\")")
    assert "FOUND=[]" in nothing.stdout, nothing.stdout + nothing.stderr


@needs_pwsh
def test_a_python_installer_is_copied_under_its_own_name_and_replaces_an_older_one(tmp_path):
    import hashlib

    installer = tmp_path / "somewhere" / "installer.exe"
    installer.parent.mkdir()
    installer.write_bytes(b"MZ python installer")
    target = tmp_path / "out" / "python"
    target.mkdir(parents=True)
    (target / "python-3.14.1-amd64.exe").write_bytes(b"old")
    result = run_bundle_script(tmp_path, NO_SIGNATURE_CHECK + f"$ok = Get-PythonInstaller -Target '{target}' -Version 3.14.9 -InstallerPath '{installer}'\nWrite-Output \"RESULT=$ok\"")
    assert "RESULT=True" in result.stdout, result.stdout + result.stderr
    assert sorted(path.name for path in target.iterdir()) == ["python-3.14.9-amd64.exe"]
    assert (target / "python-3.14.9-amd64.exe").read_bytes() == b"MZ python installer"
    assert hashlib.sha256(b"MZ python installer").hexdigest().upper() in result.stdout       # The hash that was seen is printed.


@needs_pwsh
def test_a_python_installer_must_match_the_hash_it_is_pinned_to(tmp_path):
    import hashlib

    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"MZ python installer")
    right = hashlib.sha256(installer.read_bytes()).hexdigest()
    wrong = ("0" if right[0] != "0" else "1") + right[1:]
    bad = run_bundle_script(tmp_path, NO_SIGNATURE_CHECK + f"$ok = Get-PythonInstaller -Target '{tmp_path / 'bad'}' -Version 3.14.9 -InstallerPath '{installer}' -Sha256 '{wrong}'\nWrite-Output \"RESULT=$ok\"")
    assert "RESULT=False" in bad.stdout and "does not match the expected SHA256" in bad.stdout, bad.stdout + bad.stderr
    assert not (tmp_path / "bad").exists()
    good = run_bundle_script(tmp_path, NO_SIGNATURE_CHECK + f"$ok = Get-PythonInstaller -Target '{tmp_path / 'good'}' -Version 3.14.9 -InstallerPath '{installer}' -Sha256 '{right.upper()}'\nWrite-Output \"RESULT=$ok\"")
    assert "RESULT=True" in good.stdout, good.stdout + good.stderr
    missing = run_bundle_script(tmp_path, NO_SIGNATURE_CHECK + f"$ok = Get-PythonInstaller -Target '{tmp_path / 'm'}' -Version 3.14.9 -InstallerPath '{tmp_path / 'none.exe'}'\nWrite-Output \"RESULT=$ok\"")
    assert "RESULT=False" in missing.stdout and "does not exist" in missing.stdout
    reason = run_bundle_script(tmp_path, NO_SIGNATURE_CHECK + f"$null = Get-PythonInstaller -Target '{tmp_path / 'r'}' -Version 3.14.9 -InstallerPath '{tmp_path / 'none.exe'}'\nWrite-Output \"WHY=$script:BundleProblem\"")
    assert "WHY=The Python installer could not be prepared: " in reason.stdout and "does not exist" in reason.stdout    # The build can say why.


@needs_pwsh
@pytest.mark.skipif(sys.platform != "win32", reason="only Windows can check an Authenticode signature")
def test_a_python_installer_must_be_validly_signed_by_the_python_software_foundation(tmp_path):
    unsigned = tmp_path / "python-3.14.9-amd64.exe"
    unsigned.write_bytes(b"MZ not really an installer")
    refused = run_bundle_script(tmp_path, f"$ok = Get-PythonInstaller -Target '{tmp_path / 'out'}' -Version 3.14.9 -InstallerPath '{unsigned}'\nWrite-Output \"RESULT=$ok\"")
    assert "RESULT=False" in refused.stdout and "signature is not valid" in refused.stdout, refused.stdout + refused.stderr
    assert not (tmp_path / "out").exists()                                             # Nothing is copied from a file that is not trusted.
    # PowerShell's own program is validly signed, by Microsoft: a good signature from the wrong signer is refused too.
    other = run_bundle_script(tmp_path, "try { Assert-PythonInstallerSignature (Get-Process -Id $PID).Path; Write-Output 'RESULT=accepted' } catch { Write-Output \"RESULT=$($_.Exception.Message)\" }")
    assert "RESULT=accepted" not in other.stdout and "RESULT=" in other.stdout, other.stdout + other.stderr
    assert "not signed by the Python Software Foundation" in other.stdout or "signature is not valid" in other.stdout, other.stdout


@needs_pwsh
@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in installer is a POSIX script")
@pytest.mark.parametrize("exit_code, expected", [(0, True), (1, False), (5, False)])      # (3010, "restart advisable", is 194 on a POSIX system.)
def test_python_is_installed_silently_for_the_current_user(tmp_path, exit_code, expected):
    record = tmp_path / "arguments.txt"
    installer = make_executable(tmp_path / "python-3.14.9-amd64.exe", f'#!/bin/sh\nprintf "%s\\n" "$@" > "{record}"\nexit {exit_code}\n')
    result = run_bundle_script(tmp_path, f"$ok = Install-PythonFromInstaller '{installer}' '{tmp_path / 'python.log'}'\nWrite-Output \"RESULT=$ok\"")
    assert f"RESULT={expected}" in result.stdout, result.stdout + result.stderr
    arguments = record.read_text(encoding="utf-8").split()
    for needed in ("/quiet", "InstallAllUsers=0", "PrependPath=0", "Include_launcher=0", "Include_test=0", "Shortcuts=0", "AssociateFiles=0", "/log"):
        assert needed in arguments, arguments
    assert "InstallAllUsers=1" not in arguments                 # Never needs administrator rights.
    assert "@(0, 3010) -contains $process.ExitCode" in BUNDLE.read_text(encoding="utf-8")     # "Installed, a restart is advisable" is a success.


@needs_pwsh
def test_a_wheelhouse_is_complete_only_with_what_the_installation_needs(tmp_path):
    def check(folder, minor=""):
        result = run_bundle_script(tmp_path, f"Write-Output \"RESULT=$(Test-Wheelhouse '{folder}' '{minor}')\"")
        return "RESULT=True" in result.stdout

    assert check(make_wheelhouse_folder(tmp_path / "complete"))
    assert check(tmp_path / "complete", "3.14")
    assert not check(tmp_path / "complete", "3.13")                                  # It was verified for 3.14.
    # What pip accepts for a Python is not guessed from the wheels' names: the wheel's tag does not matter, the marker does.
    assert check(make_wheelhouse_folder(tmp_path / "abi3", marker="3.14"), "3.14")
    assert not check(make_wheelhouse_folder(tmp_path / "unverified", marker="3.13"), "3.14")
    (tmp_path / "unmarked").mkdir()
    for wheel in (tmp_path / "complete").glob("*.whl"):
        (tmp_path / "unmarked" / wheel.name).write_bytes(b"x")
    assert check(tmp_path / "unmarked") and not check(tmp_path / "unmarked", "3.14")     # No marker: not verified for any Python.
    for missing in ("llama_cpp_python", "faster_whisper", "setuptools", "wheel"):
        assert not check(make_wheelhouse_folder(tmp_path / f"no-{missing}", skip=(missing,))), missing


@needs_pwsh
def test_a_wheelhouse_that_cannot_be_used_says_why(tmp_path):
    def why(folder, minor=""):
        return run_bundle_script(tmp_path, f"Write-Output \"WHY=[$(Get-WheelhouseProblem '{folder}' '{minor}')]\"").stdout

    assert "WHY=[]" in why(make_wheelhouse_folder(tmp_path / "ok"), "3.14")
    assert "no llama_cpp_python wheel" in why(make_wheelhouse_folder(tmp_path / "a", skip=("llama_cpp_python",)))
    assert "verified for Python 3.13, not for Python 3.14" in why(make_wheelhouse_folder(tmp_path / "b", marker="3.13"), "3.14")
    assert "there is no folder" in why(tmp_path / "nowhere")


@needs_pwsh
def test_the_python_of_the_gtk_runtime_is_the_newest_its_wheels_are_for(tmp_path):
    wheels = tmp_path / "gtk" / "wheels"
    wheels.mkdir(parents=True)
    for name in ("PyGObject-3.54.0-cp313-cp313-win_amd64.whl", "PyGObject-3.54.0-cp314-cp314-win_amd64.whl", "pycairo-1.28.0-cp315-cp315-win_amd64.whl"):
        (wheels / name).write_bytes(b"x")
    found = run_bundle_script(tmp_path, f"Write-Output \"MINOR=$(Get-GtkPythonMinor '{tmp_path / 'gtk'}')\"")
    assert "MINOR=3.14" in found.stdout, found.stdout + found.stderr                  # pycairo does not count.
    nothing = run_bundle_script(tmp_path, f"Write-Output \"MINOR=[$(Get-GtkPythonMinor '{tmp_path / 'nowhere'}')]\"")
    assert "MINOR=[]" in nothing.stdout


def make_project(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=77", "wheel"]\n'
        '[project]\nname = "demo"\nversion = "1.0"\ndependencies = ["faster-whisper", "llama-cpp-python", "PyGObject; sys_platform != \'win32\' and python_version < \'3\'"]\n',
        encoding="utf-8",
    )
    return folder


@needs_pwsh
@pytest.mark.skipif(sys.version_info < (3, 11), reason="reading pyproject.toml needs tomllib")
def test_the_wheelhouse_is_built_from_what_the_project_asks_for(tmp_path):
    minor, llama_tag = this_pythons_tag()
    index = tmp_path / "index"
    for name in ("faster-whisper", "setuptools", "wheel"):
        make_wheel(index, name, version="80.0" if name == "setuptools" else "1.0")
    make_wheel(index, "llama-cpp-python", tag=llama_tag)
    project = make_project(tmp_path / "project")
    target = tmp_path / "out" / "wheelhouse"
    result = run_bundle_script(
        tmp_path,
        f"$ok = Build-Wheelhouse -Target '{target}' -Python '{sys.executable}' -ProjectRoot '{project}' -ExpectMinor '{minor}' "
        f"-PipArguments '--no-index','--find-links','{index}'\nWrite-Output \"RESULT=$ok\"",
    )
    assert "RESULT=True" in result.stdout, result.stdout + result.stderr
    names = sorted(path.name for path in target.iterdir())
    assert any(name.startswith("llama_cpp_python-") for name in names) and any(name.startswith("setuptools-") for name in names), names
    assert not (tmp_path / "out" / "wheelhouse.partial").exists()
    assert (target / "for-python.txt").read_text(encoding="ascii").strip() == minor        # Verified by pip for this Python.
    assert "Checking that pip can install everything from the wheelhouse alone" in result.stdout


@needs_pwsh
@pytest.mark.skipif(sys.version_info < (3, 11), reason="reading pyproject.toml needs tomllib")
def test_the_wheelhouse_does_not_depend_on_how_the_llama_cpp_wheel_is_tagged(tmp_path):
    """Whether a wheel suits this Python is for pip to say, not for the wheel's file name (an abi3 wheel has no cp tag of its own)."""
    minor, _ = this_pythons_tag()
    platform = sysconfig.get_platform().replace("-", "_").replace(".", "_")
    index = tmp_path / "index"
    for name in ("faster-whisper", "setuptools", "wheel"):
        make_wheel(index, name, version="80.0" if name == "setuptools" else "1.0")
    make_wheel(index, "llama-cpp-python", tag=f"cp38-abi3-{platform}")
    target = tmp_path / "out" / "wheelhouse"
    result = run_bundle_script(
        tmp_path,
        f"$ok = Build-Wheelhouse -Target '{target}' -Python '{sys.executable}' -ProjectRoot '{make_project(tmp_path / 'project')}' -ExpectMinor '{minor}' "
        f"-PipArguments '--no-index','--find-links','{index}'\nWrite-Output \"RESULT=$ok\"",
    )
    assert "RESULT=True" in result.stdout, result.stdout + result.stderr


@needs_pwsh
@pytest.mark.skipif(sys.version_info < (3, 11), reason="reading pyproject.toml needs tomllib")
def test_a_wheelhouse_for_another_python_or_with_a_missing_library_is_refused_and_leaves_nothing(tmp_path):
    minor, llama_tag = this_pythons_tag()
    project = make_project(tmp_path / "project")
    index = tmp_path / "index"
    for name in ("faster-whisper", "setuptools", "wheel"):
        make_wheel(index, name, version="80.0" if name == "setuptools" else "1.0")
    make_wheel(index, "llama-cpp-python", tag=llama_tag)

    wrong = run_bundle_script(
        tmp_path,
        f"$ok = Build-Wheelhouse -Target '{tmp_path / 'wrong'}' -Python '{sys.executable}' -ProjectRoot '{project}' -ExpectMinor '3.0' "
        f"-PipArguments '--no-index','--find-links','{index}'\nWrite-Output \"RESULT=$ok\"",
    )
    assert "RESULT=False" in wrong.stdout and "have to be for Python 3.0" in wrong.stdout, wrong.stdout + wrong.stderr

    (index / next(path.name for path in index.iterdir() if path.name.startswith("faster_whisper"))).unlink()      # pip cannot find it any more.
    missing = run_bundle_script(
        tmp_path,
        f"$ok = Build-Wheelhouse -Target '{tmp_path / 'missing'}' -Python '{sys.executable}' -ProjectRoot '{project}' -ExpectMinor '{minor}' "
        f"-PipArguments '--no-index','--find-links','{index}'\nWrite-Output \"RESULT=$ok\"",
    )
    assert "RESULT=False" in missing.stdout and "pip wheel failed" in missing.stdout, missing.stdout + missing.stderr
    assert "faster-whisper" in missing.stdout.split("pip wheel failed")[1]            # The reason carries what pip complained about.
    assert not (tmp_path / "wrong").exists() and not (tmp_path / "missing").exists() and not (tmp_path / "missing.partial").exists()

    # A llama-cpp-python wheel built for some other Python is no use: pip does not accept it, so there is no wheelhouse.
    other = tmp_path / "other-index"
    for name in ("faster-whisper", "setuptools", "wheel"):
        make_wheel(other, name, version="80.0" if name == "setuptools" else "1.0")
    make_wheel(other, "llama-cpp-python", tag="cp27-cp27m-win_amd64")
    foreign = run_bundle_script(
        tmp_path,
        f"$ok = Build-Wheelhouse -Target '{tmp_path / 'foreign'}' -Python '{sys.executable}' -ProjectRoot '{project}' -ExpectMinor '{minor}' "
        f"-PipArguments '--no-index','--find-links','{other}'\nWrite-Output \"RESULT=$ok\"",
    )
    assert "RESULT=False" in foreign.stdout and "llama-cpp-python" in foreign.stdout and not (tmp_path / "foreign").exists(), foreign.stdout


def test_the_install_script_prefers_what_the_installer_carries_and_falls_back_to_the_internet():
    install = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    assert ". (Join-Path $PSScriptRoot 'python-bundle.ps1')" in install
    # Python: the installer that came along, then winget.
    python_step = install.split("function Install-Python")[1].split("$wheelDir =")[0]
    assert python_step.index("Find-PythonInstaller") < python_step.index("Install-PythonFromInstaller") < python_step.index("winget install")
    assert "installer that came with KaramVT" in python_step
    # The libraries: from the wheelhouse when it is for this Python, with no index; if that fails, downloaded as before.
    assert "$offline = Test-Wheelhouse $wheelhouse $chosen" in install
    assert "if (-not $offline) { Pip @('install', '--upgrade', 'pip', '--quiet') }" in install
    assert install.count("'--no-index', '--find-links', $wheelhouse") == 2          # llama-cpp-python, and the project with the rest.
    assert "installed from the wheelhouse (no internet needed)" in install
    assert "downloading them instead" in install and "Pip @('install', '--quiet', '-e', $ProjectRoot)" in install
    # A particular llama-cpp-python asked for with -Cuda, -BuildLlamaFromSource or -LlamaIndexUrl is never replaced by the bundled one.
    llama = install.split("if ($BuildLlamaFromSource")[1].split("# Tesseract is a program")[0]
    assert "-not $LlamaIndexUrl" in llama and llama.index("Install-LlamaFromSource") < llama.index("--no-index")


def test_the_two_places_that_name_the_llama_cpp_wheel_index_agree():
    index = re.search(r"CpuLlamaIndex = '([^']+)'", (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8"))
    bundled = re.search(r"CpuLlamaIndex = '([^']+)'", BUNDLE.read_text(encoding="utf-8"))
    assert index and bundled and index.group(1) == bundled.group(1)


def test_the_build_prepares_python_and_its_libraries_for_the_python_of_the_gtk_runtime():
    build = (ROOT / "packaging" / "build-windows.ps1").read_text(encoding="utf-8")
    assert ". (Join-Path $ProjectRoot 'scripts\\python-bundle.ps1')" in build
    assert "[switch]$NoPython" in build and "[string]$PythonExe" in build and "[string]$PythonInstaller" in build
    assert "[switch]$RequirePython" in build and 'throw "-RequirePython: $Message"' in build and build.count("Skip-PythonPart ") >= 4
    assert build.count("$script:BundleProblem") == 2                                    # Both the installer's and the libraries' reasons.
    assert build.index("Get-PortableGtk -Target") < build.index("Get-GtkPythonMinor") < build.index("Get-PythonInstaller -Target") < build.index("ISCC")
    assert "-ExpectMinor $minor" in build                                              # The wheels are for the runtime's Python.
    assert "platform.python_version()" in build                                        # The installer is the build Python's own version.
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/packaging/python/" in ignore and "/packaging/wheelhouse/" in ignore


def test_the_inno_script_carries_python_and_the_wheelhouse_only_when_the_build_prepared_them():
    iss = (WINDOWS / "karamvt.iss").read_text(encoding="utf-8")
    assert 'Source: "..\\..\\scripts\\python-bundle.ps1"; DestDir: "{app}\\scripts"' in iss
    for folder, define in (("python", "PythonFolder"), ("wheelhouse", "WheelFolder")):
        assert f'#define {define} SourcePath + "\\..\\{folder}"' in iss
        assert f'#if DirExists({define})\nSource: "{{#{define}}}\\*"; DestDir: "{{app}}\\{folder}"; Flags: recursesubdirs ignoreversion\n#endif' in iss
        assert f'Type: filesandordirs; Name: "{{app}}\\{folder}"' in iss                  # Replaced on an upgrade, removed on uninstall.
    assert iss.count('Name: "{app}\\python"') == 2 and iss.count('Name: "{app}\\wheelhouse"') == 2
    assert "[InstallDelete]\n#if DirExists(GtkFolder)" in iss                           # The section exists whichever folders do.


def test_the_installation_check_knows_what_the_install_script_says_when_it_used_the_bundled_parts():
    check = (WINDOWS / "check-installed.ps1").read_text(encoding="utf-8")
    install = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    for marker in ("Python libraries installed from the wheelhouse", "installer that came with KaramVT"):
        assert marker in install and marker in check, marker


def test_ci_checks_that_the_installer_carries_python_and_that_the_first_install_has_no_other_python():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    installer_job = ci.split("  installer:")[1].split("  windows-installer-run:")[0]
    # The libraries are collected with the Python of the GTK runtime, so the job that builds the installer has it.
    assert "actions/setup-python@v5" in installer_job and 'python-version: "3.14"' in installer_job
    carries = installer_job.split("- name: The installer carries the Python installer and the Python libraries")[1].split("- uses:")[0]
    assert "Find-PythonInstaller" in carries and "Test-Wheelhouse 'packaging\\wheelhouse'" in carries and "throw" in carries

    run_job = ci.split("  windows-installer-run:")[1].split("  windows-full-install:")[0]
    first = run_job.split("- name: Run the installer (silently, as a user would, on a machine")[1].split("- name:")[0]
    assert "hostedtoolcache" in first and "PythonCore" in first                       # Neither the PATH nor the registry finds Python.
    assert first.index("PythonCore") < first.index("run-installer.ps1 -Label first")   # Taken away before the installer runs.
    assert "check-installed.ps1 -ExpectBundledPython" in run_job
    upgrade = run_job.split("- name: Check the upgraded installation")[1]
    assert "-ExpectBundledPython" not in upgrade                                       # The upgrade finds Python already there.
    # ... and keeps the Python environment of the first installation instead of making another one.
    assert "'recreating it'" in upgrade and upgrade.index("'recreating it'") < upgrade.index("check-installed.ps1")


def test_the_installers_built_in_ci_must_carry_python_or_the_build_says_why():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert ci.count("run: ./packaging/build-windows.ps1 -RequirePython -RequireVcRuntime\n") == 2   # The installer job and windows-installer-run:
    # an installer that quietly lacks Python, its libraries or the Visual C++ runtime would only fail much later.
    assert "run: ./packaging/build-windows.ps1\n" not in ci


def test_the_installation_check_asks_the_installed_program_what_ocr_settings_would_show():
    checker = (WINDOWS / "check-installed.ps1").read_text(encoding="utf-8")
    section = checker.split("# 4b.")[1].split("# 5. The application starts")[0]
    assert "karamvt.core import ocr" in section and "ocr.check_engine()" in section and "ocr.find_tesseract()" in section
    assert "'python.exe', 'pythonw.exe'" in section                                  # With a console and, as the Start menu does, without.
    assert "Join-Path $App 'tesseract\\tesseract.exe'" in section                    # The Tesseract that came with the installer ...
    assert "does not find the Tesseract that came with the installer" in section and "cannot use it" in section    # ... must be found and usable.
    assert checker.index("# 4. GTK loads") < checker.index("# 4b.") < checker.index("# 5. The application starts")


def test_ci_checks_that_the_tesseract_row_and_ocr_agree_about_where_the_program_is():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    step = ci.split("- name: The Tesseract row finds whatever OCR would run")[1].split("- name:")[0]
    assert "ocr.find_tesseract()" in step and "ocr.check_engine()" in step and "shutil.which" in step
    assert "subprocess.run([\"tesseract\", \"--version\"]" in step                    # What pytesseract does when it is not told where the program is.
    assert "$env:PATH = \"`\"$odd`\";\"" in step                                        # On PATH only as a quoted folder ...
    assert "$env:LOCALAPPDATA = $empty" in step and "$env:XDG_DATA_HOME = $empty" in step    # ... and nowhere KaramVT itself looks.
    assert "if plain and not status.ok" in step and "throw" in step                   # The two must agree: counted, not a report.
    job = ci.split("windows-full-install:")[1]
    assert job.index("- name: OCR with the real Tesseract") < job.index("- name: The Tesseract row finds whatever OCR would run") < job.index("- name: Test suite with the real libraries installed")
