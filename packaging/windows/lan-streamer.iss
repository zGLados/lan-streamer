; Inno Setup script for the lan-streamer Windows installer.
; Built by packaging\windows\build.ps1, which passes AppVersion and StageDir.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#ifndef NumVersion
  #define NumVersion "0.0.0.0"
#endif
#ifndef StageDir
  #error StageDir is not set
#endif

[Setup]
AppId={{6F0B7C55-3D1E-4C1B-9E3A-6C2B9A1F4E21}
AppName=lan-streamer
AppVersion={#AppVersion}
VersionInfoVersion={#NumVersion}
AppPublisher=zGLados
AppPublisherURL=https://github.com/zGLados/lan-streamer
AppSupportURL=https://github.com/zGLados/lan-streamer/issues
AppUpdatesURL=https://github.com/zGLados/lan-streamer/actions/workflows/windows.yml
AppVerName=lan-streamer {#AppVersion}
; Default: all users, C:\Program Files\lan-streamer (asks for admin rights).
; The first page offers "only for me" (%LOCALAPPDATA%\Programs, no admin rights).
; Either way it is listed under Settings > Apps and Programs and Features.
DefaultDirName={autopf}\lan-streamer
DefaultGroupName=lan-streamer
DisableProgramGroupPage=yes
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog commandline
UsePreviousPrivileges=yes
UninstallDisplayIcon={app}\python\python.exe
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile={#StageDir}\LICENSE
OutputBaseFilename=lan-streamer-setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=lan-streamer

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\lan-streamer (1080p60)"; Filename: "{app}\start.cmd"; WorkingDir: "{app}"
Name: "{group}\lan-streamer (1080p30)"; Filename: "{app}\start.cmd"; Parameters: "--fps 30"; WorkingDir: "{app}"
Name: "{group}\lan-streamer self-test"; Filename: "{app}\self-test.cmd"; WorkingDir: "{app}"
Name: "{group}\Uninstall lan-streamer"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\self-test.cmd"; Description: "Run the self-test now"; Flags: postinstall shellexec skipifsilent runasoriginaluser

[UninstallDelete]
; Everything the app wrote at runtime: Python caches and MediaMTX certificates in
; the program folder, plugin registry, logs and plugin data in the user data folder.
Type: filesandordirs; Name: "{app}"
Type: filesandordirs; Name: "{localappdata}\lan-streamer"
