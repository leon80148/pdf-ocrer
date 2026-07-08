; Inno Setup script for pdf_ocrer (Windows installer, RapidOCR CPU build).
;
; Compile:  iscc /DMyAppVersion=0.5.0 packaging\installer.iss
; (build.ps1 supplies MyAppVersion from pdf_ocrer.__version__ automatically.)
;
; Paths below are relative to this .iss file (the packaging\ directory).

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "pdf-ocrer"
#define MyAppPublisher "leon80148"
#define MyAppURL "https://github.com/leon80148/pdf-ocrer"
#define MyAppExeName "pdf-ocrer-gui.exe"

[Setup]
; A stable AppId lets Inno recognise upgrade-in-place installs (never change it).
AppId={{35027203-4EE8-4A7A-8552-4ED74AB70AC4}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=Output
OutputBaseFilename=pdf-ocrer-setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Program Files install requires elevation.
PrivilegesRequired=admin

[Languages]
; Default.isl (English) ships with every Inno Setup install, so the build stays
; self-contained. The application UI itself is Traditional Chinese; a localized
; installer wizard could be added later by bundling an unofficial .isl.
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The whole PyInstaller onedir tree (both exes + shared _internal\).
; config.installer.toml is bundled inside _internal\; on first run the app seeds
; a per-user %APPDATA%\pdf_ocrer\config.toml from it (see config.bootstrap_frozen_config).
; Nothing user-writable is placed under Program Files, so non-admin users can edit
; settings and the config is found no matter which folder the app is launched from.
Source: "dist\pdf_ocrer\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent
