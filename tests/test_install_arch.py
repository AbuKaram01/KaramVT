# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""scripts/install-arch.sh, run with pacman and python replaced by stand-ins that only record what they are asked.

The installer refuses to run as root (as it should), so these tests are skipped for root."""

import os
import re
import shutil
import subprocess

from packaging_support import APP_ID, ROOT, make_executable, needs_bash, not_root


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
    checkout.run()  # A CPU install first.
    checkout.log.write_text("")

    _result, calls = checkout.run("--cuda")
    assert (
        "CMAKE_ARGS=[-DGGML_CUDA=on] pip install --force-reinstall --no-cache-dir --no-deps -q llama-cpp-python"
        in calls
    )

    checkout.log.write_text("")
    _result, calls = checkout.run("--cuda")  # Already CUDA: no second compile.
    assert not any("force-reinstall" in call for call in calls)

    checkout.log.write_text("")
    _result, calls = checkout.run()  # ...and back to the CPU build.
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
