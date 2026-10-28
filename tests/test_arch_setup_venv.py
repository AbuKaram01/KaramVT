# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The script that makes the virtual environment of the Arch package (and its hook).

It is run against a stand-in python that only records what it is asked.
"""

import os
import subprocess
from pathlib import Path

from packaging_support import ARCH, make_executable, needs_bash


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
    """Run karamvt-setup-venv against the stand-in python; return the calls it made and the application folder."""
    log = tmp_path / "calls.log"
    log.write_text("")  # Only this run's calls.
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
    assert calls == []  # No environment yet: nothing to rebuild.

    run_setup(tmp_path, variant="cpu", version="3.13")
    calls, _ = run_setup(tmp_path, "--if-python-changed", variant="cpu", version="3.13")
    assert calls == []  # Same minor version.


@needs_bash
def test_the_python_hook_rebuilds_the_environment_after_a_new_minor_version(tmp_path):
    run_setup(tmp_path, variant="cuda", version="3.13")
    calls, app_dir = run_setup(tmp_path, "--if-python-changed", variant="cuda", version="3.14")
    assert f"python3 -m venv {app_dir}/venv" in calls
    assert calls[-1] == f"CMAKE_ARGS=[-DGGML_CUDA=on] pip install --quiet {app_dir}/src"
