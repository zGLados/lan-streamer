; Inno Setup script for the lan-streamer Windows installer.
; Saved as UTF-8 with BOM: Inno Setup reads the German texts correctly only then.
; Built by packaging\windows\build.ps1, which passes AppVersion and StageDir.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#ifndef NumVersion
  #define NumVersion "0.0.0.0"
#endif
#ifndef Compression
  #define Compression "max"
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
UninstallDisplayIcon={app}\lan-streamer.ico
SetupIconFile={#StageDir}\lan-streamer.ico
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile={#StageDir}\LICENSE
OutputBaseFilename=lan-streamer-setup-{#AppVersion}
Compression=lzma2/{#Compression}
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMANumBlockThreads=4
WizardStyle=modern
UninstallDisplayName=lan-streamer

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[CustomMessages]
english.Inputs=Built-in inputs to turn on (Standby is always on; this can be changed later on the admin page):
english.Submissions=Short-video submissions – viewers submit TikTok, Shorts and Reels links on the player page
english.Playlist=Playlist – plays files and links from a list
english.TestPattern=Test pattern – colour bars and a tone, for tests
german.Inputs=Eingebaute Eingänge einschalten (Standby ist immer an; später auf der Admin-Seite änderbar):
german.Submissions=Kurzvideo-Einreichungen – Zuschauer reichen TikTok-, Shorts- und Reels-Links auf der Player-Seite ein
german.Playlist=Playlist – spielt Dateien und Links aus einer Liste
german.TestPattern=Testbild – Farbbalken und Ton, für Tests

[Tasks]
; A reinstall preselects the previous answers. The app takes them over from
; setup.ini on the first start, and again when they change (see plugins/choice.py).
Name: "submissions"; Description: "{cm:Submissions}"; GroupDescription: "{cm:Inputs}"
Name: "playlist"; Description: "{cm:Playlist}"; GroupDescription: "{cm:Inputs}"
Name: "testpattern"; Description: "{cm:TestPattern}"; GroupDescription: "{cm:Inputs}"; Flags: unchecked

[INI]
Filename: "{app}\setup.ini"; Section: "plugins"; Key: "submissions"; String: "on"; Tasks: submissions
Filename: "{app}\setup.ini"; Section: "plugins"; Key: "submissions"; String: "off"; Tasks: not submissions
Filename: "{app}\setup.ini"; Section: "plugins"; Key: "playlist"; String: "on"; Tasks: playlist
Filename: "{app}\setup.ini"; Section: "plugins"; Key: "playlist"; String: "off"; Tasks: not playlist
Filename: "{app}\setup.ini"; Section: "plugins"; Key: "test-pattern"; String: "on"; Tasks: testpattern
Filename: "{app}\setup.ini"; Section: "plugins"; Key: "test-pattern"; String: "off"; Tasks: not testpattern

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Shortcuts point to python.exe, not to the .cmd files: the Windows start menu
; hides shortcuts whose target is not a program.
; The start entries run windowless (pythonw.exe) with a tray icon next to the clock.
Name: "{group}\lan-streamer (1080p60)"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m lanstreamer --tray"; WorkingDir: "{app}"; IconFilename: "{app}\lan-streamer.ico"; AppUserModelID: "zGLados.lan-streamer"; Comment: "Start lan-streamer and open the player page"
Name: "{group}\lan-streamer (1080p30)"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m lanstreamer --tray --fps 30"; WorkingDir: "{app}"; IconFilename: "{app}\lan-streamer.ico"; AppUserModelID: "zGLados.lan-streamer"; Comment: "Start lan-streamer with 30 fps"
Name: "{group}\lan-streamer self-test"; Filename: "{app}\python\python.exe"; Parameters: "-m lanstreamer.doctor --pause"; WorkingDir: "{app}"; IconFilename: "{app}\lan-streamer.ico"; Comment: "Check GStreamer and the video encoders"

[UninstallDelete]
; Everything the app wrote at runtime: Python caches and MediaMTX certificates in
; the program folder, plugin registry, logs and plugin data in the user data folder.
Type: filesandordirs; Name: "{app}"
Type: filesandordirs; Name: "{localappdata}\lan-streamer"
