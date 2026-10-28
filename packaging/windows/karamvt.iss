; SPDX-FileCopyrightText: 2026 AbuKaram01
; SPDX-License-Identifier: AGPL-3.0-or-later
;
; Inno Setup script for the Windows installer. Build it with packaging\build-windows.ps1.
;
; The installer ships KaramVT's own files, a portable Tesseract, the GTK 4 + libadwaita runtime, Python's installer
; and the Python libraries (a "wheelhouse") when packaging\build-windows.ps1 prepared them. After copying them it runs
; scripts\install-windows.ps1, which installs Python (when it is missing), creates the Python environment and installs
; the libraries from the wheelhouse, and downloads only what the installer does not carry. The Whisper and translation
; models are downloaded by the application itself on first use. Everything is per user: no administrator rights are
; required.

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#define AppName "KaramVT"
#define AppId "io.github.AbuKaram01.KaramVT"

[Setup]
AppId={{C3A1F6B2-8D47-4E19-A5C0-6E2B9D4F7A18}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=AbuKaram01
AppPublisherURL=https://github.com/AbuKaram01/KaramVT
DefaultDirName={localappdata}\Programs\{#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\..\LICENSE
SetupIconFile=..\..\data\windows\karamvt.ico
UninstallDisplayIcon={app}\data\windows\karamvt.ico
OutputDir=..\dist
OutputBaseFilename=karamvt-{#AppVersion}-windows-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "cuda"; Description: "NVIDIA GPU support (CUDA). Compiles llama.cpp; needs the CUDA Toolkit and the Visual Studio Build Tools"; Flags: unchecked
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Files]
Source: "..\..\src\*"; DestDir: "{app}\src"; Excludes: "__pycache__,*.pyc,*.egg-info"; Flags: recursesubdirs ignoreversion
Source: "..\..\data\*"; DestDir: "{app}\data"; Flags: recursesubdirs ignoreversion
Source: "..\..\scripts\install-windows.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\..\scripts\tesseract-portable.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\..\scripts\gtk-portable.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\..\scripts\python-bundle.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\..\scripts\uninstall-windows.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\..\pyproject.toml"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

; A portable Tesseract that packaging\build-windows.ps1 prepared: installed beside the application, so nothing else
; has to be downloaded or installed. Without it the install script fetches one (or uses winget).
#define TesseractFolder SourcePath + "\..\tesseract"
#if DirExists(TesseractFolder)
Source: "{#TesseractFolder}\*"; DestDir: "{app}\tesseract"; Flags: recursesubdirs ignoreversion
#endif

; The GTK 4 + libadwaita runtime that packaging\build-windows.ps1 prepared: installed beside the application, where
; the application finds it by itself, so that it does not have to be downloaded. The install script uses it when it is
; there and downloads one only when it is not.
#define GtkFolder SourcePath + "\..\gtk"
#if DirExists(GtkFolder)
Source: "{#GtkFolder}\*"; DestDir: "{app}\gtk"; Flags: recursesubdirs ignoreversion
#endif

; The python.org installer of the Python that the GTK runtime's wheels are for: the install script runs it, silently and
; for the current user, when that Python is missing, so that winget is not needed.
#define PythonFolder SourcePath + "\..\python"
#if DirExists(PythonFolder)
Source: "{#PythonFolder}\*"; DestDir: "{app}\python"; Flags: recursesubdirs ignoreversion
#endif

; The Python libraries (wheels) that packaging\build-windows.ps1 prepared: the install script installs them without
; internet access, and without compiling llama.cpp.
#define WheelFolder SourcePath + "\..\wheelhouse"
#if DirExists(WheelFolder)
Source: "{#WheelFolder}\*"; DestDir: "{app}\wheelhouse"; Flags: recursesubdirs ignoreversion
#endif

; The Visual C++ runtime DLLs (msvcp140.dll ...) that packaging\build-windows.ps1 collected: the libraries that need them
; do not load on a computer that lacks the Visual C++ Redistributable. The application loads these before anything
; else (src\karamvt\winsupport.py).
#define VcRuntimeFolder SourcePath + "\..\vcruntime"
#if DirExists(VcRuntimeFolder)
Source: "{#VcRuntimeFolder}\*"; DestDir: "{app}\vcruntime"; Flags: recursesubdirs ignoreversion
#endif

[InstallDelete]
#if DirExists(GtkFolder)
; An upgrade starts from an empty runtime folder, so that no file of an older runtime stays beside the new DLLs.
Type: filesandordirs; Name: "{app}\gtk"
#endif
#if DirExists(PythonFolder)
Type: filesandordirs; Name: "{app}\python"
#endif
#if DirExists(WheelFolder)
Type: filesandordirs; Name: "{app}\wheelhouse"
#endif
#if DirExists(VcRuntimeFolder)
Type: filesandordirs; Name: "{app}\vcruntime"
#endif

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: "-m karamvt.gui"; WorkingDir: "{app}"; IconFilename: "{app}\data\windows\karamvt.ico"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: "-m karamvt.gui"; WorkingDir: "{app}"; IconFilename: "{app}\data\windows\karamvt.ico"; Tasks: desktopicon

[Registry]
; The application registers its notification identity here at run time; this makes the uninstaller remove it.
Root: HKCU; Subkey: "Software\Classes\AppUserModelId\{#AppId}"; Flags: uninsdeletekey

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\src"
Type: filesandordirs; Name: "{app}\build"
Type: filesandordirs; Name: "{app}\gtk"
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\wheelhouse"
Type: filesandordirs; Name: "{app}\vcruntime"
; The GTK runtime an older installer downloaded. Books, models, the index and conversations are never removed.
Type: filesandordirs; Name: "{localappdata}\karamvt\gtk"
Type: filesandordirs; Name: "{localappdata}\karamvt\tesseract"

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  Parameters: String;
  ResultCode: Integer;
begin
  if CurStep <> ssPostInstall then
    Exit;
  Parameters := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\scripts\install-windows.ps1') + '" -NoShortcut -LogFile "' + ExpandConstant('{localappdata}\karamvt\install.log') + '"';
  if WizardIsTaskSelected('cuda') then
    Parameters := Parameters + ' -Cuda';
#if DirExists(GtkFolder) && DirExists(PythonFolder) && DirExists(WheelFolder)
  WizardForm.StatusLabel.Caption := 'Setting up Python and its libraries (everything is already in this installer; this takes a few minutes)...';
#elif DirExists(GtkFolder)
  WizardForm.StatusLabel.Caption := 'Setting up Python and the dependencies (GTK is already in this installer; what is not is downloaded)...';
#else
  WizardForm.StatusLabel.Caption := 'Setting up GTK, Python and the dependencies (about a gigabyte is downloaded)...';
#endif
  if (not Exec('powershell.exe', Parameters, ExpandConstant('{app}'), SW_SHOW, ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
    MsgBox('KaramVT was copied, but its components could not be set up.' + #13#10 + #13#10 +
           'What went wrong is written in ' + ExpandConstant('{localappdata}\karamvt\install.log') + #13#10 + #13#10 +
           'Check your internet connection and run this again from a PowerShell window:' + #13#10 +
           'powershell -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\scripts\install-windows.ps1') + '"',
           mbError, MB_OK);
end;
