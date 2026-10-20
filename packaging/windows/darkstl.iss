; Inno Setup script for the Windows installer, DarkSTL_<version>_x64-setup.exe. Build the app
; with darkstl.spec first, then from the repository root:
;   ISCC.exe /DAppVersion=0.1.1 packaging\windows\darkstl.iss
; It installs for the current user (no administrator rights), with a Start menu entry.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6C1E3B52-8F0D-4B7E-9A55-2D3C0F4A7B61}
AppName=Dark STL
AppVersion={#AppVersion}
AppVerName=Dark STL {#AppVersion}
AppPublisher=DrFlGd
AppPublisherURL=https://github.com/DrFlGd/Dark-STL
DefaultDirName={autopf}\Dark STL
DefaultGroupName=Dark STL
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=DarkSTL_{#AppVersion}_x64-setup
SetupIconFile=darkstl.ico
UninstallDisplayIcon={app}\Dark STL.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\..\build\windows\dist\Dark STL\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Dark STL"; Filename: "{app}\Dark STL.exe"
Name: "{autodesktop}\Dark STL"; Filename: "{app}\Dark STL.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Dark STL.exe"; Description: "{cm:LaunchProgram,Dark STL}"; Flags: nowait postinstall skipifsilent
