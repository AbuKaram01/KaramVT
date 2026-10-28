# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""packaging/windows/dlls.py: which DLLs an installed KaramVT needs that nothing inside it provides.

The script reads the import tables of Windows programs, so these tests build small Windows executables of their own
(headers, one section, the import tables) and run everywhere. Its answers on real libraries (ctranslate2, onnxruntime,
numpy, PyAV, Pillow and tokenizers wheels) were compared with the pefile library, file by file, when it was written.
"""

import importlib.util
import json
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("karamvt_dlls", ROOT / "packaging" / "windows" / "dlls.py")
dlls = importlib.util.module_from_spec(_spec)
# The dataclasses in dlls.py look their own module up in sys.modules, so it has to be registered before it runs.
sys.modules["karamvt_dlls"] = dlls
_spec.loader.exec_module(dlls)


def make_pe(static=(), delayed=(), plus=True, old_style_delay=False) -> bytes:
    """A minimal Windows executable that imports the DLLs ``static`` at load time and ``delayed`` on first use."""
    image_base = 0x140000000 if plus else 0x10000000
    section_va, raw_pointer = 0x1000, 0x400
    static_size, delay_size = 20 * (len(static) + 1), 32 * (len(delayed) + 1)
    strings = bytearray()
    static_names, delay_names = [], []
    for names, rvas in ((static, static_names), (delayed, delay_names)):
        for name in names:
            rvas.append(section_va + static_size + delay_size + len(strings))
            strings += name.encode("latin-1") + b"\0"
    body = bytearray()
    for rva in static_names:
        body += struct.pack("<IIIII", 0, 0, 0, rva, 0)
    body += bytes(20)
    for rva in delay_names:
        body += struct.pack(
            "<IIIIIIII", 0 if old_style_delay else 1, rva + (image_base if old_style_delay else 0), 0, 0, 0, 0, 0, 0
        )
    body += bytes(32)
    body += strings

    optional = bytearray(240 if plus else 224)
    struct.pack_into("<H", optional, 0, 0x20B if plus else 0x10B)
    if plus:
        struct.pack_into("<Q", optional, 24, image_base)
    else:
        struct.pack_into("<I", optional, 28, image_base)
    struct.pack_into("<I", optional, 60, raw_pointer)  # The SizeOfHeaders field.
    count_at, directories = (108, 112) if plus else (92, 96)
    struct.pack_into("<I", optional, count_at, 16)
    if static:
        struct.pack_into("<II", optional, directories + 8, section_va, static_size)
    if delayed:
        struct.pack_into("<II", optional, directories + 8 * 13, section_va + static_size, delay_size)
    coff = struct.pack("<HHIIIHH", 0x8664 if plus else 0x14C, 1, 0, 0, 0, len(optional), 0x2022)
    section = struct.pack(
        "<8sIIIIIIHHI", b".idata", len(body), section_va, len(body), raw_pointer, 0, 0, 0, 0, 0x40000040
    )
    dos = bytearray(0x40)
    dos[:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x40)
    header = bytes(dos) + b"PE\0\0" + coff + bytes(optional) + section
    return header.ljust(raw_pointer, b"\0") + bytes(body)


def put(path: Path, content: bytes = b"") -> Path:
    """Write ``content`` to ``path``, creating its folders, and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def program(path: Path, static=(), delayed=()) -> Path:
    """Write a minimal Windows executable that imports the given DLLs to ``path`` and return the path."""
    return put(path, make_pe(static, delayed))


# --- Reading import tables ---

@pytest.mark.parametrize("plus", [True, False], ids=["64-bit", "32-bit"])
def test_the_dlls_a_file_imports_are_read_from_its_import_tables(plus):
    imports = dlls.parse_imports(make_pe(["KERNEL32.dll", "MSVCP140.dll"], ["nvcuda.dll"], plus=plus))
    assert imports.static == ("KERNEL32.dll", "MSVCP140.dll")
    assert imports.delayed == ("nvcuda.dll",)


def test_a_delay_import_table_of_the_old_kind_holds_virtual_addresses_not_offsets():
    # Delay-import tables of the old kind occur in 32-bit files only.
    assert dlls.parse_imports(make_pe(delayed=["old.dll"], plus=False, old_style_delay=True)).delayed == ("old.dll",)


def test_a_file_that_imports_nothing_has_no_imports():
    assert dlls.parse_imports(make_pe()) == dlls.Imports((), ())


def test_the_imports_are_read_from_a_file_on_disk(tmp_path):
    assert dlls.read_imports(program(tmp_path / "a.dll", ["msvcp140.dll"])).static == ("msvcp140.dll",)


@pytest.mark.parametrize("content", [
    b"", b"MZ", b"hello world " * 20, b"\x7fELF" + bytes(100),
    b"MZ" + bytes(0x3A) + struct.pack("<I", 0x40) + bytes(40),  # An MZ header with no PE signature where it should be.
    make_pe(["a.dll"])[:100],  # Cut off inside the headers.
    make_pe(["a.dll"])[:0x410],  # Cut off inside the import table.
], ids=["empty", "mz-only", "text", "elf", "no-pe-signature", "short-headers", "short-section"])
def test_a_file_that_is_not_a_readable_windows_program_is_refused_not_misread(tmp_path, content):
    with pytest.raises(dlls.PeError):
        dlls.read_imports(put(tmp_path / "x.dll", content))


def test_an_import_table_pointing_outside_every_section_is_refused():
    damaged = bytearray(make_pe(["a.dll"]))
    struct.pack_into("<I", damaged, 0x40 + 4 + 20 + 112 + 8, 0x9000)  # The import directory's address.
    with pytest.raises(dlls.PeError, match="outside every section"):
        dlls.parse_imports(bytes(damaged))


# --- What names mean ---

@pytest.mark.parametrize("name", [
    "msvcp140.dll", "MSVCP140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_atomic_wait.dll", "vcruntime140.dll",
    "VCRUNTIME140_1.dll", "vcomp140.dll", "concrt140.dll", "vccorlib140.dll", "mfc140u.dll", "msvcr120.dll",
    "msvcp120.dll",
])
def test_the_runtime_dlls_of_visual_cpp_are_recognised(name):
    assert dlls.is_vcpp_runtime(name)


@pytest.mark.parametrize("name", [
    "kernel32.dll", "ucrtbase.dll", "python314.dll", "libstdc++-6.dll", "libiomp5md.dll", "vcruntime.dll",
    "msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll",  # A copy that a wheel carries under its own name
    "msvcrt.dll",
])
def test_other_dlls_are_not_taken_for_the_visual_cpp_runtime(name):
    assert not dlls.is_vcpp_runtime(name)


def test_api_sets_and_the_universal_c_runtime_belong_to_windows():
    assert (
        dlls.is_windows_builtin("api-ms-win-crt-runtime-l1-1-0.dll")
        and dlls.is_windows_builtin("EXT-MS-win-foo.dll")
    )
    assert dlls.is_windows_builtin("UCRTBASE.dll")
    assert not dlls.is_windows_builtin("msvcp140.dll")


# --- Scanning an installation ---

@pytest.fixture
def install(tmp_path):
    """An installation shaped like the real one, and the Python and Windows folders around it."""
    app, python, system = tmp_path / "app", tmp_path / "python", tmp_path / "system32"
    for name in ("kernel32.dll", "imagehlp.dll"):
        put(system / name)
    # The Python the environment is made from: its own interpreter and runtime DLLs, and an extension module.
    program(python / "python.exe", ["python314.dll", "KERNEL32.dll"])
    program(python / "python314.dll", ["VCRUNTIME140.dll", "KERNEL32.dll"])
    program(python / "vcruntime140.dll", ["KERNEL32.dll"])
    program(python / "vcruntime140_1.dll", ["KERNEL32.dll"])
    program(python / "DLLs" / "_ssl.pyd", ["libssl-3.dll", "python314.dll", "VCRUNTIME140.dll"])
    program(python / "DLLs" / "libssl-3.dll", ["KERNEL32.dll"])
    program(python / "Lib" / "site-packages" / "other" / "x.dll", ["something-else.dll"])  # Not part of the program.
    put(app / ".venv" / "pyvenv.cfg", f"home = {python}\ninclude-system-site-packages = false\n".encode())
    packages = app / ".venv" / "Lib" / "site-packages"
    program(packages / "ctranslate2" / "ctranslate2.dll",
            ["msvcp140.dll", "VCRUNTIME140.dll", "VCRUNTIME140_1.dll", "libiomp5md.dll", "KERNEL32.dll",
             "imagehlp.dll"],
            ["nvcuda.dll"])
    program(packages / "ctranslate2" / "libiomp5md.dll", ["KERNEL32.dll"])
    program(packages / "numpy" / "_core.pyd", ["msvcp140-abc.dll", "VCRUNTIME140.dll", "python314.dll"])
    program(packages / "numpy.libs" / "msvcp140-abc.dll", ["VCRUNTIME140.dll", "KERNEL32.dll"])
    program(app / "gtk" / "bin" / "libgtk-4-1.dll",
            ["VCRUNTIME140.dll", "MSVCP140.dll", "api-ms-win-crt-runtime-l1-1-0.dll", "KERNEL32.dll"])
    program(app / "tesseract" / "tesseract.exe", ["libtesseract-5.dll", "KERNEL32.dll"])
    program(app / "tesseract" / "libtesseract-5.dll", ["libstdc++-6.dll", "KERNEL32.dll"])
    # Files that the installer carries and that the scan has to skip.
    put(app / "wheelhouse" / "not-a-program.dll", b"a wheel in disguise")
    put(app / "python" / "python-3.14-amd64.exe", b"the Python installer")
    return app, python, system


def test_the_scan_names_the_visual_cpp_dlls_that_nothing_in_the_installation_provides(install):
    app, python, system = install
    # The Python folder comes from pyvenv.cfg, as it does when installed.
    report = dlls.scan(app, None, system)
    assert report.python_home == str(python)
    unprovided = {Path(file.replace("\\", "/")).name for file in report.vcpp["msvcp140.dll"]["unprovided"]}
    assert unprovided == {"libgtk-4-1.dll", "ctranslate2.dll"}
    assert report.vcpp["msvcp140.dll"]["importers"] == 2
    assert report.problems() == ["msvcp140.dll", "libstdc++-6.dll"]  # The second is imported by libtesseract-5.dll.


def test_the_python_folder_provides_its_runtime_to_everything_loaded_by_the_python_process(install):
    app, python, system = install
    report = dlls.scan(app, None, system)
    runtime = report.vcpp["vcruntime140.dll"]
    assert runtime["unprovided"] == [] and runtime["provided_by"] and "the Python folder" in runtime["provided_by"]
    assert report.vcpp["vcruntime140_1.dll"]["unprovided"] == []


def test_a_dll_a_wheel_carries_under_its_own_name_is_found_in_the_libs_folder_of_that_wheel(install):
    app, python, system = install
    report = dlls.scan(app, python, system)
    assert "msvcp140-abc.dll" not in report.missing                  # The copy that numpy carries in numpy.libs.
    assert "msvcp140-abc.dll" not in report.vcpp                     # It is not mistaken for the Redistributable's.


def test_a_dll_beside_its_importer_is_found_and_windows_dlls_are_not_judged(install):
    app, python, system = install
    report = dlls.scan(app, python, system)
    assert "libiomp5md.dll" not in report.missing  # It is beside ctranslate2.dll.
    assert "imagehlp.dll" not in report.missing  # It is in System32.
    assert "api-ms-win-crt-runtime-l1-1-0.dll" not in report.missing  # It is an API set.


def test_a_dll_that_nobody_provides_is_reported_as_missing_with_who_needs_it(install):
    app, python, system = install
    report = dlls.scan(app, python, system)
    assert list(report.missing) == ["libstdc++-6.dll"]
    assert [Path(file.replace("\\", "/")).name for file in report.missing["libstdc++-6.dll"]] == ["libtesseract-5.dll"]
    assert "libstdc++-6.dll" in report.problems()


def test_a_program_started_on_its_own_does_not_get_the_folders_the_python_process_registers(install):
    app, python, system = install
    # vcruntime140.dll is in the Python folder, not here.
    program(app / "tesseract" / "libleptonica.dll", ["VCRUNTIME140.dll"])
    report = dlls.scan(app, python, system)
    assert [
        Path(file.replace("\\", "/")).name for file in report.vcpp["vcruntime140.dll"]["unprovided"]
    ] == ["libleptonica.dll"]


def test_delay_loaded_dlls_are_information_not_a_problem(install):
    app, python, system = install
    report = dlls.scan(app, python, system)
    assert list(report.delayed) == ["nvcuda.dll"]
    assert "nvcuda.dll" not in report.problems()


def test_a_visual_cpp_dll_that_is_only_delay_loaded_is_information_too(install):
    app, python, system = install
    program(app / ".venv" / "Lib" / "site-packages" / "lazy" / "lazy.pyd", ["KERNEL32.dll"], ["VCOMP140.dll"])
    report = dlls.scan(app, python, system)
    # Not listed as "always provided (in: nowhere)".
    assert "vcomp140.dll" in report.delayed and "vcomp140.dll" not in report.vcpp
    assert "vcomp140.dll" not in report.problems()


def test_a_folder_of_runtime_dlls_inside_the_installation_provides_them(install):
    app, python, system = install
    program(app / "vcruntime" / "msvcp140.dll", ["KERNEL32.dll"])
    report = dlls.scan(app, python, system)
    assert report.vcpp["msvcp140.dll"]["unprovided"] == []
    assert "vcruntime" in report.vcpp["msvcp140.dll"]["provided_by"]
    assert report.problems() == ["libstdc++-6.dll"]


def test_only_what_the_program_runs_is_scanned_not_the_installers_it_carries_or_the_pythons_libraries(install):
    app, python, system = install
    report = dlls.scan(app, python, system)
    # The wheelhouse and python\ folders were skipped (their files are not programs), and so was the Python's Lib\.
    assert report.unreadable == []
    names = {entry for entry in report.vcpp} | set(report.missing) | set(report.delayed)
    assert "something-else.dll" not in names
    # In the app: ctranslate2 (2 files), numpy (2), gtk (1) and tesseract (2); pyvenv.cfg is not a program.
    # In the Python: python.exe, python314.dll, the two runtime DLLs, _ssl.pyd and libssl-3.dll.
    assert report.files == 7 + 6


def test_files_that_are_not_windows_programs_are_listed_not_fatal(install):
    app, python, system = install
    put(app / ".venv" / "Lib" / "site-packages" / "broken" / "broken.dll", b"not a dll")
    report = dlls.scan(app, python, system)
    assert [Path(path.replace("\\", "/")).name for path, _why in report.unreadable] == ["broken.dll"]
    assert report.vcpp["msvcp140.dll"]["unprovided"]  # The rest was still checked.


def test_without_a_system_folder_unknown_dlls_are_not_judged(install):
    app, python, _system = install
    report = dlls.scan(app, python, None)
    assert report.missing == {}  # Not on Windows: nothing says what Windows has.
    # The Visual C++ runtime needs no System32 to be judged.
    assert report.problems() == ["msvcp140.dll"]


def test_gtks_command_line_tools_are_counted_but_not_judged(install):
    app, python, system = install
    before = dlls.scan(app, python, system)
    program(app / "gtk" / "bin" / "gdbus.exe",
            ["VCRUNTIME140.dll", "MSVCP140.dll", "libglib-2.0-0.dll", "KERNEL32.dll"])
    program(app / "gtk" / "bin" / "cjpeg.exe", ["VCRUNTIME140.dll", "KERNEL32.dll"])
    program(app / "gtk" / "bin" / "libglib-2.0-0.dll", ["KERNEL32.dll"])
    after = dlls.scan(app, python, system)
    assert after.tools["count"] == 2 and before.tools["count"] == 0
    # The DLLs that the tools would look for on their own ...
    assert sorted(after.tools["needs"]) == ["msvcp140.dll", "vcruntime140.dll"]
    # ... are not reported as a problem, ...
    assert after.problems() == before.problems()
    # ... and the tools are not counted as importers.
    assert after.vcpp["msvcp140.dll"]["importers"] == before.vcpp["msvcp140.dll"]["importers"]
    assert after.vcpp["vcruntime140.dll"]["unprovided"] == before.vcpp["vcruntime140.dll"]["unprovided"]
    assert any(
        line.startswith("GTK's 2 command-line tools in gtk\\bin are not started by KaramVT")
        for line in dlls.format_report(after)
    )


def test_a_program_that_is_not_one_of_gtks_tools_is_still_judged_on_its_own(install):
    app, python, system = install
    program(app / "tesseract" / "extra.exe", ["VCRUNTIME140.dll"])
    report = dlls.scan(app, python, system)
    assert "extra.exe" in {Path(file.replace("\\", "/")).name for file in report.vcpp["vcruntime140.dll"]["unprovided"]}
    assert report.tools["count"] == 0


def test_a_name_that_is_allowed_is_not_a_problem(install):
    app, python, system = install
    report = dlls.scan(app, python, system, allow=("MSVCP140.dll", "libstdc++-6.dll"))
    assert report.problems() == []
    assert any("(allowed)" in line for line in dlls.format_report(report))


def test_the_report_says_what_is_missing_and_where_the_rest_is_found(install):
    app, python, system = install
    text = "\n".join(dlls.format_report(dlls.scan(app, python, system)))
    assert "msvcp140.dll: imported by 2 files; NOT PROVIDED for 2 of them" in text
    assert "vcruntime140.dll:" in text and "always provided (in: " in text
    assert "libstdc++-6.dll" in text and "nvcuda.dll" in text
    assert (
        text.splitlines()[-1].startswith("RESULT: 2 DLL(s)")
        and "msvcp140.dll, libstdc++-6.dll" in text.splitlines()[-1]
    )
    # The report has to be pure ASCII, so that a console that is not UTF-8 can print it.
    text.encode("ascii")


def test_the_report_of_a_complete_installation_says_so(install):
    app, python, system = install
    program(app / "vcruntime" / "msvcp140.dll", ["KERNEL32.dll"])
    program(app / "tesseract" / "libstdc++-6.dll", ["KERNEL32.dll"])
    lines = dlls.format_report(dlls.scan(app, python, system))
    assert lines[-1] == "RESULT: every DLL that the program loads comes with Windows or with the installation."


# --- Command line ---

def test_the_command_line_reports_and_only_fails_when_asked_to(install, capsys):
    app, python, system = install
    base = ["scan", "--app", str(app), "--system-dir", str(system)]
    assert dlls.main(base) == 0
    assert "NOT PROVIDED" in capsys.readouterr().out
    assert dlls.main([*base, "--strict"]) == 1
    assert dlls.main([*base, "--strict", "--allow", "msvcp140.dll", "--allow", "libstdc++-6.dll"]) == 0


def test_the_command_line_takes_another_folder_that_provides_dlls(install, tmp_path):
    app, python, system = install
    program(tmp_path / "extra" / "msvcp140.dll", ["KERNEL32.dll"])
    base = ["scan", "--app", str(app), "--system-dir", str(system), "--strict", "--allow", "libstdc++-6.dll"]
    assert dlls.main(base) == 1
    assert dlls.main([*base, "--provider", str(tmp_path / "extra")]) == 0


def test_the_command_line_writes_the_full_report_as_json(install, tmp_path):
    app, python, system = install
    target = tmp_path / "report.json"
    dlls.main(["scan", "--app", str(app), "--system-dir", str(system), "--json", str(target)])
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["vcpp"]["msvcp140.dll"]["importers"] == 2 and "libstdc++-6.dll" in data["missing"]


def test_on_github_the_scan_writes_a_job_summary_and_a_warning(install, tmp_path, monkeypatch, capsys):
    app, python, system = install
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert dlls.main(["scan", "--app", str(app), "--system-dir", str(system), "--github"]) == 0
    assert (
        "::warning title=The installed program needs DLLs from the machine::msvcp140.dll, libstdc++-6.dll"
        in capsys.readouterr().out
    )
    text = summary.read_text(encoding="utf-8")
    assert "`msvcp140.dll`" in text and "libstdc++-6.dll" in text and "| DLL |" in text


def test_on_github_a_complete_installation_gets_no_warning(install, tmp_path, monkeypatch, capsys):
    app, python, system = install
    program(app / "vcruntime" / "msvcp140.dll", ["KERNEL32.dll"])
    program(app / "tesseract" / "libstdc++-6.dll", ["KERNEL32.dll"])
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    assert dlls.main(["scan", "--app", str(app), "--system-dir", str(system), "--github", "--strict"]) == 0
    assert "::warning" not in capsys.readouterr().out
    assert "Everything the program loads comes with Windows" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_a_folder_without_an_installation_is_an_error_not_an_empty_report(tmp_path, capsys):
    assert dlls.main(["scan", "--app", str(tmp_path / "nothing")]) == 2
    assert "There is no installation at" in capsys.readouterr().out


# --- What a running program loaded ---

def test_loaded_modules_are_sorted_by_where_they_came_from(tmp_path):
    app, python, windows = tmp_path / "app", tmp_path / "py", tmp_path / "win"
    paths = [str(app / "gtk" / "bin" / "libgtk.dll"), str(python / "vcruntime140.dll"),
             str(windows / "System32" / "msvcp140.dll"), str(tmp_path / "Program Files" / "other" / "libomp.dll"),
             str(tmp_path / "application" / "x.dll")]
    sorted_paths = dlls.classify_loaded(paths, app, python, windows)
    assert (
        sorted_paths["app"] == [paths[0]]
        and sorted_paths["python"] == [paths[1]]
        and sorted_paths["windows"] == [paths[2]]
    )
    assert sorted_paths["other"] == paths[3:]  # "application" is not inside "app"


def test_the_folder_of_microsoft_defender_counts_as_windows_too(tmp_path):
    app, python = tmp_path / "app", tmp_path / "py"
    windows, defender = tmp_path / "win", tmp_path / "data" / "Windows Defender"
    paths = [str(defender / "Platform" / "MpOav.dll"), str(windows / "System32" / "kernel32.dll"),
             str(tmp_path / "elsewhere" / "x.dll")]
    sorted_paths = dlls.classify_loaded(paths, app, python, [windows, defender])
    assert sorted_paths["windows"] == paths[:2] and sorted_paths["other"] == paths[2:]


@pytest.mark.skipif(sys.platform == "win32", reason="the answer for a machine without Windows")
def test_listing_the_loaded_dlls_is_a_windows_command(tmp_path, capsys):
    assert dlls.main(["loaded", "--app", str(tmp_path)]) == 2
    assert "Windows only" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform != "win32", reason="lists the modules of a Windows process")
def test_on_windows_the_modules_of_the_running_process_are_listed():
    paths = dlls.loaded_module_paths()
    names = {Path(path).name.lower() for path in paths}
    assert Path(sys.executable).name.lower() in names or "python.exe" in names
    assert any(name.startswith("python3") and name.endswith(".dll") for name in names)
    assert "kernel32.dll" in names and all(Path(path).is_absolute() for path in paths)


@pytest.mark.skipif(sys.platform != "win32", reason="reads real Windows files")
def test_on_windows_the_real_import_tables_of_the_pythons_own_files_are_readable():
    imports = dlls.read_imports(Path(sys.executable))
    assert any(name.lower().startswith(("python3", "kernel32", "api-ms-win")) for name in imports.static)
    assert dlls.scan(Path(sys.prefix), Path(sys.base_prefix), Path(r"C:\Windows\System32")).files > 0


# --- The CI job that uses it ---

def test_the_installer_job_scans_the_installed_program_and_fails_when_something_would_come_from_the_machine():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = ci.split("  windows-installer-run:")[1].split("  windows-full-install:")[0]
    scan = job.split("- name: Which DLLs does the installed program need from the machine?")[1].split("- name:")[0]
    # The script runs with the installed program's own python, in isolated mode.
    assert "dlls.py scan --app $app --strict" in scan and "-I " in scan
    # The report goes to the job summary and to a warning, and the full report is kept as JSON.
    assert "--github" in scan and "--json" in scan
    # These two DLLs are allowed because only llama-common.dll imports them, and the program does not load it.
    assert "--allow libcrypto-3-x64.dll --allow libssl-3-x64.dll" in scan
    heading = "- name: Where do the Visual C++ runtime DLLs of the running program come from?"
    loaded = job.split(heading)[1].split("- uses:")[0]
    assert "dlls.py loaded --app $app --strict" in loaded
    for step in (scan, loaded):
        # The step runs even when an earlier step failed, so that its result is always reported.
        assert "if: ${{ !cancelled() }}" in step
        # A failed check fails the step.
        assert "throw" in step
    assert "path: ${{ runner.temp }}/dll-scan.json" in job and "name: dll-scan" in job
    upgraded = job.index("- name: Check the upgraded installation")
    # Both scans run on the upgraded installation, after its own check, and the report is uploaded last.
    assert (
        upgraded
        < job.index("Which DLLs does the installed program need")
        < job.index("Where do the Visual C++ runtime DLLs")
        < job.index("name: dll-scan")
    )
