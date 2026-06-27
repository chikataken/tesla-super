; Inno Setup script for the TFI Shipment Creator GUI.
;
; Build the exe first (pyinstaller shipment_creator.spec), then compile this with the
; Inno Setup Compiler (ISCC.exe installer.iss) to produce a single Setup.exe that a
; normal user double-clicks to install. build.bat does both steps for you.

#define AppName "TFI Shipment Creator"
#define AppExe "ShipmentCreator.exe"
#define AppVersion "1.0.0"
#define AppPublisher "TFI Trans"

[Setup]
; A fixed GUID identifies the app for upgrades/uninstall — keep it stable across versions.
AppId={{B2D1F3A4-7C6E-4F2B-9A1D-3E5C8F0A2B11}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename=ShipmentCreatorSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Program Files install needs admin; the app's own DATA lives in %LOCALAPPDATA% (per-user).
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#AppExe}

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
; Ship the whole PyInstaller onedir output.
Source: "dist\ShipmentCreator\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName} now"; Flags: nowait postinstall skipifsilent
