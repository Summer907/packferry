; Build on Windows with Inno Setup after build.ps1 produced dist/packferry.
#define AppName "PackFerry"
#define AppVersion "0.1.0"
#define RepoRoot AddBackslash(SourcePath) + "..\\..\\"

[Setup]
AppId={{8E379D8E-83B0-4314-9F58-E492B2949B65}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\PackFerry
DefaultGroupName=PackFerry
OutputDir={#RepoRoot}dist\installers
OutputBaseFilename=PackFerry-0.1.0-Windows-x64-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayIcon={app}\packferry.exe

[Files]
Source: "{#RepoRoot}dist\packferry\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\PackFerry"; Filename: "{app}\packferry.exe"
Name: "{autodesktop}\PackFerry"; Filename: "{app}\packferry.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Run]
Filename: "{app}\packferry.exe"; Description: "启动 PackFerry"; Flags: nowait postinstall skipifsilent
